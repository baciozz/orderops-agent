"""Wrong-order fixture output cannot move an acquired intent or approval."""

from datetime import UTC, datetime, timedelta

import app.orchestration.agents as agents
import app.orchestration.dispatch as dispatch
import pytest
from app.db.models import InputPayload, Task
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from app.orchestration.inbox import accept_input
from app.orchestration.watchdog import scan
from app.orchestration.worker import run_once

import test_late_bound_intent as support

sandbox = support.sandbox


def receive(factory):
    return accept_input(
        factory,
        thread_id="scope",
        client_event_key="scope",
        text=TEXT_BLACK_TO_WHITE,
        selected_order_id="SO-1002",
    )


def preserved(engine, factory, task_id, before):
    after = support.snapshot(engine)
    for table in ("sales_order", "approval", "plan", "event"):
        assert before[table] == after[table]
    assert support.business(before) == support.business(after)
    with factory() as session:
        task = session.get(Task, task_id)
        assert task.order_id == "SO-1002"
        assert task.intent_revision == 3
        assert task.status == "NEEDS_CLARIFICATION"
        assert (
            session.get(InputPayload, task.last_event_id).body["extraction"]["business_event"]
            is None
        )


def test_selected_fixture_change_does_not_rebind(sandbox):
    engine, factory, settings = sandbox
    _, task_id, _ = receive(factory)
    before = support.snapshot(engine)
    assert run_once(factory, settings=settings).status == "NEEDS_CLARIFICATION"
    preserved(engine, factory, task_id, before)
    accept_input(
        factory,
        thread_id="scope",
        client_event_key="answer",
        task_id=task_id,
        text=TEXT_BLACK_TO_WHITE,
        selected_order_id="SO-1001",
    )
    before = support.snapshot(engine)
    assert run_once(factory, settings=settings).status == "NEEDS_CLARIFICATION"
    # Answer retains the selected order and bumps only its intent.
    after = support.snapshot(engine)
    for table in ("sales_order", "approval", "plan", "event"):
        assert before[table] == after[table]
    with factory() as session:
        assert session.get(Task, task_id).order_id == "SO-1002"


def test_fenced_acceptance_rechecks_scope_even_if_parser_guard_is_bypassed(sandbox, monkeypatch):
    engine, factory, settings = sandbox
    _, task_id, _ = receive(factory)
    before = support.snapshot(engine)
    monkeypatch.setattr(agents, "_respect_selected_order", lambda ctx, extracted: extracted)
    result = run_once(factory, settings=settings)
    assert result.error_code == "ORDER_SCOPE"
    after = support.snapshot(engine)
    for table in ("sales_order", "approval", "plan", "event", "input_payload"):
        assert before[table] == after[table]
    assert support.business(before) == support.business(after)
    with factory() as session:
        assert session.get(Task, task_id).order_id == "SO-1002"


def test_wrong_order_crash_recovery(sandbox, monkeypatch):
    engine, factory, settings = sandbox
    _, task_id, _ = receive(factory)
    before = support.snapshot(engine)
    now = datetime.now(UTC)

    def crash(*args, **kwargs):
        raise RuntimeError("acceptance crash")

    with monkeypatch.context() as patch:
        patch.setattr(dispatch, "_add_step", crash)
        with pytest.raises(RuntimeError, match="acceptance crash"):
            run_once(factory, settings=settings, clock=lambda: now)
    later = now + timedelta(seconds=61)
    scan(factory, settings=settings, clock=lambda: later)
    assert run_once(factory, settings=settings, clock=lambda: later).status == "NEEDS_CLARIFICATION"
    preserved(engine, factory, task_id, before)


def test_expired_wrong_order_result_after_takeover(sandbox):
    from app.errors import AppError

    engine, factory, settings = sandbox
    _, task_id, _ = receive(factory)
    before = support.snapshot(engine)
    now = datetime.now(UTC)

    def takeover(ctx):
        later = now + timedelta(seconds=61)
        scan(factory, settings=settings, clock=lambda: later)
        assert (
            run_once(factory, settings=settings, clock=lambda: later).status
            == "NEEDS_CLARIFICATION"
        )

    with pytest.raises(AppError):
        run_once(factory, settings=settings, clock=lambda: now, before_step=takeover)
    preserved(engine, factory, task_id, before)


def test_inferred_order_answer_cannot_transfer_acquired_intent(sandbox):
    engine, factory, settings = sandbox
    _, task_id, _ = support.receive(factory)
    assert support.run(factory, settings, support.payload("SO-1002")).status == "AWAITING_APPROVAL"
    accept_input(
        factory,
        thread_id="answer",
        client_event_key="answer",
        task_id=task_id,
        text=TEXT_BLACK_TO_WHITE,
    )
    before = support.snapshot(engine)
    assert run_once(factory, settings=settings).status == "NEEDS_CLARIFICATION"
    after = support.snapshot(engine)
    for table in ("sales_order", "approval", "plan", "event"):
        assert before[table] == after[table]
    with factory() as session:
        assert session.get(Task, task_id).order_id == "SO-1002"
