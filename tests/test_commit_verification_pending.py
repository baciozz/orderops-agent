"""A durable commit must remain pending until its readback verification succeeds."""

from datetime import UTC, datetime

import pytest
from app.config import Settings
from app.db.models import AuditRecord, Event, Operation, OutboxDelivery, SalesOrder, Task
from app.main import create_app
from app.orchestration.watchdog import scan
from app.services import kernel as kernel_service
from app.services.kernel import approve_plan, commit_plan, plan_change
from app.services.seed import load_seed
from fastapi.testclient import TestClient
from sqlalchemy import func, select

NOW = datetime(2026, 11, 3, tzinfo=UTC)


class SimulatedCrash(Exception):
    """Simulate process exit after the commit transaction, before readback."""


@pytest.fixture()
def kernel(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    return session_factory


@pytest.fixture()
def client(kernel, database_url, monkeypatch):
    app = create_app(Settings(_env_file=None, database_url=database_url))
    monkeypatch.setattr("app.api.tasks.utcnow", lambda: NOW)
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def approved_commit(kernel):
    plan = plan_change(
        kernel,
        order_id="SO-1001",
        base_revision=1,
        intent_revision=1,
        changes=[
            {
                "mode": "replace_remaining",
                "from_sku": "A-BLACK-M",
                "to_sku": "A-WHITE-M",
                "qty": 40,
            }
        ],
        client_event_key="verification-plan",
        now=NOW,
    )
    approval = approve_plan(
        kernel,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
        now=NOW,
    )
    return plan.task_id, {
        "plan_id": plan.plan_id,
        "approval_id": approval.approval_id,
        "plan_hash": plan.plan_hash,
        "idempotency_key": "verification-commit",
        "now": NOW,
        "acknowledge": True,
    }


def _crash_before_readback(kernel, approved_commit, monkeypatch):
    task_id, request = approved_commit

    def crash(_factory, _operation_id):
        raise SimulatedCrash("process exited before readback")

    # _post_verify runs after the transaction commits and before acknowledgement.
    # Raising here leaves durable writes and COMMITTING, just like process exit.
    monkeypatch.setattr(kernel_service, "_post_verify", crash)
    with pytest.raises(SimulatedCrash, match="process exited before readback"):
        commit_plan(kernel, **request)

    with kernel() as session:
        operation = session.scalars(select(Operation)).one()
        task = session.get(Task, task_id)
        assert operation.status == "COMMITTED"
        assert task.status == "COMMITTING"
        assert task.result_ref == operation.id
        assert session.get(SalesOrder, "SO-1001").current_revision == 2
        return operation.id


def _row_counts(kernel):
    with kernel() as session:
        return {
            model.__name__: session.scalar(select(func.count()).select_from(model))
            for model in (Operation, Event, OutboxDelivery, AuditRecord)
        }


def _verification(client, task_id):
    response = client.get(f"/tasks/{task_id}/view")
    assert response.status_code == 200
    return response.json()["result"]["verification"]


def test_crash_before_readback_reports_pending(kernel, client, approved_commit, monkeypatch):
    operation_id = _crash_before_readback(kernel, approved_commit, monkeypatch)
    verification = _verification(client, approved_commit[0])
    with kernel() as session:
        operation = session.get(Operation, operation_id)
        assert (operation.committed_verification_pending, verification) == (True, "pending")


def test_watchdog_after_crash_keeps_pending_without_recommit(
    kernel, client, approved_commit, monkeypatch
):
    operation_id = _crash_before_readback(kernel, approved_commit, monkeypatch)
    before = _row_counts(kernel)
    task_id = approved_commit[0]

    # Keep the crash hook installed: watchdog must recover without readback.
    assert scan(kernel, clock=lambda: NOW) == [task_id]
    assert _row_counts(kernel) == before
    with kernel() as session:
        task = session.get(Task, task_id)
        assert task.status == "SUCCEEDED"
        assert task.result_ref == operation_id
        assert session.get(SalesOrder, "SO-1001").current_revision == 2
    assert _verification(client, task_id) == "pending"


def test_idempotent_replay_after_crash_reports_pending(
    kernel, client, approved_commit, monkeypatch
):
    with monkeypatch.context() as patch:
        operation_id = _crash_before_readback(kernel, approved_commit, patch)
    # Restore the real verifier before replaying the exact same idempotency key.
    before = _row_counts(kernel)
    task_id, request = approved_commit
    outcome = commit_plan(kernel, **request)

    assert outcome.replayed is True
    assert outcome.operation_id == operation_id
    assert _row_counts(kernel) == before
    with kernel() as session:
        task = session.get(Task, task_id)
        assert task.status == "SUCCEEDED"
        assert task.result_ref == operation_id
        assert session.get(SalesOrder, "SO-1001").current_revision == 2
    assert _verification(client, task_id) == "pending"


def test_normal_commit_reports_passed(kernel, client, approved_commit):
    task_id, request = approved_commit
    outcome = commit_plan(kernel, **request)
    with kernel() as session:
        operation = session.get(Operation, outcome.operation_id)
        assert operation.committed_verification_pending is False
        assert session.get(Task, task_id).status == "SUCCEEDED"
    assert _verification(client, task_id) == "passed"


def test_verification_failure_reports_pending(kernel, client, approved_commit):
    task_id, request = approved_commit
    token = kernel_service.verify_failpoint.set(True)
    try:
        outcome = commit_plan(kernel, **request)
    finally:
        kernel_service.verify_failpoint.reset(token)

    with kernel() as session:
        operation = session.get(Operation, outcome.operation_id)
        assert operation.committed_verification_pending is True
        assert session.get(Task, task_id).status == "SUCCEEDED"
    assert _verification(client, task_id) == "pending"
