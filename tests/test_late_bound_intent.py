"""Synthetic LIVE conversion, fenced acquisition and pre-commit business immutability."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import app.orchestration.dispatch as dispatch
import pytest
from app.adapters.llm import StructuredCallResult
from app.config import Settings
from app.db.base import Base
from app.db.models import Approval, InputPayload, Plan, SalesOrder, Task
from app.db.schema import init_db
from app.db.session import begin_immediate, make_engine, make_session_factory
from app.errors import AppError
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE, golden_fixtures
from app.orchestration.inbox import accept_input
from app.orchestration.watchdog import scan
from app.orchestration.worker import run_once
from app.services.kernel import approve_plan, commit_plan, plan_change
from app.services.seed import load_seed
from schemas.common import UsageReport
from sqlalchemy import select


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'late-binding.sqlite'}"
    engine = make_engine(url)  # Production busy timeout, including concurrent workers.
    init_db(engine)
    factory = make_session_factory(engine)
    load_seed(factory, database_url=url)
    settings = Settings(_env_file=None, database_url=url, live_enabled=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("No network or live client construction")

    monkeypatch.setattr(dispatch, "LiveLLMClient", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    for order_id in ("SO-1001", "SO-1002"):
        with factory() as session:
            order = session.get(SalesOrder, order_id)
            revision, intent = order.current_revision, order.intent_revision
        plan = plan_change(
            factory,
            order_id=order_id,
            base_revision=revision,
            intent_revision=intent,
            changes=[{"mode": "increment", "sku_id": "A-WHITE-M", "qty": 1}],
            source_refs=[],
            client_event_key=f"prior-{order_id}",
        )
        approve_plan(
            factory,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            expected_intent_revision=plan.intent_revision,
        )
    yield engine, factory, settings
    engine.dispose()


def snapshot(engine):
    with engine.connect() as connection:
        return {
            table.name: [
                dict(row)
                for row in connection.execute(
                    table.select().order_by(*table.primary_key.columns)
                ).mappings()
            ]
            for table in Base.metadata.sorted_tables
        }


def business(snapshot):
    # All tables except the explicit control-plane/audit/transport set, not selected quantities.
    control = {
        "task",
        "event",
        "input_payload",
        "inbox_delivery",
        "job_attempt",
        "dispatch_step",
        "plan",
        "approval",
        "audit_record",
        "model_usage",
        "model_budget_reservation",
        "outbox_delivery",
    }
    return {
        key: [
            {k: v for k, v in row.items() if key != "sales_order" or k != "intent_revision"}
            for row in rows
        ]
        for key, rows in snapshot.items()
        if key not in control
    }


def payload(order_id="SO-1001"):
    return dict(
        order_candidates=[order_id] if order_id else [],
        customer_candidate=None,
        line_changes=[
            dict(mode="increment", sku_id="A-WHITE-M", from_sku=None, to_sku=None, qty=1)
        ],
        new_lines=[],
        missing_fields=[],
        conflicts=[],
        questions=[],
        event_type="CHANGE_ORDER",
        order_id=order_id,
        clarification_choice=None,
    )


class ControlledLive:
    mode = "LIVE"

    def __init__(self, output=None, callback=lambda: None):
        self.output = payload() if output is None else output
        self.callback = callback

    def structured_call(self, request):
        assert request.prompt_ref == "live://parser"
        self.callback()
        return StructuredCallResult(
            payload=self.output,
            usage=UsageReport(mode="FIXTURE", input_tokens=0, output_tokens=0, cost_usd="0"),
        )


def run(factory, settings, output=None, hook=lambda ctx: None, callback=lambda: None, **kwargs):
    def inject(ctx):
        ctx.llm = ControlledLive(output, callback)
        hook(ctx)

    return run_once(factory, settings=settings, before_step=inject, **kwargs)


def receive(factory, key="new", selected=None):
    return accept_input(
        factory,
        thread_id=key,
        client_event_key=key,
        text="Synthetic controlled change",
        selected_order_id=selected,
    )


def assert_intents(factory, target=3):
    with factory() as session:
        assert session.get(SalesOrder, "SO-1001").intent_revision == target
        assert session.get(SalesOrder, "SO-1002").intent_revision == 2


def test_unbound_acquires_once_revokes_only_target_and_preserves_business(sandbox):
    engine, factory, settings = sandbox
    before = snapshot(engine)
    event_id, task_id, _ = receive(factory)
    assert_intents(factory, 2)
    result = run(factory, settings)
    assert result.status == "AWAITING_APPROVAL"
    assert result.intent_revision == 3
    assert_intents(factory)
    with factory() as session:
        assert session.get(Plan, result.plan_id).intent_revision == 3
        receipt = session.get(InputPayload, event_id)
        assert receipt.body["extraction"]["intent_revision"] == 3
        assert receipt.body["late_bound_intent"] == {"order_id": "SO-1001", "intent_revision": 3}
        approvals = session.scalars(select(Approval).join(Plan).join(Task)).all()
        for approval in approvals:
            task = session.get(Task, session.get(Plan, approval.plan_id).task_id)
            assert approval.status == ("REVOKED" if task.order_id == "SO-1001" else "ACTIVE")
    assert receive(factory)[:2] == (event_id, task_id)
    assert run(factory, settings) is None
    assert_intents(factory)
    assert business(snapshot(engine)) == business(before)
    approval = approve_plan(
        factory,
        plan_id=result.plan_id,
        plan_hash=result.plan_hash,
        expected_intent_revision=result.intent_revision,
    )
    args = dict(
        plan_id=result.plan_id,
        plan_hash=result.plan_hash,
        approval_id=approval.approval_id,
        idempotency_key="synthetic-commit",
        verification_settings=settings,
    )
    committed = commit_plan(factory, **args)
    after_commit = business(snapshot(engine))
    replay = commit_plan(factory, **args)
    assert replay.operation_id == committed.operation_id
    assert business(snapshot(engine)) == after_commit
    assert len(snapshot(engine)["operation"]) == 1
    assert_intents(factory)


@pytest.mark.parametrize(
    "selected,output,target,status",
    [
        ("SO-1001", payload(), 3, "AWAITING_APPROVAL"),
        ("SO-1002", payload(), 2, "NEEDS_CLARIFICATION"),
        ("SO-1002", payload(None), 2, "NEEDS_CLARIFICATION"),
    ],
)
def test_selected_live_boundaries(sandbox, selected, output, target, status):
    engine, factory, settings = sandbox
    before = business(snapshot(engine))
    _, task_id, _ = receive(factory, selected=selected)
    result = run(factory, settings, output)
    assert result.status == status
    with factory() as session:
        assert session.get(Task, task_id).order_id == selected
        assert session.get(SalesOrder, "SO-1001").intent_revision == target
        assert session.get(SalesOrder, selected).intent_revision == 3
    assert business(snapshot(engine)) == before


def test_acceptance_failure_rolls_back_binding_revision_and_revocation(sandbox, monkeypatch):
    engine, factory, settings = sandbox
    event_id, task_id, _ = receive(factory)
    before = snapshot(engine)

    def crash(*args, **kwargs):
        raise RuntimeError("crash inside acceptance transaction")

    with monkeypatch.context() as patch:
        patch.setattr(dispatch, "_add_step", crash)
        with pytest.raises(RuntimeError, match="crash inside"):
            run(factory, settings)
    after = snapshot(engine)
    for table in ("sales_order", "approval", "plan", "input_payload"):
        assert before[table] == after[table]
    with factory() as session:
        assert session.get(Task, task_id).order_id is None
        assert "late_bound_intent" not in session.get(InputPayload, event_id).body
    assert business(after) == business(before)


def test_crash_retry_does_not_reacquire(sandbox):
    engine, factory, settings = sandbox
    receive(factory)
    before = business(snapshot(engine))
    now = datetime.now(UTC)

    def crash(ctx):
        if ctx.spec.agent_id == "inventory-check":
            raise RuntimeError("after durable parse")

    with pytest.raises(RuntimeError, match="after durable parse"):
        run(factory, settings, hook=crash, clock=lambda: now)
    assert_intents(factory)
    later = now + timedelta(seconds=61)
    scan(factory, clock=lambda: later, settings=settings)
    result = run(factory, settings, clock=lambda: later)
    assert result.status == "AWAITING_APPROVAL"
    assert result.intent_revision == 3
    assert_intents(factory)
    assert business(snapshot(engine)) == before


def test_stale_model_result_after_takeover_cannot_revoke_new_approval(sandbox):
    engine, factory, settings = sandbox
    receive(factory)
    now = datetime.now(UTC)
    before = business(snapshot(engine))

    def takeover():
        later = now + timedelta(seconds=61)
        scan(factory, clock=lambda: later, settings=settings)
        result = run(factory, settings, clock=lambda: later)
        approve_plan(
            factory,
            plan_id=result.plan_id,
            plan_hash=result.plan_hash,
            expected_intent_revision=result.intent_revision,
            now=later,
        )

    with pytest.raises(AppError, match="旧 attempt"):
        run(factory, settings, callback=takeover, clock=lambda: now)
    assert_intents(factory)
    with factory() as session:
        assert len(session.scalars(select(Approval).where(Approval.status == "ACTIVE")).all()) == 2
    assert business(snapshot(engine)) == before


def test_changed_latest_input_rejects_before_acquisition(sandbox):
    engine, factory, settings = sandbox
    _, task_id, _ = receive(factory)
    before = snapshot(engine)

    def replace_input():
        with factory() as session, begin_immediate(session):
            session.get(Task, task_id).last_event_id = None

    # Isolate the latest-input fence even if an invalid caller retained the lease token.
    with pytest.raises(AppError, match="输入已更新"):
        run(factory, settings, callback=replace_input)
    assert_intents(factory, 2)
    assert snapshot(engine)["approval"] == before["approval"]


def test_two_unbound_inputs_serialize_and_only_latest_plan_survives(sandbox):
    engine, factory, settings = sandbox
    receive(factory, key="one")
    receive(factory, key="two")
    before = business(snapshot(engine))
    parser_ready = Barrier(2)

    def hook(ctx):
        pass

    def worker():
        try:
            return run(factory, settings, hook=hook, callback=lambda: parser_ready.wait(timeout=10))
        except AppError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    with factory() as session:
        receipts = session.scalars(select(InputPayload)).all()
        latest_receipt = max(receipts, key=lambda row: row.body["receipt_ordinal"])
        latest_task = session.get(Task, latest_receipt.task_id)
        assert latest_task.status == "AWAITING_APPROVAL"
        current = session.get(SalesOrder, "SO-1001").intent_revision
        assert latest_task.intent_revision == current
        assert current in (3, 4)
        acquired = [
            row.body["late_bound_intent"]["intent_revision"]
            for row in receipts
            if "late_bound_intent" in row.body
        ]
        assert sorted(acquired) == list(range(3, current + 1))
    assert all(
        not isinstance(result, AppError) or result.code == "STALE_INTENT" for result in results
    )
    assert business(snapshot(engine)) == before


def test_retry_cannot_adopt_competing_intent(sandbox):
    engine, factory, settings = sandbox
    receive(factory)
    now = datetime.now(UTC)
    before = business(snapshot(engine))

    def crash(ctx):
        if ctx.spec.agent_id == "inventory-check":
            raise RuntimeError("after durable parse")

    with pytest.raises(RuntimeError):
        run(factory, settings, hook=crash, clock=lambda: now)
    with factory() as session:
        order = session.get(SalesOrder, "SO-1001")
        revision = order.current_revision
    competing = plan_change(
        factory,
        order_id="SO-1001",
        base_revision=revision,
        intent_revision=3,
        changes=[{"mode": "increment", "sku_id": "A-WHITE-M", "qty": 2}],
        source_refs=[],
        client_event_key="competing",
    )
    approval = approve_plan(
        factory,
        plan_id=competing.plan_id,
        plan_hash=competing.plan_hash,
        expected_intent_revision=competing.intent_revision,
    )
    later = now + timedelta(seconds=61)
    scan(factory, clock=lambda: later, settings=settings)
    with pytest.raises(AppError) as rejected:
        run(factory, settings, clock=lambda: later)
    assert rejected.value.code == "STALE_INTENT"
    assert_intents(factory, 4)
    with factory() as session:
        assert session.get(Approval, approval.approval_id).status == "ACTIVE"
    assert business(snapshot(engine)) == before


@pytest.mark.parametrize(
    "output",
    [
        {**payload(), "questions": ["Which quantity?"]},
        {**payload(), "conflicts": ["Conflicting quantity"]},
        {**payload(), "line_changes": "not an array"},
    ],
)
def test_unaccepted_parse_does_not_acquire(sandbox, output):
    engine, factory, settings = sandbox
    receive(factory)
    before = snapshot(engine)
    result = run(factory, settings, output)
    assert result.status in {"NEEDS_CLARIFICATION", "FAILED"}
    assert_intents(factory, 2)
    assert snapshot(engine)["approval"] == before["approval"]
    assert business(snapshot(engine)) == business(before)


def test_partial_binding_crash_then_valid_reparse_acquires(sandbox, monkeypatch):
    engine, factory, settings = sandbox
    event_id, task_id, _ = receive(factory)
    now = datetime.now(UTC)
    before = business(snapshot(engine))

    def crash(*args, **kwargs):
        raise RuntimeError("after partial parse persisted")

    with monkeypatch.context() as patch:
        patch.setattr(dispatch, "_fail", crash)
        with pytest.raises(RuntimeError, match="after partial parse"):
            partial = golden_fixtures()[TEXT_BLACK_TO_WHITE]
            partial["questions"] = ["Which quantity?"]
            run_once(
                factory,
                settings=settings,
                fixtures={"Synthetic controlled change": partial},
                clock=lambda: now,
            )
    with factory() as session:
        assert session.get(Task, task_id).order_id == "SO-1001"
        assert "late_bound_intent" not in session.get(InputPayload, event_id).body
    assert_intents(factory, 2)
    later = now + timedelta(seconds=61)
    scan(factory, clock=lambda: later, settings=settings)
    result = run(factory, settings, clock=lambda: later)
    assert result.status == "AWAITING_APPROVAL"
    assert result.intent_revision == 3
    assert_intents(factory)
    assert business(snapshot(engine)) == before


@pytest.mark.parametrize("intervening", ["selected", "direct"])
@pytest.mark.parametrize("clock_delta", [0, -60])
def test_older_unbound_cannot_revoke_newer_cross_task_approval(sandbox, intervening, clock_delta):
    engine, factory, settings = sandbox
    now = datetime.now(UTC)
    accept_input(
        factory, thread_id="older", client_event_key="older", text="controlled", clock=lambda: now
    )
    before = business(snapshot(engine))
    protected = []

    def newer_intent():
        later_clock = now + timedelta(seconds=clock_delta)
        if intervening == "selected":
            accept_input(
                factory,
                thread_id="newer",
                client_event_key="newer",
                text="controlled",
                selected_order_id="SO-1001",
                clock=lambda: later_clock,
            )
            result = run(factory, settings, clock=lambda: now)
        else:
            result = plan_change(
                factory,
                order_id="SO-1001",
                base_revision=1,
                intent_revision=2,
                changes=[{"mode": "increment", "sku_id": "A-WHITE-M", "qty": 1}],
                source_refs=[],
                client_event_key="newer-direct",
                now=later_clock,
            )
        approved = approve_plan(
            factory,
            plan_id=result.plan_id,
            plan_hash=result.plan_hash,
            expected_intent_revision=result.intent_revision,
            now=now,
        )
        protected.append(approved.approval_id)

    result = run(factory, settings, callback=newer_intent, clock=lambda: now)
    assert result.status == "FAILED"
    assert result.error_code == (
        "STALE_INPUT_ORDER" if intervening == "selected" else "INTENT_ORDER_UNPROVEN"
    )
    assert result.order_id is None
    with factory() as session:
        assert session.get(Approval, protected[0]).status == "ACTIVE"
        assert session.get(Task, result.task_id).error_code == result.error_code
    assert_intents(factory)
    assert business(snapshot(engine)) == before


@pytest.mark.parametrize("newer_finishes_first", [False, True])
def test_unbound_cross_task_completion_order(sandbox, newer_finishes_first):
    engine, factory, settings = sandbox
    _, old_task, _ = receive(factory, key="old")
    new_tasks = []
    newer_results = []
    before = business(snapshot(engine))

    def receive_newer():
        new_tasks.append(receive(factory, key="newer")[1])
        if newer_finishes_first:
            newer_results.append(run(factory, settings))

    older = run(factory, settings, callback=receive_newer)
    if not newer_finishes_first:
        newer_results.append(run(factory, settings))
    newer = newer_results[0]
    assert newer.task_id == new_tasks[0]
    assert newer.status == "AWAITING_APPROVAL"
    if newer_finishes_first:
        assert older.status == "FAILED"
        assert older.error_code == "STALE_INPUT_ORDER"
        assert_intents(factory, 3)
    else:
        assert older.status == "AWAITING_APPROVAL"
        assert_intents(factory, 4)
        with factory() as session:
            assert session.get(Task, old_task).status == "SUPERSEDED"
    assert business(snapshot(engine)) == before


def test_receipt_ordinals_are_atomic_clock_independent_and_replay_preserves_metadata(sandbox):
    _, factory, _ = sandbox
    barrier = Barrier(2)
    now = datetime.now(UTC)

    def submit(index):
        barrier.wait(timeout=10)
        return accept_input(
            factory,
            thread_id=str(index),
            client_event_key=str(index),
            text="controlled",
            clock=lambda: now,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(submit, (0, 1)))
    with factory() as session:
        bodies = {row.event_id: row.body for row in session.scalars(select(InputPayload))}
    assert sorted(body["receipt_ordinal"] for body in bodies.values()) == [1, 2]
    assert all(body["intent_watermark"]["SO-1001"] == 2 for body in bodies.values())
    for index, receipt in enumerate(receipts):
        with pytest.raises(AppError) as conflict:
            accept_input(
                factory,
                thread_id=str(index),
                client_event_key=str(index),
                text="different replay text",
                clock=lambda: now,
            )
        assert conflict.value.code == "INPUT_REQUEST_CONFLICT"
        assert (
            accept_input(
                factory,
                thread_id=str(index),
                client_event_key=str(index),
                text="controlled",
                clock=lambda: now,
            )[:2]
            == receipt[:2]
        )
    with factory() as session:
        assert {row.event_id: row.body for row in session.scalars(select(InputPayload))} == bodies


@pytest.mark.parametrize(
    "damage",
    [
        "legacy",
        "ordinal_bool",
        "ordinal_duplicate",
        "watermark_bool",
        "watermark_missing",
        "counter_reset",
    ],
)
def test_unprovable_metadata_fails_closed_without_revocation(sandbox, damage):
    engine, factory, settings = sandbox
    event_id, _, _ = receive(factory)
    if damage == "ordinal_duplicate":
        other_id, _, _ = receive(factory, key="duplicate")
        with factory() as session, begin_immediate(session):
            other = session.get(InputPayload, other_id)
            other.body = {**other.body, "receipt_ordinal": 1}
            # Keep the focus task deterministically claimable.
            session.get(Task, other.task_id).status = "CANCELLED"
    with factory() as session, begin_immediate(session):
        receipt = session.get(InputPayload, event_id)
        body = {**receipt.body, "test_damage": damage}
        if damage == "legacy":
            body.pop("receipt_ordinal")
            body.pop("intent_watermark")
        elif damage == "ordinal_bool":
            body["receipt_ordinal"] = True
        elif damage == "watermark_bool":
            body["intent_watermark"] = {"SO-1001": True}
        elif damage == "watermark_missing":
            body["intent_watermark"] = {}
        elif damage == "counter_reset":
            body["intent_watermark"] = {"SO-1001": 3}
        receipt.body = body
    before = snapshot(engine)
    result = run(factory, settings)
    assert result.status == "FAILED"
    assert result.error_code == "INTENT_ORDER_UNPROVEN"
    assert_intents(factory, 2)
    assert snapshot(engine)["approval"] == before["approval"]
    assert business(snapshot(engine)) == business(before)


def test_provider_cannot_supply_acquisition_metadata(sandbox):
    engine, factory, settings = sandbox
    event_id, _, _ = receive(factory)
    before = snapshot(engine)
    result = run(
        factory,
        settings,
        {
            **payload(),
            "late_bound_intent": {"order_id": "SO-1001", "intent_revision": 99},
            "receipt_ordinal": 999,
        },
    )
    assert result.status == "FAILED"
    with factory() as session:
        assert "late_bound_intent" not in session.get(InputPayload, event_id).body
        assert session.get(InputPayload, event_id).body["receipt_ordinal"] == 1
    assert snapshot(engine)["approval"] == before["approval"]
    assert_intents(factory, 2)


def test_chronology_failure_has_fixed_safe_operator_guidance(sandbox):
    from app.main import create_app
    from fastapi.testclient import TestClient

    _, factory, settings = sandbox
    event_id, task_id, _ = receive(factory)
    with factory() as session, begin_immediate(session):
        receipt = session.get(InputPayload, event_id)
        receipt.body = {
            key: value
            for key, value in receipt.body.items()
            if key not in {"receipt_ordinal", "intent_watermark"}
        }
    assert run(factory, settings).error_code == "INTENT_ORDER_UNPROVEN"
    assert scan(factory, settings=settings) == []
    assert run(factory, settings) is None
    with factory() as session, begin_immediate(session):
        session.get(Task, task_id).result_ref = "PRIVATE INTERNAL MESSAGE"
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        page = client.get(f"/ui/tasks/{task_id}")
        assert page.status_code == 200
        assert "无法可靠确定输入先后" in page.text
        assert "新的提交标识重新提交" in page.text
        assert "PRIVATE INTERNAL MESSAGE" not in page.text
        assert 'href="/ui/demo/submit"' in page.text
        detail = client.get(f"/tasks/{task_id}")
        assert detail.status_code == 200
        for internal in (
            "receipt_ordinal",
            "intent_watermark",
            "received_intent",
            "late_bound_intent",
        ):
            assert internal not in detail.text
            assert internal not in page.text


@pytest.mark.parametrize(
    "damage", ["missing_origin", "origin_ordinal_bool", "origin_equal", "duplicate_origin"]
)
def test_changed_intent_with_unprovable_origin_keeps_approval(sandbox, damage):
    engine, factory, settings = sandbox
    old_id, _, _ = receive(factory)
    protected = []

    def newer():
        new_id, _, _ = receive(factory, key="selected", selected="SO-1001")
        result = run(factory, settings)
        approved = approve_plan(
            factory,
            plan_id=result.plan_id,
            plan_hash=result.plan_hash,
            expected_intent_revision=result.intent_revision,
        )
        protected.append(approved.approval_id)
        with factory() as session, begin_immediate(session):
            receipt = session.get(InputPayload, new_id)
            body = {**receipt.body, "test_damage": damage}
            if damage == "missing_origin":
                body.pop("received_intent")
            elif damage == "origin_ordinal_bool":
                body["receipt_ordinal"] = True
            elif damage == "origin_equal":
                body["receipt_ordinal"] = session.get(InputPayload, old_id).body["receipt_ordinal"]
            else:
                # Contradictory duplicated acquisition metadata cannot become provenance.
                body["late_bound_intent"] = body["received_intent"]
            receipt.body = body

    result = run(factory, settings, callback=newer)
    assert result.status == "FAILED"
    assert result.error_code == "INTENT_ORDER_UNPROVEN"
    assert_intents(factory)
    with factory() as session:
        assert session.get(Approval, protected[0]).status == "ACTIVE"


def test_target_created_after_receipt_is_not_inferred_from_missing_watermark(sandbox):
    engine, factory, settings = sandbox
    receive(factory)
    with factory() as session, begin_immediate(session):
        customer_id = session.get(SalesOrder, "SO-1001").customer_id
        session.add(
            SalesOrder(
                id="SO-AFTER",
                customer_id=customer_id,
                current_revision=1,
                intent_revision=1,
                status="OPEN",
            )
        )
    before = snapshot(engine)
    controlled = golden_fixtures()[TEXT_BLACK_TO_WHITE]
    controlled["business_event"]["order_id"] = "SO-AFTER"
    result = run_once(
        factory, settings=settings, fixtures={"Synthetic controlled change": controlled}
    )
    assert result.error_code == "INTENT_ORDER_UNPROVEN"
    assert result.status == "FAILED"
    assert snapshot(engine)["sales_order"] == before["sales_order"]
    assert snapshot(engine)["approval"] == before["approval"]


@pytest.mark.parametrize(
    "marker",
    [
        None,
        [],
        {},
        {"order_id": "SO-1001"},
        {"order_id": "", "intent_revision": 3},
        {"order_id": "SO-1001", "intent_revision": True},
        {"order_id": "SO-1001", "intent_revision": 0},
    ],
)
def test_malformed_own_acquisition_marker_fails_with_safe_guidance(sandbox, marker):
    engine, factory, settings = sandbox
    event_id, _, _ = receive(factory)
    with factory() as session, begin_immediate(session):
        receipt = session.get(InputPayload, event_id)
        receipt.body = {**receipt.body, "late_bound_intent": marker}
    before = snapshot(engine)
    result = run(factory, settings)
    assert result.status == "FAILED"
    assert result.error_code == "INTENT_ORDER_UNPROVEN"
    assert snapshot(engine)["approval"] == before["approval"]
    assert snapshot(engine)["sales_order"] == before["sales_order"]
    assert business(snapshot(engine)) == business(before)
