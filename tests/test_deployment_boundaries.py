"""Synthetic mode-crossing and privacy regressions; all model I/O is mocked."""

import io
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from multiprocessing import get_context
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from alembic import command
from app import cli
from app.adapters.live_model import LiveLLMClient
from app.api.inputs import receipt
from app.config import Settings
from app.db.models import JobAttempt, ModelUsage, Task
from app.db.schema import _migration_config, init_db, schema_ready
from app.db.session import begin_immediate, make_engine, make_session_factory
from app.errors import LiveStartRefused
from app.main import create_app
from app.observability import configure_logging
from app.orchestration import dispatch, watchdog, worker
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from app.orchestration.inbox import accept_input
from app.services.seed import load_seed
from app.services.sheet_order import ACTIVATION
from fastapi.testclient import TestClient
from pydantic import ValidationError
from schemas.api import InputSubmitRequest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request
from tests import test_compat_provider as compatible
from tests import test_stage4_live as live

no_network = live.no_network


@pytest.fixture()
def seeded(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    return session_factory


def input_request(factory, configured):
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    session_factory=factory,
                    settings=configured,
                )
            ),
        }
    )
    request.state.request_id = request.state.correlation_id = "synthetic"
    return request


def mock_client(calls):
    def build(configured, book):
        return LiveLLMClient(
            configured,
            book,
            transport=httpx.MockTransport(lambda request: calls.append(request) or live.response()),
            clock=lambda: live.NOW,
        )

    return build


def test_fixture_api_receipt_cannot_be_promoted_by_live_consumer(seeded):
    accepted = receipt(
        InputSubmitRequest(thread_id="mode", client_event_key="fixture", text=TEXT_BLACK_TO_WHITE),
        input_request(seeded, Settings(_env_file=None)),
    )
    calls = []
    outcome = worker.run_once(seeded, settings=live.settings(), client_factory=mock_client(calls))
    assert outcome.task_id == accepted.task_id and outcome.status == "AWAITING_APPROVAL"
    assert calls == []
    with seeded() as session:
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0


def test_worker_once_cli_matches_its_fixture_only_help(seeded, database_url, monkeypatch):
    accept_input(seeded, thread_id="cli", client_event_key="fixture", text=TEXT_BLACK_TO_WHITE)
    configured = live.settings(database_url=database_url)
    monkeypatch.setattr(cli, "get_settings", lambda: configured)
    monkeypatch.setattr(dispatch, "get_settings", lambda: configured)
    calls = []
    monkeypatch.setattr(dispatch, "LiveLLMClient", mock_client(calls))
    assert cli.main(["worker-once"]) == 0
    assert calls == []
    with seeded() as session:
        assert session.scalar(select(Task.status)) == "AWAITING_APPROVAL"


def test_http_transport_logs_do_not_expose_urls_or_headers(session_factory, caplog):
    configured = compatible.configured(
        llm_base_url="https://mock.invalid/private-path", llm_api_key=compatible.MOCK_KEY
    )
    configure_logging(configured)
    caplog.set_level(logging.DEBUG)

    def respond(request):
        # Simulate httpcore's debug trace using its actual pinned logger namespace.
        logging.getLogger("httpcore.http11").debug("headers %r", [(b"cookie", b"PRIVATE_HEADER")])
        return httpx.Response(200, json=compatible.response_data())

    LiveLLMClient(
        configured,
        compatible.ledger(session_factory, configured),
        transport=httpx.MockTransport(respond),
    ).structured_call(compatible.request())
    assert "/private-path" not in caplog.text and "PRIVATE_HEADER" not in caplog.text


def test_config_validation_exception_hides_raw_environment_values():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, worker_max_active="PRIVATE_CONFIG_VALUE")
    assert "PRIVATE_CONFIG_VALUE" not in str(error.value)


def test_lifespan_failure_does_not_echo_exception_payload(seeded, database_url, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE_STARTUP_PAYLOAD")

    monkeypatch.setattr("app.main.reconcile_reservations", broken)
    with (
        pytest.raises(RuntimeError) as error,
        TestClient(
            create_app(Settings(_env_file=None, database_url=database_url)),
            base_url="http://127.0.0.1",
        ),
    ):
        pytest.fail("Startup must fail")
    assert "PRIVATE_STARTUP_PAYLOAD" not in str(error.value)


def test_configured_credential_cannot_be_embedded_in_endpoint_path(session_factory):
    from app.errors import LiveStartRefused

    configured = compatible.configured(
        llm_base_url="https://mock.invalid/" + compatible.MOCK_KEY, llm_api_key=compatible.MOCK_KEY
    )
    with pytest.raises(LiveStartRefused):
        LiveLLMClient(
            configured,
            compatible.ledger(session_factory, configured),
            transport=httpx.MockTransport(lambda _: pytest.fail("No transport")),
        )


@pytest.mark.parametrize("original", ["FIXTURE", "LIVE"])
def test_intake_replay_and_answers_preserve_mode(seeded, original):
    other = "LIVE" if original == "FIXTURE" else "FIXTURE"
    payload = dict(thread_id="stable", client_event_key="one", text=TEXT_BLACK_TO_WHITE)
    first = accept_input(seeded, **payload, runtime_mode=original)
    assert accept_input(seeded, **payload, runtime_mode=other) == first
    with seeded() as session, begin_immediate(session):
        session.get(Task, first[1]).status = "NEEDS_CLARIFICATION"
    answer = accept_input(
        seeded,
        thread_id="stable",
        client_event_key="answer",
        text=TEXT_BLACK_TO_WHITE,
        task_id=first[1],
        runtime_mode=other,
    )
    assert answer[1] == first[1]
    with seeded() as session:
        assert session.get(Task, first[1]).runtime_mode == original
    calls = []
    result = worker.run_once(seeded, settings=live.settings(), client_factory=mock_client(calls))
    assert result.status == "AWAITING_APPROVAL"
    assert len(calls) == (1 if original == "LIVE" else 0)


def test_fixture_worker_skips_live_without_downgrading_or_leasing(seeded):
    _, live_id, _ = accept_input(
        seeded,
        thread_id="live",
        client_event_key="one",
        text=TEXT_BLACK_TO_WHITE,
        runtime_mode="LIVE",
    )
    assert worker.run_once(seeded, settings=Settings(_env_file=None)) is None
    with seeded() as session:
        task = session.get(Task, live_id)
        assert (task.status, task.runtime_mode) == ("QUEUED", "LIVE")
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 0
    _, fixture_id, _ = accept_input(
        seeded,
        thread_id="fixture",
        client_event_key="two",
        text=TEXT_BLACK_TO_WHITE,
    )
    assert worker.run_once(seeded, settings=Settings(_env_file=None)).task_id == fixture_id


def test_incomplete_config_at_intake_cannot_later_be_promoted(seeded):
    accepted = receipt(
        InputSubmitRequest(thread_id="fallback", client_event_key="one", text=TEXT_BLACK_TO_WHITE),
        input_request(seeded, live.settings(price_in_per_mtok=None)),
    )
    calls = []
    worker.run_once(seeded, settings=live.settings(), client_factory=mock_client(calls))
    with seeded() as session:
        assert session.get(Task, accepted.task_id).runtime_mode == "FIXTURE"
    assert calls == []


@pytest.mark.parametrize("entry", ["demo", "api", "sheet"])
def test_http_intake_uses_server_policy_and_demo_pins_fixture(seeded, database_url, entry):
    with TestClient(
        create_app(live.settings(database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        body = dict(thread_id="http-mode", client_event_key="one", text=TEXT_BLACK_TO_WHITE)
        if entry == "demo":
            result = client.post(
                "/ui/demo/submit",
                data={**body, "selected_order_id": "SO-1001"},
                follow_redirects=False,
            )
            assert result.status_code == 303
            task_id = result.headers["location"].rsplit("/", 1)[-1]
        else:
            if entry == "sheet":
                body["text"] = ACTIVATION
            result = client.post("/inputs", json=body)
            assert result.status_code == 202
            task_id = result.json()["task_id"]
        assert client.get(f"/tasks/{task_id}").json()["runtime_mode"] == (
            "LIVE" if entry == "api" else "FIXTURE"
        )
        override = client.post("/inputs", json={**body, "runtime_mode": "LIVE"})
        assert override.status_code == 400
        assert override.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("original", ["FIXTURE", "LIVE"])
def test_sync_replay_uses_original_mode_and_cached_result(seeded, original):
    calls = []
    configured = live.settings() if original == "LIVE" else Settings(_env_file=None)
    first = dispatch.dispatch_text(
        seeded,
        text=TEXT_BLACK_TO_WHITE,
        client_event_key="sync-mode",
        settings=configured,
        client_factory=mock_client(calls),
    )
    second = dispatch.dispatch_text(
        seeded,
        text=TEXT_BLACK_TO_WHITE,
        client_event_key="sync-mode",
        settings=Settings(_env_file=None) if original == "LIVE" else live.settings(),
        client_factory=mock_client(calls),
    )
    assert first == second
    assert len(calls) == (1 if original == "LIVE" else 0)
    with seeded() as session:
        assert session.get(Task, first.task_id).runtime_mode == original


@pytest.mark.parametrize("mode", ["FIXTURE", "LIVE"])
def test_expired_attempt_recovery_keeps_persisted_mode(seeded, mode):
    _, task_id, _ = accept_input(
        seeded,
        thread_id="recover",
        client_event_key="one",
        text=TEXT_BLACK_TO_WHITE,
        runtime_mode=mode,
        clock=lambda: live.NOW,
    )
    assert worker.claim(seeded, "crashed", allow_live=True, clock=lambda: live.NOW) == (task_id, 1)
    later = live.NOW + timedelta(seconds=61)
    assert watchdog.scan(seeded, clock=lambda: later) == [task_id]
    calls = []
    outcome = worker.run_once(
        seeded,
        settings=live.settings(),
        clock=lambda: later,
        client_factory=mock_client(calls),
    )
    assert outcome.task_id == task_id and outcome.status == "AWAITING_APPROVAL"
    assert len(calls) == (1 if mode == "LIVE" else 0)
    with seeded() as session:
        task = session.get(Task, task_id)
        assert task.runtime_mode == mode
        assert session.scalar(select(func.max(JobAttempt.fencing_token))) == 2


def test_simultaneous_consumers_cannot_promote_or_duplicate_fixture(seeded, database_url):
    _, task_id, _ = accept_input(
        seeded,
        thread_id="race",
        client_event_key="one",
        text=TEXT_BLACK_TO_WHITE,
    )
    barrier = Barrier(2)
    calls = []

    def consume(configured):
        engine = make_engine(database_url)
        try:
            barrier.wait(timeout=5)
            return worker.run_once(
                make_session_factory(engine), settings=configured, client_factory=mock_client(calls)
            )
        finally:
            engine.dispose()

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(consume, s) for s in (live.settings(), Settings(_env_file=None))]
        outcomes = [future.result(timeout=20) for future in futures]
    assert [outcome.task_id for outcome in outcomes if outcome] == [task_id]
    assert calls == []
    with seeded() as session:
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 1


def _child_consume_fixture(database_url):
    # A fresh interpreter and engine demonstrate that this is not an in-memory flag.
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")

    socket.socket.connect = socket.socket.connect_ex = forbidden
    socket.create_connection = socket.getaddrinfo = forbidden
    engine = make_engine(database_url)
    try:
        result = worker.run_once(
            make_session_factory(engine), settings=live.settings(), client_factory=forbidden
        )
        assert result.status == "AWAITING_APPROVAL"
    finally:
        engine.dispose()


def test_persisted_fixture_survives_fresh_process(seeded, database_url):
    _, task_id, _ = accept_input(
        seeded,
        thread_id="process",
        client_event_key="one",
        text=TEXT_BLACK_TO_WHITE,
    )
    process = get_context("spawn").Process(target=_child_consume_fixture, args=(database_url,))
    process.start()
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail("child did not complete")
    assert process.exitcode == 0
    with seeded() as session:
        assert session.get(Task, task_id).runtime_mode == "FIXTURE"
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0


def test_mode_migration_retains_evidence_defaults_and_constraints(seeded, engine):
    task_ids = []
    for state in ("QUEUED", "RUNNING", "NEEDS_CLARIFICATION", "SUCCEEDED"):
        _, task_id, _ = accept_input(
            seeded,
            thread_id=state,
            client_event_key=state,
            text=TEXT_BLACK_TO_WHITE,
            runtime_mode="LIVE",
        )
        with seeded() as session, begin_immediate(session):
            session.get(Task, task_id).status = state
        task_ids.append(task_id)
    # Historical usage remains history; it must not authorize another call after migration.
    LiveLLMClient(
        live.settings(),
        live.ledger(seeded),
        transport=httpx.MockTransport(lambda _: live.response()),
    ).structured_call(live.request(task_id=task_ids[-1]))
    cfg = _migration_config()
    tables = ("input_payload", "event", "inbox_delivery", "model_usage")
    with engine.connect() as connection:
        before = {
            name: connection.exec_driver_sql(f'SELECT * FROM "{name}"').all() for name in tables
        }
        task_before = connection.exec_driver_sql("SELECT id, status, last_event_id FROM task").all()
        connection.rollback()
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, "sync_dispatch_receipts")
        # Simulate a pre-migration writer: old schema has no runtime-mode column.
        connection.exec_driver_sql(
            "INSERT INTO task (id, chain_id, status, intent_revision, "
            "cancel_requested) "
            "VALUES ('legacy-writer', 'legacy', 'QUEUED', 0, 0)"
        )
        connection.commit()
        command.upgrade(cfg, "head")
        for name, rows in before.items():
            assert connection.exec_driver_sql(f'SELECT * FROM "{name}"').all() == rows
        assert (
            connection.exec_driver_sql(
                "SELECT id, status, last_event_id FROM task WHERE id != 'legacy-writer'"
            ).all()
            == task_before
        )
        assert connection.exec_driver_sql("SELECT DISTINCT runtime_mode FROM task").all() == [
            ("FIXTURE",)
        ]
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    init_db(engine)
    assert schema_ready(engine)
    for invalid in ("LIVE", "UNKNOWN", None):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text("UPDATE task SET runtime_mode=:mode WHERE id=:id"),
                dict(mode=invalid, id=task_ids[0]),
            )
    for invalid in ("UNKNOWN", None):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO task (id, chain_id, status, intent_revision, "
                    "cancel_requested, runtime_mode) "
                    "VALUES ('bad', 'bad', 'QUEUED', 0, 0, :mode)"
                ),
                dict(mode=invalid),
            )
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE task SET runtime_mode=runtime_mode")


@pytest.mark.parametrize(
    "stage", ["get_settings", "make_engine", "reconcile_reservations", "dispose"]
)
def test_startup_and_shutdown_formatted_tracebacks_are_sanitized(
    seeded, database_url, monkeypatch, stage
):
    def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE_FACTORY_PAYLOAD")

    if stage != "dispose":
        monkeypatch.setattr("app.main." + stage, broken)
    with pytest.raises(RuntimeError) as caught:
        application = create_app(
            None if stage == "get_settings" else Settings(_env_file=None, database_url=database_url)
        )
        if stage == "dispose":
            monkeypatch.setattr(application.state.engine, "dispose", broken)
        with TestClient(application, base_url="http://127.0.0.1"):
            pass
    assert "PRIVATE_FACTORY_PAYLOAD" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("stage", ["get_settings", "make_engine", "init_db", "load_seed"])
def test_cli_startup_failures_are_sanitized(database_url, monkeypatch, capsys, caplog, stage):
    def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE_CLI_PAYLOAD")

    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, database_url=database_url)
    )
    monkeypatch.setattr(cli, stage, broken)
    assert cli.main(["seed"]) == 1
    captured = capsys.readouterr()
    assert "PRIVATE_CLI_PAYLOAD" not in captured.out + captured.err + caplog.text
    assert "未自动重试" in captured.out


@pytest.mark.parametrize(
    "namespace",
    [
        "httpx",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.socks",
    ],
)
def test_http_logger_filters_cover_own_handlers_even_if_level_is_reset(namespace, caplog):
    root = logging.getLogger()
    original = (list(root.handlers), root.level)
    configure_logging(Settings(_env_file=None))
    configure_logging(Settings(_env_file=None))
    assert (list(root.handlers), root.level) == original
    logger = logging.getLogger(namespace)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.debug("PRIVATE_HTTP_DETAIL", exc_info=True)
        assert "PRIVATE_HTTP_DETAIL" not in stream.getvalue() + caplog.text
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.mark.parametrize("location", ["path", "host", "userinfo", "query", "fragment", "encoded"])
def test_credential_urls_refused_before_model_reservation(session_factory, location):
    key = compatible.MOCK_KEY
    urls = {
        "path": f"https://mock.invalid/{key}",
        "host": f"https://{key}.invalid/v1",
        "userinfo": f"https://{key}@mock.invalid/v1",
        "query": f"https://mock.invalid/v1?token={key}",
        "fragment": f"https://mock.invalid/v1#{key}",
        "encoded": "https://mock.invalid/" + "".join(f"%{ord(char):02x}" for char in key),
    }
    configured = compatible.configured(llm_base_url=urls[location], llm_api_key=key)
    with pytest.raises(LiveStartRefused) as caught:
        LiveLLMClient(
            configured,
            compatible.ledger(session_factory, configured),
            transport=httpx.MockTransport(lambda _: pytest.fail("No transport")),
        )
    assert key not in str(caught.value) and quote(key) not in str(caught.value)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0
