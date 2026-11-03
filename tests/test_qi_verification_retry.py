"""Retry durable commit readback without repeating any business writes."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from app.db.base import Base
from app.db.models import Operation, SalesOrder, Task
from app.orchestration.recovery import durable_commit
from app.orchestration.watchdog import scan
from app.services import kernel as kernel_service
from app.services.kernel import CommitOutcome, approve_plan, commit_plan, plan_change
from app.services.seed import load_seed
from sqlalchemy import func, select

NOW = datetime(2026, 11, 3, tzinfo=UTC)


class SimulatedCrash(Exception):
    """Exit after the business transaction commits, before readback starts."""


@pytest.fixture()
def kernel(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    return session_factory


def _approved_request(kernel, key="retry"):
    with kernel() as session:
        order = session.get(SalesOrder, "SO-1001")
        assert order is not None
        base_revision, intent_revision = order.current_revision, order.intent_revision
    plan = plan_change(
        kernel,
        order_id="SO-1001",
        base_revision=base_revision,
        intent_revision=intent_revision,
        changes=[
            {
                "mode": "replace_remaining",
                "from_sku": "A-BLACK-M",
                "to_sku": "A-WHITE-M",
                "qty": 10,
            }
        ],
        client_event_key=f"{key}-plan",
        now=NOW,
    )
    assert plan.status == "VALIDATED"
    approval = approve_plan(
        kernel,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
        now=NOW,
    )
    # Omit acknowledge: durable_commit supplies acknowledge=False itself.
    return dict(
        plan_id=plan.plan_id,
        approval_id=approval.approval_id,
        plan_hash=plan.plan_hash,
        idempotency_key=f"{key}-commit",
        now=NOW,
    )


def _pending_commit(kernel, request, monkeypatch, origin) -> CommitOutcome:
    if origin == "crash":

        def crash(_factory, _operation_id):
            raise SimulatedCrash("process exited before readback")

        with monkeypatch.context() as patch:
            patch.setattr(kernel_service, "_post_verify", crash)
            with pytest.raises(SimulatedCrash, match="process exited before readback"):
                commit_plan(kernel, **request)
        with kernel() as session:
            operation = session.scalars(
                select(Operation).where(Operation.idempotency_key == request["idempotency_key"])
            ).one()
            # The crash prevents a return; the durable result is the original outcome.
            original = CommitOutcome(operation_id=operation.id, **operation.result_json)
    else:
        assert origin == "failure"
        token = kernel_service.verify_failpoint.set(True)
        try:
            original = commit_plan(kernel, **request)
        finally:
            kernel_service.verify_failpoint.reset(token)
    assert original.replayed is False
    with kernel() as session:
        operation = session.get(Operation, original.operation_id)
        task = session.get(Task, original.task_id)
        assert operation is not None and task is not None
        assert operation.status == "COMMITTED"
        assert operation.committed_verification_pending is True
        assert task.status == ("COMMITTING" if origin == "crash" else "SUCCEEDED")
        assert task.result_ref == original.operation_id
    return original


def _snapshot(kernel):
    with kernel() as session:
        # Cover every table, including all business, audit, event and delivery rows.
        counts = {
            table.name: session.scalar(select(func.count()).select_from(table))
            for table in Base.metadata.sorted_tables
        }
        revisions = list(
            session.execute(
                select(SalesOrder.id, SalesOrder.current_revision).order_by(SalesOrder.id)
            )
        )
        return counts, revisions


def _assert_operation(kernel, original, *, pending):
    with kernel() as session:
        operation = session.get(Operation, original.operation_id)
        task = session.get(Task, original.task_id)
        assert operation is not None and task is not None
        assert operation.status == "COMMITTED"
        assert operation.committed_verification_pending is pending
        assert CommitOutcome(operation_id=operation.id, **operation.result_json) == original
        assert task.status == "SUCCEEDED"
        assert task.result_ref == original.operation_id


def _verification_spies(monkeypatch, *, still_failing=False):
    # Spy on the real verifier's checks so either watchdog import style is supported.
    consistency = Mock(wraps=kernel_service.consistency_problems)
    downstream = Mock(wraps=kernel_service.downstream_problems)
    if still_failing:
        consistency.side_effect = RuntimeError("合成读回核验失败")
    monkeypatch.setattr(kernel_service, "consistency_problems", consistency)
    monkeypatch.setattr(kernel_service, "downstream_problems", downstream)
    return consistency, downstream


@pytest.mark.parametrize("origin", ["crash", "failure"])
@pytest.mark.parametrize("still_failing", [False, True], ids=["passed", "pending"])
def test_watchdog_retries_pending_verification_without_recommit(
    kernel, monkeypatch, origin, still_failing
):
    request = _approved_request(kernel)
    original = _pending_commit(kernel, request, monkeypatch, origin)
    assert original.revision == 2
    before = _snapshot(kernel)
    consistency, downstream = _verification_spies(monkeypatch, still_failing=still_failing)

    for run in range(2):
        # Only task recovery belongs in scan's return value, never operation IDs.
        expected_tasks = [original.task_id] if origin == "crash" and run == 0 else []
        assert scan(kernel, clock=lambda: NOW) == expected_tasks
        assert _snapshot(kernel) == before
        _assert_operation(kernel, original, pending=still_failing)
        # Pending must cause a real attempt on each scan; passed must be skipped.
        expected_checks = run + 1 if still_failing else 1
        assert consistency.call_count == expected_checks
        assert downstream.call_count == (0 if still_failing else 1)


@pytest.mark.parametrize("replay", [commit_plan, durable_commit], ids=["kernel", "durable"])
@pytest.mark.parametrize("origin", ["crash", "failure"])
@pytest.mark.parametrize("still_failing", [False, True], ids=["passed", "pending"])
def test_commit_replay_retries_pending_verification_without_recommit(
    kernel, monkeypatch, replay, origin, still_failing
):
    request = _approved_request(kernel)
    original = _pending_commit(kernel, request, monkeypatch, origin)
    assert original.revision == 2
    before = _snapshot(kernel)
    consistency, downstream = _verification_spies(monkeypatch, still_failing=still_failing)

    for run in range(2):
        outcome = replay(kernel, **request)
        assert outcome == replace(original, replayed=True)
        assert _snapshot(kernel) == before
        _assert_operation(kernel, original, pending=still_failing)
        expected_checks = run + 1 if still_failing else 1
        assert consistency.call_count == expected_checks
        assert downstream.call_count == (0 if still_failing else 1)


@pytest.mark.parametrize("retry", [scan, commit_plan, durable_commit])
def test_already_passed_operation_is_never_reverified(kernel, monkeypatch, retry):
    request = _approved_request(kernel)
    original = commit_plan(kernel, **request)
    _assert_operation(kernel, original, pending=False)
    before = _snapshot(kernel)
    consistency, downstream = _verification_spies(monkeypatch)

    for _ in range(2):
        if retry is scan:
            assert scan(kernel, clock=lambda: NOW) == []
        else:
            assert retry(kernel, **request) == replace(original, replayed=True)
        assert _snapshot(kernel) == before
        _assert_operation(kernel, original, pending=False)
        consistency.assert_not_called()
        downstream.assert_not_called()


def test_watchdog_verifies_all_pending_operations_even_when_tasks_succeeded(kernel, monkeypatch):
    first = _pending_commit(kernel, _approved_request(kernel, "first"), monkeypatch, "failure")
    second = _pending_commit(kernel, _approved_request(kernel, "second"), monkeypatch, "failure")
    passed = commit_plan(kernel, **_approved_request(kernel, "passed"))
    _assert_operation(kernel, passed, pending=False)
    before = _snapshot(kernel)
    consistency, downstream = _verification_spies(monkeypatch)

    for _ in range(2):
        assert scan(kernel, clock=lambda: NOW) == []
        assert _snapshot(kernel) == before
        for original in (first, second, passed):
            _assert_operation(kernel, original, pending=False)
        assert consistency.call_count == 2
        assert downstream.call_count == 2


@pytest.mark.parametrize("replay", [commit_plan, durable_commit], ids=["kernel", "durable"])
def test_replay_verifies_only_its_own_pending_operation(kernel, monkeypatch, replay):
    first_request = _approved_request(kernel, "first")
    first = _pending_commit(kernel, first_request, monkeypatch, "failure")
    second_request = _approved_request(kernel, "second")
    second = _pending_commit(kernel, second_request, monkeypatch, "failure")
    passed = commit_plan(kernel, **_approved_request(kernel, "passed"))
    before = _snapshot(kernel)
    consistency, downstream = _verification_spies(monkeypatch)

    for _ in range(2):
        # Replay an older revision while another pending operation exists on this order.
        assert replay(kernel, **first_request) == replace(first, replayed=True)
        assert _snapshot(kernel) == before
        _assert_operation(kernel, first, pending=False)
        _assert_operation(kernel, second, pending=True)
        _assert_operation(kernel, passed, pending=False)
        assert consistency.call_count == 1
        assert downstream.call_count == 1

    assert replay(kernel, **second_request) == replace(second, replayed=True)
    assert _snapshot(kernel) == before
    _assert_operation(kernel, second, pending=False)
    assert consistency.call_count == 2
    assert downstream.call_count == 2
