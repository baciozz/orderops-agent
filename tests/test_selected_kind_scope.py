"""Original selected receipt is preserved when a parser proposes a new order."""

from datetime import UTC, datetime, timedelta

import app.orchestration.dispatch as dispatch
import pytest
from app.db.models import InputPayload, Task
from app.db.session import begin_immediate
from app.errors import AppError
from app.orchestration.inbox import accept_input
from app.orchestration.watchdog import scan
from app.orchestration.worker import run_once
from app.services.task_view import build_task_view

import test_late_bound_intent as support

sandbox = support.sandbox


def new_payload(order=None):
    data = support.payload(order)
    data.update(
        event_type="NEW_ORDER",
        customer_candidate="HD",
        line_changes=[],
        new_lines=[dict(sku_id="A-WHITE-M", qty=1)],
    )
    return data


def scope(state, task_id):
    task = next(row for row in state["task"] if row["id"] == task_id)
    return tuple(task[key] for key in ("order_id", "chain_id", "intent_revision"))


def assert_preserved(engine, factory, task_id, before):
    after = support.snapshot(engine)
    assert scope(after, task_id) == scope(before, task_id)
    for table in ("sales_order", "approval", "plan", "event"):
        assert after[table] == before[table]
    assert support.business(after) == support.business(before)
    with factory() as session:
        task = session.get(Task, task_id)
        assert task.status == "NEEDS_CLARIFICATION"
        body = session.get(InputPayload, task.last_event_id).body
        assert body["extraction"]["business_event"] is None
        assert body["extraction"]["new_lines"] == []
        assert "另行提交不选择已有订单" in body["questions"][0]
        view = build_task_view(
            session,
            task_id,
            request_id="synthetic",
            correlation_id="synthetic",
            now=datetime.now(UTC),
        )
        assert not view.approval.approvable
    assert support.snapshot(engine) == after


@pytest.mark.parametrize("target", [None, "SO-1002", "SO-1001"])
def test_live_selected_new_never_rebinds_or_creates(sandbox, target, monkeypatch):
    engine, factory, settings = sandbox
    _, task_id, _ = support.receive(factory, selected="SO-1002")
    before = support.snapshot(engine)
    copied = dispatch._copy_step_writes

    def check_clean(source, writer):
        assert not source.dirty
        copied(source, writer)

    monkeypatch.setattr(dispatch, "_copy_step_writes", check_clean)
    result = support.run(factory, settings, new_payload(target))
    assert result.status == "NEEDS_CLARIFICATION"
    assert_preserved(engine, factory, task_id, before)
    assert sorted(row["status"] for row in before["approval"]) == ["ACTIVE", "REVOKED"]


def test_fixture_selected_new_and_replayed_receipt(sandbox):
    from app.orchestration.fixtures import TEXT_NEW_ORDER

    engine, factory, settings = sandbox
    kwargs = dict(
        thread_id="fixture",
        client_event_key="fixture",
        text=TEXT_NEW_ORDER,
        selected_order_id="SO-1002",
    )
    event, task_id, _ = accept_input(factory, **kwargs)
    before = support.snapshot(engine)
    assert run_once(factory, settings=settings).status == "NEEDS_CLARIFICATION"
    assert_preserved(engine, factory, task_id, before)
    after = support.snapshot(engine)
    assert accept_input(factory, **kwargs)[:2] == (event, task_id)
    assert support.snapshot(engine) == after


@pytest.mark.parametrize("selected", [None, "SO-1002"])
def test_answers_use_original_selection_not_inherited_binding(sandbox, selected):
    engine, factory, settings = sandbox
    _, task_id, _ = support.receive(factory, selected=selected)
    # An originally unbound change acquires an inferred binding, not explicit selection.
    assert support.run(factory, settings, support.payload("SO-1002")).status == "AWAITING_APPROVAL"
    accept_input(
        factory,
        thread_id="answer",
        client_event_key="answer",
        text="synthetic new",
        task_id=task_id,
        selected_order_id="SO-1001",
    )
    before = support.snapshot(engine)
    result = support.run(factory, settings, new_payload())
    if selected:
        assert result.status == "NEEDS_CLARIFICATION"
        assert_preserved(engine, factory, task_id, before)
    else:
        assert result.status == "AWAITING_APPROVAL"
        assert scope(support.snapshot(engine), task_id)[0] is None


@pytest.mark.parametrize("partial", [False, True])
def test_unbound_new_ignores_mutable_task_binding(sandbox, partial):
    engine, factory, settings = sandbox
    _, task_id, _ = support.receive(factory)
    if partial:
        with factory() as session, begin_immediate(session):
            task = session.get(Task, task_id)
            task.order_id = "SO-1002"
            task.chain_id = "CH-SO-1002"
            task.intent_revision = 2
    before = support.snapshot(engine)
    assert support.run(factory, settings, new_payload()).status == "AWAITING_APPROVAL"
    after = support.snapshot(engine)
    assert scope(after, task_id)[0] is None
    assert before["approval"] == after["approval"]
    assert before["sales_order"] == after["sales_order"]


def test_latest_input_fence_still_rejects_selected_new(sandbox):
    engine, factory, settings = sandbox
    _, task_id, _ = support.receive(factory, selected="SO-1002")
    before = support.snapshot(engine)

    def replace():
        with factory() as session, begin_immediate(session):
            session.get(Task, task_id).last_event_id = None

    with pytest.raises(AppError, match="输入已更新"):
        support.run(factory, settings, new_payload(), callback=replace)
    after = support.snapshot(engine)
    for table in ("sales_order", "approval", "plan", "input_payload"):
        assert after[table] == before[table]
    assert scope(after, task_id) == scope(before, task_id)


def test_crash_acceptance_replay_preserves_selected_scope(sandbox, monkeypatch):
    engine, factory, settings = sandbox
    _, task_id, _ = support.receive(factory, selected="SO-1002")
    before = support.snapshot(engine)
    now = datetime.now(UTC)

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic acceptance crash")

    with monkeypatch.context() as patch:
        patch.setattr(dispatch, "_add_step", crash)
        with pytest.raises(RuntimeError, match="acceptance crash"):
            support.run(factory, settings, new_payload("SO-1001"), clock=lambda: now)
    after = support.snapshot(engine)
    for table in ("sales_order", "approval", "plan", "input_payload"):
        assert before[table] == after[table]
    later = now + timedelta(seconds=61)
    scan(factory, clock=lambda: later, settings=settings)
    assert (
        support.run(factory, settings, new_payload(), clock=lambda: later).status
        == "NEEDS_CLARIFICATION"
    )
    assert_preserved(engine, factory, task_id, before)


def test_expired_result_after_takeover_cannot_write(sandbox):
    engine, factory, settings = sandbox
    _, task_id, _ = support.receive(factory, selected="SO-1002")
    before = support.snapshot(engine)
    now = datetime.now(UTC)

    def takeover():
        later = now + timedelta(seconds=61)
        scan(factory, clock=lambda: later, settings=settings)
        assert (
            support.run(factory, settings, new_payload(), clock=lambda: later).status
            == "NEEDS_CLARIFICATION"
        )

    with pytest.raises(AppError):
        support.run(factory, settings, new_payload("SO-1001"), callback=takeover, clock=lambda: now)
    assert_preserved(engine, factory, task_id, before)
