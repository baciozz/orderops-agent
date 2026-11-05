"""Offline observability contracts, including the real fixture HTTP golden chain."""

import asyncio
import json
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from inspect import signature
from io import StringIO
from uuid import uuid4

import httpx
import pytest
from app.adapters.budget import BudgetLedger
from app.adapters.fixture_model import FixtureLLMClient
from app.adapters.live_model import LiveLLMClient
from app.config import Settings
from app.db.models import ModelUsage, Operation, OutboxDelivery, Task
from app.db.session import begin_immediate
from app.errors import AppError, BudgetExceeded, FixtureNotFound
from app.main import create_app
from app.metrics import reset_for_tests, snapshot
from app.observability import (
    JSONFormatter,
    RedactionFilter,
    configure_logging,
    enrich,
    get_logger,
    log_context,
    log_event,
    observed,
    task_log_fields,
    text_digest,
)
from app.orchestration.dispatch import dispatch_text
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE, golden_fixtures
from app.orchestration.inbox import accept_input
from app.orchestration.outbox import consume, publish_pending
from app.orchestration.recovery import ResponseLost, durable_commit
from app.orchestration.registry import Registry
from app.orchestration.state import check_fence, transition
from app.orchestration.watchdog import scan
from app.orchestration.worker import claim, run_once
from app.services import kernel as kernel_service
from app.services.kernel import approve_plan, commit_plan, retry_pending_verification
from app.services.seed import load_seed
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from tests.test_stage4_live import ledger, request, response, settings

NOW = datetime(2026, 11, 3, tzinfo=UTC)
logger = get_logger(__name__)


@pytest.fixture(autouse=True)
def offline_logging(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Observability tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    configure_logging(Settings(_env_file=None))
    yield
    configure_logging(Settings(_env_file=None))


@pytest.fixture()
def web(session_factory, database_url, tmp_path):
    load_seed(session_factory, database_url=database_url)
    configured = Settings(
        _env_file=None, database_url=database_url, llm_api_key="sk-fake-observability-app-key"
    )
    app = create_app(configured)
    app.state.source_storage_dir = tmp_path
    try:
        with TestClient(app) as client:
            yield client, session_factory, configured, app
    finally:
        app.state.engine.dispose()


def events(caplog, name):
    return [record for record in caplog.records if getattr(record, "event", None) == name]


def captured(caplog):
    return caplog.text + "\n".join(
        JSONFormatter().format(record) for record in caplog.records if record.name.startswith("oos")
    )


def fixture_plan(web, text=TEXT_BLACK_TO_WHITE):
    client, factory, configured, _ = web
    receipt = client.post(
        "/inputs", json={"thread_id": "test-thread", "client_event_key": "one", "text": text}
    )
    assert receipt.status_code == 202
    outcome = run_once(
        factory,
        settings=configured,
        clock=lambda: NOW,
        fixtures={text: golden_fixtures()[TEXT_BLACK_TO_WHITE]},
    )
    assert outcome.status == "AWAITING_APPROVAL"
    return receipt, outcome


def committed(web):
    client, _, _, _ = web
    receipt, plan = fixture_plan(web)
    approval = client.post(
        f"/plans/{plan.plan_id}/approve",
        json={"plan_hash": plan.plan_hash, "expected_intent_revision": plan.intent_revision},
    )
    assert approval.status_code == 200
    result = client.post(
        f"/plans/{plan.plan_id}/commit",
        json={
            "approval_id": approval.json()["approval_id"],
            "plan_hash": plan.plan_hash,
            "idempotency_key": "observability-commit",
        },
    )
    assert result.status_code == 200
    return receipt, plan, result


def test_json_formatter_serializes_types(caplog):
    identity = uuid4()
    log_event(logger, "test.formatted", amount=Decimal("1.25"), at=NOW, identity=identity)
    body = json.loads(JSONFormatter().format(events(caplog, "test.formatted")[0]))
    assert body["level"] == "INFO" and body["event"] == "test.formatted"
    assert body["message"] == "test.formatted" and body["logger"].startswith("oos.")
    assert datetime.fromisoformat(body["ts"]).utcoffset() == timedelta(0)
    assert body["amount"] == "1.25" and body["identity"] == str(identity)
    assert body["at"] == str(NOW)


def test_redacts_messages_nested_extras_and_secretstr(caplog):
    key = "sk-test-FAKE1234567890abcdef"
    configured_secret = "opaque-configured-credential"
    configure_logging(Settings(_env_file=None, llm_api_key=configured_secret))
    log_event(
        logger,
        "test.redacted",
        api_key=key,
        note=f"Authorization: Bearer xyz; key={key}; configured={configured_secret}",
        nested={
            "PASSWORD": "nested-password",
            "items": [{"Cookie": "session-cookie"}],
            "safe": SecretStr("never-expand-this"),
        },
    )
    logger.info({"token": "message-token", "other": "Bearer message-bearer"})
    logger.info("authorization %s", {"secret": "argument-secret"})
    output = captured(caplog)
    for secret in (
        key,
        "Bearer xyz",
        "nested-password",
        "session-cookie",
        configured_secret,
        "never-expand-this",
        "message-token",
        "message-bearer",
        "argument-secret",
    ):
        assert secret not in output
    assert "[REDACTED]" in output
    assert isinstance(logging.getLogger("oos").filters[0], RedactionFilter)


def test_fixture_golden_events_and_configured_key(web, caplog):
    _, plan, result = committed(web)
    assert publish_pending(web[1]) == 1
    for event in (
        "inbox.received",
        "dispatch.start",
        "dispatch.finish",
        "model.call",
        "approval.granted",
        "commit.applied",
        "outbox.delivered",
    ):
        records = events(caplog, event)
        assert records, event
        for record in records:
            assert record.task_id == plan.task_id
            if event in {"dispatch.start", "dispatch.finish", "model.call"}:
                assert record.mode == "FIXTURE"
            else:
                assert "mode" not in vars(record)
            assert record.duration_ms >= 0
            assert record.outcome in {"started", "success"}
    assert events(caplog, "dispatch.start")[0].attempt_id
    call = events(caplog, "model.call")[0]
    assert call.model == "fixture" and call.prompt_kind
    assert call.input_tokens == call.output_tokens == 0
    assert events(caplog, "approval.granted")[0].approval_id
    commit = events(caplog, "commit.applied")[0]
    assert commit.order_id == result.json()["order_id"] and commit.revision == 2
    assert "sk-fake-observability-app-key" not in captured(caplog)
    assert TEXT_BLACK_TO_WHITE not in captured(caplog)


def test_customer_text_and_upload_content_stay_out_of_logs(web, caplog):
    marker = "UNIQUE_CUSTOMER_MESSAGE_4d13c9"
    _, plan = fixture_plan(web, text=marker)
    upload = web[0].post(
        "/sources", data={"task_id": plan.task_id}, files={"file": ("notes.txt", marker.encode())}
    )
    assert upload.status_code == 200
    output = captured(caplog)
    assert marker not in output
    receipt = events(caplog, "inbox.received")[0]
    assert receipt.text_len == len(marker)
    assert receipt.text_sha256 == text_digest(marker)["text_sha256"]
    assert len(receipt.text_sha256) == 12
    assert events(caplog, "source.parsed")[0].text_sha256 == receipt.text_sha256
    assert events(caplog, "upload.received")[0].file_size == len(marker.encode())


def test_inbox_duplicate_logged(web, caplog):
    receipt, _ = fixture_plan(web)
    duplicate = web[0].post(
        "/inputs",
        json={"thread_id": "test-thread", "client_event_key": "one", "text": TEXT_BLACK_TO_WHITE},
    )
    assert duplicate.json()["event_id"] == receipt.json()["event_id"]
    assert [record.duplicate for record in events(caplog, "inbox.received")] == [False, True]
    assert snapshot()["tasks_enqueued"] == 1


@pytest.mark.parametrize(
    "path,status,code",
    [
        ("/health?hidden=do-not-log", 200, None),
        ("/tasks/not-present/view", 404, "TASK_NOT_FOUND"),
    ],
)
def test_http_ids_path_duration_and_handled_errors(web, caplog, path, status, code):
    result = web[0].get(path, headers={"x-correlation-id": "corr-test"})
    assert result.status_code == status
    record = events(caplog, "http.request")[-1]
    assert record.request_id == result.headers["x-request-id"]
    assert record.correlation_id == result.headers["x-correlation-id"] == "corr-test"
    assert record.path == path.split("?")[0] and record.method == "GET"
    assert record.status_code == status and record.duration_ms >= 0
    assert record.error_code == code
    assert record.app_mode == "FIXTURE" and "mode" not in vars(record)
    assert "do-not-log" not in captured(caplog)


def test_http_unhandled_exception_logs_500(web, caplog):
    @web[3].get("/test-unhandled")
    def fail():
        raise RuntimeError("PRIVATE_EXCEPTION_BODY")

    with TestClient(web[3], raise_server_exceptions=False) as client:
        assert client.get("/test-unhandled").status_code == 500
    record = events(caplog, "http.request")[-1]
    assert record.status_code == 500 and record.duration_ms >= 0 and record.request_id
    assert record.error_code == "INTERNAL_ERROR"
    assert "PRIVATE_EXCEPTION_BODY" not in captured(caplog)


def test_approval_rejection_and_expired_commit(web, caplog):
    _, plan = fixture_plan(web)
    with pytest.raises(AppError, match="意图版本已变化"):
        approve_plan(
            web[1],
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            expected_intent_revision=plan.intent_revision + 1,
            now=NOW,
        )
    rejected = events(caplog, "approval.rejected")[-1]
    assert rejected.task_id == plan.task_id and rejected.error_code == "STALE_INTENT"
    assert rejected.actor == "demo-user" and rejected.duration_ms >= 0
    approval = approve_plan(
        web[1],
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
        now=NOW,
        ttl_seconds=1,
    )
    with pytest.raises(AppError) as error:
        commit_plan(
            web[1],
            plan_id=plan.plan_id,
            approval_id=approval.approval_id,
            plan_hash=plan.plan_hash,
            idempotency_key="expired",
            now=NOW + timedelta(seconds=2),
        )
    assert error.value.code == "EXPIRED"
    failure = events(caplog, "commit.failed")[-1]
    assert failure.task_id == plan.task_id and failure.error_code == "EXPIRED"
    assert failure.approval_id == approval.approval_id and failure.duration_ms >= 0


def test_dispatch_and_fixture_model_failure(web, caplog):
    accept_input(web[1], thread_id="bad", client_event_key="bad", text="PRIVATE_UNKNOWN_TEXT")
    result = run_once(web[1], settings=web[2], clock=lambda: NOW)
    assert result.error_code == "FIXTURE_NOT_FOUND"
    for event in ("model.call", "dispatch.finish"):
        record = events(caplog, event)[-1]
        assert record.error_code == "FIXTURE_NOT_FOUND" and record.outcome == "failed"
        assert record.task_id == result.task_id and record.duration_ms >= 0
    assert "PRIVATE_UNKNOWN_TEXT" not in captured(caplog)
    assert snapshot()["tasks_failed"] == 1
    assert snapshot()["worker_claims"] == 1
    assert snapshot()["model_calls"] == 1


def test_outbox_failure_and_duplicate(web, caplog):
    _, plan, _ = committed(web)

    def fail(*args):
        raise AppError("PRIVATE_DELIVERY_BODY", code="DELIVERY_TEST")

    assert publish_pending(web[1], send=fail) == 0
    record = events(caplog, "outbox.failed")[-1]
    assert record.task_id == plan.task_id and record.error_code == "DELIVERY_TEST"
    assert "PRIVATE_DELIVERY_BODY" not in captured(caplog)
    with web[1]() as session:
        delivery = session.scalar(select(OutboxDelivery))
        event_id, consumer = delivery.event_id, delivery.consumer
    assert consume(web[1], event_id, consumer)
    assert not consume(web[1], event_id, consumer)
    duplicate = events(caplog, "outbox.duplicate_skipped")[-1]
    assert duplicate.task_id == plan.task_id and duplicate.outcome == "duplicate"
    assert publish_pending(web[1], clock=lambda: datetime.now(UTC) + timedelta(minutes=2)) == 1


def test_outbox_post_send_failure_preserves_exception(web, caplog):
    committed(web)

    def crash(point):
        raise RuntimeError("PRIVATE_FAULT")

    with pytest.raises(RuntimeError, match="PRIVATE_FAULT"):
        publish_pending(web[1], fault=crash)
    assert events(caplog, "outbox.failed")[-1].error_code == "INTERNAL_ERROR"
    with web[1]() as session:
        assert session.scalar(select(OutboxDelivery)).delivery_state == "PENDING"
    assert "PRIVATE_FAULT" not in captured(caplog)


def test_budget_live_mock_transport_events(session_factory, caplog):
    configured = settings()
    book = ledger(session_factory, configured)
    client = LiveLLMClient(
        configured,
        book,
        transport=httpx.MockTransport(
            lambda _: response(payload={"private": "PRIVATE_MODEL_OUTPUT"})
        ),
    )
    task_id = accept_input(
        session_factory, thread_id="budget", client_event_key="budget", text="budget-input"
    )[1]
    result = client.structured_call(request(task_id=task_id, user_prompt="PRIVATE_MODEL_PROMPT"))
    reserve = events(caplog, "budget.reserve")[-1]
    settle = events(caplog, "budget.settle")[-1]
    for record in (reserve, settle):
        assert record.task_id == task_id and record.mode == "LIVE"
        assert record.cap_usd == "5.00" and Decimal(record.reserved_usd) > 0
        assert record.input_tokens > 0 and record.output_tokens > 0
        assert record.duration_ms >= 0 and record.outcome == "success"
    assert settle.actual_usd == result.usage.cost_usd
    assert (settle.input_tokens, settle.output_tokens) == (1000, 200)
    assert events(caplog, "model.call")[-1].input_tokens == 1000
    assert "PRIVATE_MODEL_PROMPT" not in captured(caplog)
    assert "PRIVATE_MODEL_OUTPUT" not in captured(caplog)


def test_budget_refused_before_transport(session_factory, caplog):
    configured = settings(live_budget_usd=Decimal("0"))
    client = LiveLLMClient(
        configured,
        ledger(session_factory, configured),
        transport=httpx.MockTransport(lambda _: pytest.fail("must refuse before transport")),
    )
    with pytest.raises(BudgetExceeded):
        client.structured_call(request(task_id="task-refused"))
    refusal = events(caplog, "budget.refused")[-1]
    assert refusal.error_code == "BUDGET_EXCEEDED" and refusal.cap_usd == "0"
    assert Decimal(refusal.reserved_usd) > 0 and refusal.task_id == "task-refused"
    assert events(caplog, "model.call")[-1].error_code == "BUDGET_EXCEEDED"
    assert snapshot()["budget_refused"] == snapshot()["model_calls"] == 1


def test_configure_idempotent_and_leaves_root_untouched():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    namespace = logging.getLogger("oos")
    before = list(namespace.handlers)
    configured = Settings(_env_file=None)
    configure_logging(configured)
    configure_logging(configured)
    assert namespace.handlers == before and namespace.propagate is True
    assert root.handlers == handlers and root.level == level


def test_env_warning_level_suppresses_info(monkeypatch, caplog):
    monkeypatch.setenv("OOS_LOG_LEVEL", "WARNING")
    configured = Settings(_env_file=None)
    assert configured.log_level == "WARNING"
    configure_logging(configured)
    log_event(logger, "test.suppressed")
    assert not events(caplog, "test.suppressed")


def test_env_plaintext_format_is_redacted(monkeypatch):
    monkeypatch.setenv("OOS_LOG_JSON", "false")
    configure_logging(Settings(_env_file=None))
    namespace = logging.getLogger("oos")
    handler = namespace.handlers[0]
    record = logging.makeLogRecord(
        {
            "name": "oos.test",
            "levelname": "INFO",
            "msg": "Bearer do-not-print",
            "event": "test.text",
        }
    )
    assert "do-not-print" not in handler.format(record)
    assert handler.format(record).startswith("INFO oos.test")


def test_broken_logging_handler_does_not_change_business(caplog):
    class BrokenHandler(logging.Handler):
        def emit(self, record):
            raise RuntimeError("broken log sink")

    broken = BrokenHandler()
    namespace = logging.getLogger("oos")
    namespace.addHandler(broken)
    try:
        log_event(logger, "test.sink_failed")
        result = FixtureLLMClient({"test": {}}).structured_call(request(fixture_key="test"))
        assert result.payload == {}
        # Same exception and message from the business call despite the broken sink.
        with pytest.raises(FixtureNotFound):
            FixtureLLMClient().structured_call(request())
    finally:
        namespace.removeHandler(broken)


def test_circular_and_unprintable_values_are_safe(caplog):
    class Unprintable:
        def __str__(self):
            raise ValueError("bad str")

    cycle = []
    cycle.append(cycle)
    log_event(logger, "test.safe_values", cycle=cycle, value=Unprintable())
    result = json.loads(JSONFormatter().format(events(caplog, "test.safe_values")[-1]))
    assert result["cycle"] == ["[CIRCULAR]"] and result["value"] == "[UNPRINTABLE]"


def test_standard_message_formatting_still_works(caplog):
    logger.info("count=%d note=%s", 3, "safe")
    assert caplog.records[-1].getMessage() == "count=3 note=safe"
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactionFilter())
    handler.handle(logging.makeLogRecord({"msg": {"Password": "never-print"}}))
    assert "never-print" not in stream.getvalue()


@pytest.mark.parametrize("value", ["SK-2025", "sk-abc", "task-sk-1234567890abcdef"])
def test_redaction_preserves_noncredentials(value: str, caplog: pytest.LogCaptureFixture) -> None:
    log_event(logger, "test.noncredential", value=value)
    assert events(caplog, "test.noncredential")[-1].value == value


@pytest.mark.parametrize(
    "value",
    [
        "sk-test-FAKE1234567890abcdef",
        "sk-proj-1234567890abcdefghijklmnopqrstuvwxyz",
        "Bearer example-credential",
        "bEaReR example-credential",
    ],
)
def test_redaction_matches_credentials(value: str, caplog: pytest.LogCaptureFixture) -> None:
    log_event(logger, "test.credential", value=value)
    assert events(caplog, "test.credential")[-1].value == "[REDACTED]"


def test_observed_sync_preserves_signature_defaults_and_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @observed(logger, "test.sync", fields=lambda task_id: {"task_id": task_id})
    def business(task_id: str = "default-task") -> str:
        enrich(order_id="order-sync")
        return task_id

    assert str(signature(business)) == "(task_id: str = 'default-task') -> str"
    assert business.__name__ == "business" and business.__wrapped__.__name__ == "business"
    with log_context(correlation_id="parent"):
        assert business() == "default-task"
        assert business(task_id="keyword-task") == "keyword-task"
        log_event(logger, "test.after_sync")
    records = events(caplog, "test.sync")
    assert [record.task_id for record in records] == ["default-task", "keyword-task"]
    assert all(
        record.order_id == "order-sync" and record.correlation_id == "parent" for record in records
    )
    assert "task_id" not in vars(events(caplog, "test.after_sync")[-1])


def test_observed_async_preserves_return_exception_and_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure = AppError("original message", code="TEST_ASYNC")

    @observed(
        logger,
        "test.async",
        failed_event="test.async_failed",
        fields=lambda task_id, **_: {"task_id": task_id},
    )
    async def business(task_id: str, *, fail: bool = False) -> str:
        await asyncio.sleep(0)
        enrich(order_id="order-async")
        if fail:
            raise failure
        return task_id

    async def exercise() -> None:
        assert await business("async-task") == "async-task"
        with pytest.raises(AppError) as error:
            await business(task_id="failed-task", fail=True)
        assert error.value is failure and str(error.value) == "original message"
        log_event(logger, "test.after_async")

    asyncio.run(exercise())
    assert events(caplog, "test.async")[-1].task_id == "async-task"
    failed = events(caplog, "test.async_failed")[-1]
    assert failed.task_id == "failed-task" and failed.error_code == "TEST_ASYNC"
    assert failed.order_id == "order-async"
    assert "task_id" not in vars(events(caplog, "test.after_async")[-1])


def test_dispatch_live_mode_before_model_call(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    sent: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda req: sent.append(req) or response(payload={}))
    outcome = dispatch_text(
        session_factory,
        text="unused",
        client_event_key="live-route-failure",
        registry=Registry(),
        settings=settings(),
        client_factory=lambda configured, book: LiveLLMClient(
            configured, book, transport=transport
        ),
    )
    assert outcome.error_code == "CAPABILITY_MISSING"
    for name in ("dispatch.start", "dispatch.finish"):
        assert events(caplog, name)[-1].mode == "LIVE"
    assert events(caplog, "dispatch.finish")[-1].outcome == "failed"
    assert not events(caplog, "model.call") and not sent
    with session_factory() as session:
        assert session.scalar(select(ModelUsage)) is None


def test_dispatch_live_budget_refused_before_transport(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    sent: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda req: sent.append(req) or response(payload={}))
    outcome = dispatch_text(
        session_factory,
        text=TEXT_BLACK_TO_WHITE,
        client_event_key="live-budget-refusal",
        settings=settings(live_budget_usd=Decimal("0")),
        client_factory=lambda configured, book: LiveLLMClient(
            configured, book, transport=transport
        ),
    )
    assert outcome.error_code == "BUDGET_EXCEEDED" and not sent
    for name in ("dispatch.start", "budget.refused", "dispatch.finish"):
        record = events(caplog, name)[-1]
        assert record.mode == "LIVE" and record.task_id == outcome.task_id
    assert events(caplog, "dispatch.finish")[-1].outcome == "failed"
    with session_factory() as session:
        assert session.scalar(select(ModelUsage)) is None


def test_direct_budget_events_omit_mode(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    book: BudgetLedger = ledger(session_factory)
    reservation = book.reserve(
        provider="openai", model="offline", input_tokens=100, max_output_tokens=10
    )
    book.settle(reservation, input_tokens=80, output_tokens=5)
    with pytest.raises(BudgetExceeded):
        ledger(session_factory, settings(live_budget_usd=Decimal("0"))).reserve(
            provider="openai", model="offline", input_tokens=100, max_output_tokens=10
        )
    for name in ("budget.reserve", "budget.settle", "budget.refused"):
        assert "mode" not in vars(events(caplog, name)[-1])


def test_task_lifecycle_events_omit_inherited_mode(
    session_factory: sessionmaker[Session], database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    load_seed(session_factory, database_url=database_url)
    plan = dispatch_text(
        session_factory,
        text=TEXT_BLACK_TO_WHITE,
        client_event_key="lifecycle",
        settings=Settings(_env_file=None),
        now=NOW,
    )
    assert plan.status == "VALIDATED"
    # A persisted usage row is not a reliable source for the task's execution mode.
    ledger(session_factory).reserve(
        provider="openai",
        model="offline",
        task_id=plan.task_id,
        input_tokens=100,
        max_output_tokens=10,
    )
    with session_factory() as session:
        assert "mode" not in task_log_fields(session, plan.task_id)
    with log_context(mode="LIVE"):
        approval = approve_plan(
            session_factory,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            expected_intent_revision=plan.intent_revision,
            now=NOW,
        )
        commit_plan(
            session_factory,
            plan_id=plan.plan_id,
            approval_id=approval.approval_id,
            plan_hash=plan.plan_hash,
            idempotency_key="lifecycle",
            now=NOW,
        )
        assert publish_pending(session_factory) == 1
        with session_factory() as session:
            delivery = session.scalar(select(OutboxDelivery))
            assert not consume(session_factory, delivery.event_id, delivery.consumer)
        log_event(logger, "test.after_lifecycle")
    for name in (
        "approval.granted",
        "commit.applied",
        "outbox.delivered",
        "outbox.duplicate_skipped",
    ):
        record = events(caplog, name)[-1]
        assert record.task_id == plan.task_id and "mode" not in vars(record)
    assert events(caplog, "test.after_lifecycle")[-1].mode == "LIVE"


def durable_request(web):
    _, plan = fixture_plan(web)
    approval = approve_plan(
        web[1],
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
        now=NOW,
    )
    return dict(
        plan_id=plan.plan_id,
        approval_id=approval.approval_id,
        plan_hash=plan.plan_hash,
        idempotency_key="durable-observability",
        now=NOW,
    )


def test_metrics_endpoint_after_worker_round_and_commit_replay(web, caplog):
    client = web[0]
    initial = client.get("/metrics")
    assert initial.status_code == 200
    assert initial.headers["content-type"] == "application/json"
    assert len(initial.json()) == 12 and set(initial.json().values()) == {0}
    _, plan, _ = committed(web)
    counters = client.get("/metrics").json()
    assert counters == {
        **initial.json(),
        "tasks_enqueued": 1,
        "tasks_succeeded": 1,
        "approvals_granted": 1,
        "commits_applied": 1,
        "worker_claims": 1,
        "model_calls": 1,
    }
    approval_id = events(caplog, "approval.granted")[-1].approval_id
    replay = client.post(
        f"/plans/{plan.plan_id}/commit",
        json={
            "approval_id": approval_id,
            "plan_hash": plan.plan_hash,
            "idempotency_key": "observability-commit",
        },
    )
    assert replay.status_code == 200
    assert events(caplog, "commit.applied")[-1].replayed is True
    assert client.get("/metrics").json() == {**counters, "commits_replayed": 1}
    lease = json.loads(JSONFormatter().format(events(caplog, "lease.acquired")[0]))
    assert lease["task_id"] == plan.task_id and lease["attempt_id"]
    assert lease["outcome"] == "success" and lease["duration_ms"] >= 0
    assert len(events(caplog, "task.finished")) == 1


def test_durable_commit_success_replay_and_lost_ack(web, caplog):
    request = durable_request(web)

    def lose_response(_):
        raise ResponseLost("PRIVATE_LOST_ACK")

    with pytest.raises(ResponseLost, match="PRIVATE_LOST_ACK"):
        durable_commit(web[1], fault=lose_response, **request)
    failure = events(caplog, "recovery.durable_commit")[-1]
    assert failure.outcome == "failed" and failure.replayed is False
    assert failure.error_code == "INTERNAL_ERROR" and failure.task_id
    assert snapshot()["tasks_succeeded"] == 0
    original = durable_commit(web[1], **request)
    assert original.replayed
    success = json.loads(JSONFormatter().format(events(caplog, "recovery.durable_commit")[-1]))
    assert success["outcome"] == "success" and success["replayed"] is True
    assert success["task_id"] == original.task_id and success["attempt_id"]
    assert success["order_id"] == original.order_id and success["duration_ms"] >= 0
    assert [record.outcome for record in events(caplog, "recovery.durable_commit")] == [
        "started",
        "failed",
        "started",
        "success",
    ]
    assert snapshot()["tasks_succeeded"] == 1
    assert snapshot()["commits_applied"] == snapshot()["commits_replayed"] == 1
    assert "PRIVATE_LOST_ACK" not in captured(caplog)


@pytest.mark.parametrize("retry_outcome", ["cleared", "still_pending", "error"])
def test_verification_retry_event_and_watchdog_counts(web, caplog, monkeypatch, retry_outcome):
    request = durable_request(web)
    token = kernel_service.verify_failpoint.set(True)
    try:
        original = durable_commit(web[1], **request)
    finally:
        kernel_service.verify_failpoint.reset(token)
    if retry_outcome == "still_pending":
        monkeypatch.setattr(kernel_service, "consistency_problems", lambda _: ["PRIVATE_READBACK"])
    elif retry_outcome == "error":

        def fail(*_):
            raise AppError("PRIVATE_READBACK", code="READBACK_TEST")

        monkeypatch.setattr(kernel_service, "_post_verify", fail)
    assert scan(web[1], clock=lambda: NOW) == []
    record = json.loads(JSONFormatter().format(events(caplog, "verification.retry")[-1]))
    assert record["outcome"] == retry_outcome and record["operation_id"] == original.operation_id
    assert record["task_id"] == original.task_id and record["attempt_id"]
    assert record["order_id"] == original.order_id and record["duration_ms"] >= 0
    assert record["error_code"] == ("READBACK_TEST" if retry_outcome == "error" else None)
    start, finish = events(caplog, "watchdog.scan")
    assert start.outcome == "started" and finish.outcome == "success"
    assert finish.scanned_tasks == finish.recovered_tasks == finish.outbox_created == 0
    assert finish.pending_retries == 1 and finish.duration_ms >= 0
    counters = snapshot()
    assert counters["watchdog_scans"] == counters["verification_pending_retries"] == 1
    assert counters["verification_pending_cleared"] == (retry_outcome == "cleared")
    with web[1]() as session:
        assert session.get(Operation, original.operation_id).committed_verification_pending is (
            retry_outcome != "cleared"
        )
    if retry_outcome == "cleared":
        assert retry_pending_verification(web[1]) == 0
        assert len(events(caplog, "verification.retry")) == 1
    assert "PRIVATE_READBACK" not in captured(caplog)
    assert TEXT_BLACK_TO_WHITE not in captured(caplog)


def test_lease_fence_rejection_watchdog_expiry_and_empty_scan(web, caplog):
    factory = web[1]
    _, task_id, _ = accept_input(
        factory, thread_id="lease", client_event_key="lease", text="PRIVATE_LEASE_INPUT"
    )
    claimed_task, fence = claim(factory, "test-owner", clock=lambda: NOW)
    assert claimed_task == task_id
    with factory() as session, pytest.raises(AppError) as error:
        check_fence(session, task_id, fence + 1, NOW)
    assert error.value.code == "STALE_ATTEMPT"
    rejection = events(caplog, "lease.expired_or_fenced")[-1]
    assert rejection.outcome == "rejected" and rejection.error_code == "STALE_ATTEMPT"
    assert rejection.task_id == task_id and rejection.attempt_id
    assert scan(factory, clock=lambda: NOW + timedelta(seconds=61)) == [task_id]
    expiry = json.loads(JSONFormatter().format(events(caplog, "lease.expired_or_fenced")[-1]))
    assert expiry["outcome"] == "expired" and expiry["reason"] == "lease_expired"
    finish = events(caplog, "watchdog.scan")[-1]
    assert finish.scanned_tasks == finish.recovered_tasks == 1 and finish.pending_retries == 0
    assert scan(factory, clock=lambda: NOW + timedelta(minutes=2)) == []
    assert snapshot()["watchdog_scans"] == 2 and snapshot()["worker_claims"] == 1
    assert "PRIVATE_LEASE_INPUT" not in captured(caplog)


def test_terminal_task_events_follow_commit_and_skip_rollback(web, caplog):
    factory = web[1]
    _, task_id, _ = accept_input(
        factory, thread_id="rollback", client_event_key="rollback", text="rollback"
    )
    with factory() as session:
        with pytest.raises(RuntimeError, match="rollback"):
            with begin_immediate(session):
                task = session.get(Task, task_id)
                transition(task, "FAILED")
                session.flush()
                assert not events(caplog, "task.finished")
                raise RuntimeError("rollback")
        assert snapshot()["tasks_failed"] == 0
        with begin_immediate(session):
            task = session.get(Task, task_id)
            transition(task, "FAILED")
            session.flush()
            task.error_code = "TEST_FAILURE"
    assert snapshot()["tasks_failed"] == 1
    terminal = events(caplog, "task.finished")
    assert len(terminal) == 1 and terminal[0].task_id == task_id
    assert terminal[0].status == "FAILED" and terminal[0].error_code == "TEST_FAILURE"
    # Updating an already terminal task must not count it a second time.
    with factory() as session, begin_immediate(session):
        session.get(Task, task_id).error_code = "TEST_UPDATED"
    assert snapshot()["tasks_failed"] == 1


def test_metrics_ignore_log_level_and_are_thread_safe(caplog):
    configure_logging(Settings(_env_file=None, log_level="WARNING"))

    def call(_):
        log_event(logger, "model.call", outcome="success")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(call, range(400)))
    assert not events(caplog, "model.call")
    result = snapshot()
    assert result["model_calls"] == 400
    result["model_calls"] = -1
    assert snapshot()["model_calls"] == 400
    reset_for_tests()
    assert set(snapshot().values()) == {0}


def test_watchdog_recovers_lost_ack_and_clears_pending(web, caplog):
    request = durable_request(web)

    def lose_response(_):
        raise ResponseLost("PRIVATE_LOST_RESPONSE")

    token = kernel_service.verify_failpoint.set(True)
    try:
        with pytest.raises(ResponseLost):
            durable_commit(web[1], fault=lose_response, **request)
    finally:
        kernel_service.verify_failpoint.reset(token)
    assert snapshot()["tasks_succeeded"] == 0
    changed = scan(web[1], clock=lambda: NOW)
    assert len(changed) == 1
    finish = events(caplog, "watchdog.scan")[-1]
    assert finish.scanned_tasks == finish.recovered_tasks == finish.pending_retries == 1
    assert events(caplog, "verification.retry")[-1].outcome == "cleared"
    assert snapshot()["tasks_succeeded"] == snapshot()["verification_pending_cleared"] == 1
    assert "PRIVATE_LOST_RESPONSE" not in captured(caplog)


def test_watchdog_failure_is_observed_and_counted(caplog):
    def unavailable():
        raise AppError("PRIVATE_DATABASE_FAILURE", code="TEST_DATABASE")

    with pytest.raises(AppError) as error:
        scan(unavailable)
    assert error.value.code == "TEST_DATABASE"
    start, failure = events(caplog, "watchdog.scan")
    assert start.outcome == "started" and failure.outcome == "failed"
    assert failure.error_code == "TEST_DATABASE" and failure.duration_ms >= 0
    assert snapshot()["watchdog_scans"] == 1
    assert "PRIVATE_DATABASE_FAILURE" not in captured(caplog)
