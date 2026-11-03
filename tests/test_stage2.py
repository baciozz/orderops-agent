"""Durable acceptance tests use database restarts, an injected clock and crash hooks."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from app.adapters.fixture_model import FixtureLLMClient
from app.api.inputs import cancel_task
from app.api.inputs import router as inputs_router
from app.api.inputs import submit as submit_endpoint
from app.config import Settings
from app.db.base import Base
from app.db.models import (
    AuditRecord,
    ConsumerReceipt,
    DispatchStep,
    Event,
    InputPayload,
    JobAttempt,
    Operation,
    OutboxDelivery,
    Plan,
    SalesOrder,
    Task,
)
from app.db.schema import init_db
from app.db.session import begin_immediate, make_engine, make_session_factory
from app.errors import AppError
from app.main import create_app
from app.orchestration.fixtures import (
    TEXT_ALL_NAVY,
    TEXT_BAD_SCHEMA,
    TEXT_BLACK_TO_WHITE,
    TEXT_NEW_ORDER,
    TEXT_ONLY_UNCUT,
)
from app.orchestration.inbox import accept_input
from app.orchestration.outbox import consume, publish_pending
from app.orchestration.recovery import ResponseLost, durable_commit
from app.orchestration.state import aware, check_fence, guard, transition
from app.orchestration.watchdog import scan
from app.orchestration.worker import Limits, claim, heartbeat, run_once
from app.services.kernel import approve_plan, commit_plan, consistency_problems
from app.services.seed import load_seed
from schemas.api import InputSubmitRequest
from sqlalchemy import event, func, inspect, select
from starlette.requests import Request
from tests.test_stage1c import NOW


@pytest.fixture()
def durable(tmp_path):
    url = f"sqlite:///{tmp_path / 'durable.db'}"
    engine = make_engine(url)
    init_db(engine)
    factory = make_session_factory(engine)
    load_seed(factory, database_url=url)
    clock = [NOW]
    yield factory, lambda: clock[0], clock, url
    engine.dispose()


def submit(durable, text=TEXT_BLACK_TO_WHITE, key="one", **kwargs):
    factory, clock, _, _ = durable
    return accept_input(
        factory, thread_id="thread", client_event_key=key, text=text, clock=clock, **kwargs
    )


def planned(durable):
    submit(durable)
    outcome = run_once(durable[0], clock=durable[1])
    assert outcome.status == "AWAITING_APPROVAL"
    approval = approve_plan(
        durable[0],
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
        now=NOW,
    )
    return outcome, dict(
        plan_id=outcome.plan_id,
        approval_id=approval.approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="commit-one",
        now=NOW,
    )


def crash(_):
    raise ResponseLost("simulated process loss")


def test_R01_receipt_while_running_and_two_active(durable):
    factory, clock, _, url = durable
    first = submit(durable)
    assert claim(factory, "a", clock=clock)[0] == first[1]
    app = create_app(Settings(_env_file=None, database_url=url))
    request = Request({"type": "http", "app": app})
    request.state.request_id = "r01"
    request.state.correlation_id = "r01"
    response = submit_endpoint(
        InputSubmitRequest(thread_id="other", text=TEXT_NEW_ORDER, client_event_key="two"), request
    )
    route = next(route for route in inputs_router.routes if getattr(route, "path", "") == "/inputs")
    assert route.status_code == 202
    second = response.task_id
    assert claim(factory, "b", clock=clock)[0] == second
    submit(durable, key="third")
    assert claim(factory, "c", clock=clock) is None
    with factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(Task).where(Task.status == "RUNNING"))
            == 2
        )
        assert session.get(InputPayload, response.event_id)


def test_R02_delivery_identity_dedupes_before_revision(durable):
    first = submit(durable)
    result = run_once(durable[0], clock=durable[1])
    with durable[0]() as session:
        revision = session.get(SalesOrder, "SO-1001").intent_revision
    again = submit(durable)
    assert again[:2] == first[:2]
    assert run_once(durable[0], clock=durable[1]) is None
    with durable[0]() as session:
        assert session.get(SalesOrder, "SO-1001").intent_revision == revision
        assert session.scalar(select(func.count()).select_from(Task)) == 1
        assert session.scalar(select(func.count()).select_from(Plan)) == 1
    assert submit(durable, key="independent")[1] != result.task_id
    assert consume(durable[0], first[0], "ui", clock=durable[1])
    assert not consume(durable[0], first[0], "ui", clock=durable[1])


def test_R03_expired_takeover_rejects_stale_write(durable):
    factory, clock, moment, _ = durable
    submit(durable)
    with pytest.raises(ResponseLost):
        run_once(factory, clock=clock, fault=crash)
    with factory() as session:
        old = session.scalar(select(JobAttempt))
        task_id, token = old.task_id, old.fencing_token
    assert scan(factory, clock=clock) == []
    moment[0] += timedelta(seconds=61)
    assert scan(factory, clock=clock) == [task_id]
    new = claim(factory, "replacement", clock=clock)
    assert new == (task_id, token + 1)
    with factory() as session, pytest.raises(AppError, match="旧 attempt"):
        check_fence(session, task_id, token, clock())
    assert heartbeat(factory, *new, progress=True, clock=clock)


def test_R04_cancel_first_rejects_commit(durable):
    outcome, request = planned(durable)
    assert cancel_task(durable[0], outcome.task_id, outcome.intent_revision) == "CANCELLED"
    with pytest.raises(AppError) as exc:
        commit_plan(durable[0], **request)
    assert exc.value.code == "CANCELLED"
    with durable[0]() as session:
        assert session.get(SalesOrder, "SO-1001").current_revision == 1
        assert session.scalar(select(func.count()).select_from(Operation)) == 0
        assert consistency_problems(session) == []


def test_R05_commit_first_cannot_be_cancelled(durable):
    outcome, request = planned(durable)
    committed = commit_plan(durable[0], **request)
    assert cancel_task(durable[0], outcome.task_id, outcome.intent_revision) == "ALREADY_COMMITTED"
    with durable[0]() as session:
        assert session.get(SalesOrder, "SO-1001").current_revision == 2
        assert session.get(Task, outcome.task_id).result_ref == committed.operation_id
        assert consistency_problems(session) == []


def test_R06_lost_commit_response_reconciles_same_key(durable):
    outcome, request = planned(durable)
    transitions = []

    def record(task, value, old, _initiator):
        if task.id == outcome.task_id:
            transitions.append((old, value))

    def lost_ack(_):
        with durable[0]() as session:
            task = session.get(Task, outcome.task_id)
            assert task.status == "COMMITTING"
            assert session.get(Operation, task.result_ref) is not None
        crash("after_commit")

    event.listen(Task.status, "set", record)
    try:
        with pytest.raises(ResponseLost):
            durable_commit(durable[0], fault=lost_ack, **request)
        assert transitions == [
            ("AWAITING_APPROVAL", "COMMITTING"),
            ("COMMITTING", "RECONCILING"),
        ]
    finally:
        event.remove(Task.status, "set", record)
    with durable[0]() as session:
        assert session.get(Task, outcome.task_id).status == "RECONCILING"
        operation_id = session.scalar(select(Operation.id))
    engine = make_engine(durable[3])
    restarted = make_session_factory(engine)
    try:
        replay = durable_commit(restarted, **request)
        assert replay.replayed and replay.operation_id == operation_id
    finally:
        engine.dispose()
    assert scan(durable[0], clock=durable[1]) == []
    with durable[0]() as session:
        assert session.scalar(select(func.count()).select_from(Operation)) == 1
        assert session.get(Task, outcome.task_id).status == "SUCCEEDED"
        assert session.get(SalesOrder, "SO-1001").current_revision == 2
        assert consistency_problems(session) == []


def test_R07_outbox_restart_and_consumer_dedupe(durable):
    _, request = planned(durable)
    commit_plan(durable[0], **request)
    with pytest.raises(ResponseLost):
        publish_pending(durable[0], clock=durable[1], fault=crash)
    durable[2][0] += timedelta(seconds=3)
    engine = make_engine(durable[3])
    restarted = make_session_factory(engine)
    assert publish_pending(restarted, clock=durable[1]) == 1
    with restarted() as session:
        assert session.scalar(select(func.count()).select_from(ConsumerReceipt)) == 1
        row = session.scalar(select(OutboxDelivery))
        assert row.attempts == 2 and row.delivery_state == "DELIVERED"
        assert session.scalar(select(func.count()).select_from(Operation)) == 1
    engine.dispose()


def test_R08_clarification_survives_restart_and_answer_event(durable):
    factory, clock, _, url = durable
    initial = submit(durable, TEXT_ALL_NAVY)
    result = run_once(factory, clock=clock)
    assert result.status == "NEEDS_CLARIFICATION"
    engine = make_engine(url)
    restarted = make_session_factory(engine)
    assert scan(restarted, clock=clock) == []
    with restarted() as session:
        task = session.get(Task, initial[1])
        assert task.status == "NEEDS_CLARIFICATION"
        assert session.get(InputPayload, initial[0]).body["text"] == TEXT_ALL_NAVY
        attempt = session.scalar(select(JobAttempt))
        assert attempt.lease_until is None
        plan = session.scalar(select(Plan))
        assert plan.normalized_changes and plan.source_refs
    accept_input(
        restarted,
        thread_id="thread",
        client_event_key="answer",
        text=TEXT_ONLY_UNCUT,
        task_id=initial[1],
        clock=clock,
    )
    resumed = run_once(restarted, clock=clock)
    assert resumed.task_id == initial[1] and resumed.status == "AWAITING_APPROVAL"
    engine.dispose()


def test_R09_bounded_errors_and_watchdog_limits(durable):
    factory, clock, moment, _ = durable
    submit(durable, TEXT_BAD_SCHEMA)
    failed = run_once(factory, clock=clock)
    assert failed.status == "FAILED" and failed.error_code == "SCHEMA_INVALID"
    assert len(failed.steps) == 3
    task_id = submit(durable, key="stalled")[1]
    for _index in range(3):
        assert claim(factory, "worker", clock=clock)
        moment[0] += timedelta(seconds=61)
        scan(factory, clock=clock)
    with factory() as session:
        task = session.get(Task, task_id)
        assert task.status == "FAILED" and task.error_code == "ATTEMPT_LIMIT"
        assert session.scalar(select(func.count()).select_from(Operation)) == 0


def test_inbox_atomic_rollback_and_illegal_transition(durable):
    with pytest.raises(ResponseLost):
        submit(durable, fault=crash)
    with durable[0]() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 0
        assert session.scalar(select(func.count()).select_from(Task)) == 0
    task = Task(status="SUCCEEDED")
    with pytest.raises(AppError):
        transition(task, "RUNNING")


def test_R16_restart_reservations_and_movements_rebuild_balance(durable):
    _, request = planned(durable)
    commit_plan(durable[0], **request)
    engine = make_engine(durable[3])
    with make_session_factory(engine)() as session:
        assert consistency_problems(session) == []
        assert session.get(SalesOrder, "SO-1001").current_revision == 2
    engine.dispose()


def test_stale_worker_cannot_persist_dispatch_or_plan(durable):
    factory, clock, moment, _ = durable
    submit(durable)

    replacement = []

    def takeover(ctx):
        moment[0] += timedelta(seconds=61)
        assert scan(factory, clock=clock) == [ctx.task.id]
        replacement.append(claim(factory, "replacement", clock=clock))
        assert replacement[-1] == (ctx.task.id, ctx.fence + 1)

    with pytest.raises(AppError) as exc:
        run_once(factory, clock=clock, before_step=takeover)
    assert exc.value.code == "STALE_ATTEMPT"
    assert len(replacement) == 1
    assert guard.get() is None
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(DispatchStep)) == 0
        assert session.get(Task, replacement[0][0]).status == "RUNNING"
        assert session.scalar(select(func.count()).select_from(Plan)) == 0
        assert session.get(SalesOrder, "SO-1001").intent_revision == 1


def test_input_dedupe_does_not_invalidate_approval_again(durable):
    factory, clock, _, _ = durable
    outcome, request = planned(durable)
    first = submit(durable, key="new-intent", selected_order_id="SO-1001")
    with factory() as session:
        revision = session.get(SalesOrder, "SO-1001").intent_revision
        assert session.get(Task, outcome.task_id).status == "SUPERSEDED"
    assert submit(durable, key="new-intent", selected_order_id="SO-1001") == first
    with factory() as session:
        assert session.get(SalesOrder, "SO-1001").intent_revision == revision
    with pytest.raises(AppError) as exc:
        commit_plan(factory, **request)
    assert exc.value.code == "STALE_INTENT"
    result = run_once(factory, clock=clock)
    assert result.status == "AWAITING_APPROVAL"
    assert result.intent_revision == revision


def test_step_limit_and_no_progress_watchdog(durable):
    from app.orchestration.worker import Limits

    factory, clock, moment, _ = durable
    submit(durable)
    outcome = run_once(factory, clock=clock, limits=Limits(steps=1))
    assert outcome.status == "FAILED" and outcome.error_code == "STEP_LIMIT"
    task_id = submit(durable, key="no-progress")[1]
    claimed = claim(factory, "worker", clock=clock)
    for _ in range(3):
        moment[0] += timedelta(seconds=40)
        assert heartbeat(factory, *claimed, clock=clock)
    assert scan(factory, clock=clock) == [task_id]


def test_outbox_retry_and_atomic_consumer_effect(durable):
    factory, clock, moment, _ = durable
    _, request = planned(durable)
    commit_plan(factory, **request)
    calls = []

    def unavailable(event_id, consumer):
        calls.append(event_id)
        raise RuntimeError("temporary delivery failure")

    assert publish_pending(factory, unavailable, clock=clock) == 0
    assert publish_pending(factory, unavailable, clock=clock) == 0
    assert len(calls) == 1
    moment[0] += timedelta(seconds=3)
    assert publish_pending(factory, clock=clock) == 1
    event_id = calls[0]
    with pytest.raises(ResponseLost):
        consume(factory, event_id, "atomic", lambda session, event: crash(event), clock=clock)
    with factory() as session:
        assert session.get(ConsumerReceipt, (event_id, "atomic")) is None
    assert consume(factory, event_id, "atomic", clock=clock)


def test_migration_matches_durable_models(durable):
    factory = durable[0]
    engine = factory.kw["bind"]
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
    # Alembic autogenerate does not detect all CHECK constraint changes.
    for table in Base.metadata.sorted_tables:
        actual = {
            item["name"]: item["sqltext"] for item in inspector.get_check_constraints(table.name)
        }
        expected = {
            constraint.name: str(constraint.sqltext)
            for constraint in table.constraints
            if hasattr(constraint, "sqltext")
        }
        assert actual == expected, table.name
    init_db(factory.kw["bind"])


def test_worker_once_cli_processes_persisted_input(durable, monkeypatch):
    from app.cli import main

    task_id = submit(durable)[1]
    monkeypatch.setenv("OOS_DATABASE_URL", durable[3])
    assert main(["worker-once"]) == 0
    with durable[0]() as session:
        assert session.get(Task, task_id).status == "AWAITING_APPROVAL"


@pytest.mark.parametrize("missing_delivery", [False, True])
def test_restart_scan_repairs_and_publishes_success_notification(durable, missing_delivery):
    factory, clock, _, url = durable
    outcome, request = planned(durable)
    committed = commit_plan(factory, **request)
    with factory() as session, begin_immediate(session):
        row = session.scalar(select(OutboxDelivery))
        event_id, delivery_id = row.event_id, row.id
        if missing_delivery:
            session.delete(row)
    engine = make_engine(url)
    restarted = make_session_factory(engine)
    try:
        assert scan(restarted, clock=clock) == []
        assert scan(restarted, clock=clock) == []
        with restarted() as session:
            row = session.scalars(select(OutboxDelivery)).one()
            assert row.event_id == event_id and row.consumer == "ui"
            assert row.delivery_state == "PENDING" and row.attempts == 0
            if not missing_delivery:
                assert row.id == delivery_id
        assert publish_pending(restarted, clock=clock) == 1
        assert scan(restarted, clock=clock) == []
        assert publish_pending(restarted, clock=clock) == 0
        with restarted() as session:
            assert session.scalar(select(func.count()).select_from(ConsumerReceipt)) == 1
            assert session.scalar(select(func.count()).select_from(OutboxDelivery)) == 1
            assert session.scalar(select(func.count()).select_from(Operation)) == 1
            assert session.get(Task, outcome.task_id).result_ref == committed.operation_id
            assert session.get(SalesOrder, "SO-1001").current_revision == 2
            assert consistency_problems(session) == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("recovery_state", ["COMMITTING", "RECONCILING"])
def test_restart_scan_reconciles_commit_and_missing_outbox(durable, recovery_state):
    factory, clock, _, url = durable
    outcome, request = planned(durable)
    if recovery_state == "COMMITTING":
        commit_plan(factory, acknowledge=False, **request)
    else:
        with pytest.raises(ResponseLost):
            durable_commit(factory, fault=crash, **request)
    with factory() as session, begin_immediate(session):
        assert session.get(Task, outcome.task_id).status == recovery_state
        session.delete(session.scalar(select(OutboxDelivery)))
    engine = make_engine(url)
    restarted = make_session_factory(engine)
    try:
        assert scan(restarted, clock=clock) == [outcome.task_id]
        assert publish_pending(restarted, clock=clock) == 1
        replay = durable_commit(restarted, **request)
        assert replay.replayed
        with restarted() as session:
            assert session.get(Task, outcome.task_id).status == "SUCCEEDED"
            assert session.scalar(select(func.count()).select_from(Operation)) == 1
            assert session.get(SalesOrder, "SO-1001").current_revision == 2
    finally:
        engine.dispose()


def test_replayed_lost_ack_never_reopens_success(durable):
    outcome, request = planned(durable)
    first = durable_commit(durable[0], **request)
    with pytest.raises(ResponseLost):
        durable_commit(durable[0], fault=crash, **request)
    with durable[0]() as session:
        task = session.get(Task, outcome.task_id)
        assert task.status == "SUCCEEDED" and task.result_ref == first.operation_id
        with pytest.raises(AppError) as exc:
            transition(task, "RECONCILING")
        assert exc.value.code == "ILLEGAL_TRANSITION"


def test_watchdog_preserves_notification_retry_schedule(durable):
    factory, clock, moment, _ = durable
    _, request = planned(durable)
    commit_plan(factory, **request)

    def unavailable(*_):
        raise RuntimeError("offline")

    assert publish_pending(factory, unavailable, clock=clock) == 0
    assert scan(factory, clock=clock) == []
    assert publish_pending(factory, clock=clock) == 0
    moment[0] += timedelta(seconds=3)
    assert publish_pending(factory, clock=clock) == 1
    with factory() as session:
        row = session.scalars(select(OutboxDelivery)).one()
        assert row.attempts == 2 and row.delivery_state == "DELIVERED"


def test_claim_skips_exhausted_candidate(durable):
    factory, clock, moment, _ = durable
    ids = sorted(submit(durable, key=f"queued-{index}")[1] for index in range(2))
    assert claim(factory, "old", clock=clock) == (ids[0], 1)
    moment[0] += timedelta(seconds=61)
    assert scan(factory, clock=clock) == [ids[0]]
    assert claim(factory, "next", clock=clock, limits=Limits(attempts=1)) == (ids[1], 1)
    with factory() as session:
        exhausted = session.get(Task, ids[0])
        assert exhausted.status == "FAILED" and exhausted.error_code == "ATTEMPT_LIMIT"
        assert session.get(Task, ids[1]).status == "RUNNING"
        assert len(list(session.scalars(select(JobAttempt)))) == 2


@pytest.mark.parametrize(("active_limit", "expected"), [(0, 0), (1, 1), (2, 2), (10, 2)])
def test_concurrent_claims_respect_global_and_configured_capacity(durable, active_limit, expected):
    factory, clock, _, _ = durable
    for index in range(4):
        submit(durable, key=f"capacity-{index}")

    def take(index):
        return claim(factory, f"worker-{index}", clock=clock, limits=Limits(active=active_limit))

    with ThreadPoolExecutor(max_workers=4) as workers:
        claimed = [result for result in workers.map(take, range(4)) if result]
    assert len(claimed) == len(set(claimed)) == expected
    with factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(Task).where(Task.status == "RUNNING"))
            == expected
        )
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == expected


def test_dispatch_preserves_parser_dirty_updates_and_has_no_model_write_lock(durable, monkeypatch):
    factory, clock, _, _ = durable
    task_id = submit(durable)[1]
    observed = []
    structured_call = FixtureLLMClient.structured_call

    def model_call(client, request):
        # A separate writer can accept work during the model call. In particular,
        # the current worker's ambient fence must not guard unrelated transactions.
        observed.append(submit(durable, key="during-model")[1])
        return structured_call(client, request)

    def before_step(ctx):
        if ctx.spec.agent_id != "parser":
            assert ctx.task.order_id == "SO-1001"
            assert ctx.task.chain_id == "CH-SO-1001"
            with factory() as session:
                persisted = session.get(Task, task_id)
                assert persisted.order_id == ctx.task.order_id
                assert persisted.chain_id == ctx.task.chain_id
                assert persisted.intent_revision == ctx.task.intent_revision == 1

    monkeypatch.setattr(FixtureLLMClient, "structured_call", model_call)
    result = run_once(factory, clock=clock, before_step=before_step)
    assert observed and result.status == "AWAITING_APPROVAL"
    assert guard.get() is None
    with factory() as session:
        assert session.get(Task, observed[0]).status == "QUEUED"


def test_dispatch_preserves_dirty_fields_and_denied_tool_audit(durable):
    factory, clock, _, _ = durable
    task_id = submit(durable)[1]

    def denied(ctx):
        ctx.task.chain_id = "recorded-before-denial"
        ctx.call_tool("commit")

    result = run_once(factory, clock=clock, before_step=denied)
    assert result.status == "FAILED" and result.error_code == "TOOL_NOT_ALLOWED"
    with factory() as session:
        assert session.get(Task, task_id).chain_id == "recorded-before-denial"
        audit = session.scalars(
            select(AuditRecord).where(AuditRecord.action == "tool.denied")
        ).one()
        assert audit.object_id == task_id
        assert session.scalars(select(DispatchStep)).one().error_code == "TOOL_NOT_ALLOWED"


def test_dispatch_progress_heartbeats_extend_custom_lease(durable):
    factory, clock, moment, _ = durable
    task_id = submit(durable)[1]
    observed = []
    limits = Limits(lease_seconds=30)

    def advance(ctx):
        with factory() as session:
            _, attempt = check_fence(session, task_id, ctx.fence, clock())
            assert aware(attempt.lease_until) == clock() + timedelta(seconds=limits.lease_seconds)
            observed.append(aware(attempt.heartbeat_at))
        moment[0] += timedelta(seconds=20)
        assert scan(factory, clock=clock, limits=limits) == []
        with factory() as session:
            check_fence(session, task_id, ctx.fence, clock())

    result = run_once(factory, clock=clock, limits=limits, before_step=advance)
    assert result.status == "AWAITING_APPROVAL"
    assert len(observed) == 3 and moment[0] == NOW + timedelta(seconds=60)
    with factory() as session:
        attempt = session.scalar(select(JobAttempt))
        assert aware(attempt.heartbeat_at) == clock()
        assert attempt.lease_until is None
