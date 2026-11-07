"""A durable commit must remain pending until its readback verification succeeds."""

from datetime import UTC, datetime, timedelta

import pytest
from app.config import Settings
from app.db.base import Base
from app.db.models import AuditRecord, Event, Operation, OutboxDelivery, SalesOrder, Task
from app.main import create_app
from app.orchestration.watchdog import scan
from app.services import kernel as kernel_service
from app.services.kernel import (
    approve_plan,
    commit_plan,
    plan_change,
    retry_pending_verification,
)
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
    with TestClient(base_url="http://127.0.0.1", app=app) as client:
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

    # Keep the crash hook installed: a failed retry must not prevent task recovery.
    assert scan(kernel, clock=lambda: NOW) == [task_id]
    assert _row_counts(kernel) == before
    with kernel() as session:
        task = session.get(Task, task_id)
        assert task.status == "SUCCEEDED"
        assert task.result_ref == operation_id
        assert session.get(SalesOrder, "SO-1001").current_revision == 2
    assert _verification(client, task_id) == "pending"


def test_idempotent_replay_after_crash_reports_passed(kernel, client, approved_commit, monkeypatch):
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
    assert _verification(client, task_id) == "partial"


def test_normal_commit_reports_passed(kernel, client, approved_commit):
    task_id, request = approved_commit
    outcome = commit_plan(kernel, **request)
    with kernel() as session:
        operation = session.get(Operation, outcome.operation_id)
        assert operation.committed_verification_pending is False
        assert session.get(Task, task_id).status == "SUCCEEDED"
    assert _verification(client, task_id) == "partial"


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


def _contents_except_verification_flag(kernel):
    with kernel() as session:
        return {
            table.name: [
                dict(row)
                for row in session.execute(
                    select(
                        *(
                            col
                            for col in table.c
                            if col.name != "committed_verification_pending"
                            and not col.name.startswith("verification_")
                        )
                    ).order_by(*table.primary_key.columns)
                ).mappings()
            ]
            for table in Base.metadata.sorted_tables
        }


@pytest.mark.parametrize("raises", [False, True], ids=["problems", "exception"])
def test_slower_failed_verification_preserves_concurrent_pass(
    kernel, approved_commit, monkeypatch, raises
):
    token = kernel_service.verify_failpoint.set(True)
    try:
        original = commit_plan(kernel, **approved_commit[1])
    finally:
        kernel_service.verify_failpoint.reset(token)
    before = _contents_except_verification_flag(kernel)
    real_consistency = kernel_service.consistency_problems
    current = [NOW]
    config = Settings(_env_file=None, verification_lease_seconds=10)

    def slower_failure(session):
        # Keep a stale ORM instance in the reader while another retry passes.
        stale = session.get(Operation, original.operation_id)
        assert stale.committed_verification_pending is True
        with monkeypatch.context() as patch:
            patch.setattr(kernel_service, "consistency_problems", real_consistency)
            current[0] += timedelta(seconds=11)
            retry_pending_verification(
                kernel, original.operation_id, settings=config, clock=lambda: current[0]
            )
        assert stale.committed_verification_pending is True
        if raises:
            raise RuntimeError("合成回读异常")
        return ["合成回读核验失败"]

    monkeypatch.setattr(kernel_service, "consistency_problems", slower_failure)
    retry_pending_verification(
        kernel, original.operation_id, settings=config, clock=lambda: current[0]
    )
    with kernel() as session:
        assert session.get(Operation, original.operation_id).committed_verification_pending is False
    assert _contents_except_verification_flag(kernel) == before


@pytest.mark.parametrize("status", ["PENDING", "FAILED"])
def test_retry_skips_noncommitted_and_missing_operations(
    kernel, approved_commit, monkeypatch, status
):
    original = commit_plan(kernel, **approved_commit[1])
    with kernel() as session:
        operation = session.get(Operation, original.operation_id)
        operation.status = status
        operation.committed_verification_pending = True
        session.commit()
    before = _contents_except_verification_flag(kernel)

    def unexpected_verification(*args):
        pytest.fail("非已提交操作不应回读核验")

    monkeypatch.setattr(kernel_service, "_post_verify", unexpected_verification)
    retry_pending_verification(kernel)
    retry_pending_verification(kernel, original.operation_id)
    retry_pending_verification(kernel, "missing-operation")
    with kernel() as session:
        assert session.get(Operation, original.operation_id).committed_verification_pending is True
    assert _contents_except_verification_flag(kernel) == before


def test_manual_escalation_and_recovery_preserve_committed_receipt_and_business_data(
    kernel, client, approved_commit, monkeypatch
):
    from app.services.verification import retry_pending, verify_manually

    settings = Settings(_env_file=None, verification_max_retries=1)
    token = kernel_service.verify_failpoint.set(True)
    try:
        original = commit_plan(kernel, **approved_commit[1])
    finally:
        kernel_service.verify_failpoint.reset(token)
    before = _contents_except_verification_flag(kernel)
    with monkeypatch.context() as patch:
        patch.setattr(kernel_service, "_post_verify", lambda *_, **__: None)
        assert retry_pending(kernel, now=NOW, settings=settings) == 1
        result = client.get(f"/tasks/{original.task_id}/view").json()
        assert result["card"]["status"] == "SUCCEEDED"
        assert result["result"]["verification"] == "pending"
        assert result["result"]["verification_manual_required"] is True
        assert result["result"]["verification_retry_attempts"] == 1
        assert "自动核验已停止" in client.get(f"/ui/tasks/{original.task_id}").text
        assert verify_manually(kernel, original.operation_id, now=NOW, settings=settings) is False
        assert _contents_except_verification_flag(kernel) == before
    assert verify_manually(kernel, original.operation_id, now=NOW, settings=settings) is True
    assert _contents_except_verification_flag(kernel) == before
    result = client.get(f"/tasks/{original.task_id}/view").json()["result"]
    assert result["verification"] == "partial" and not result["verification_manual_required"]
    assert result["operation_id"] == original.operation_id


def test_pending_retry_timestamp_is_explicit_utc_in_api(
    kernel, client, approved_commit, monkeypatch
):
    from app.services.verification import retry_pending

    token = kernel_service.verify_failpoint.set(True)
    try:
        original = commit_plan(kernel, **approved_commit[1])
    finally:
        kernel_service.verify_failpoint.reset(token)
    monkeypatch.setattr(kernel_service, "_post_verify", lambda *_, **__: None)
    assert retry_pending(kernel, now=NOW) == 1
    payload = client.get(f"/tasks/{original.task_id}/view").json()["result"]
    timestamp = payload["verification_next_attempt_at"]
    assert datetime.fromisoformat(timestamp).utcoffset() == timedelta(0)
    assert datetime.fromisoformat(timestamp) == NOW + timedelta(seconds=30)


@pytest.mark.parametrize("route", ["worker", "replay"])
def test_web_honors_explicit_verification_settings(
    kernel, database_url, approved_commit, monkeypatch, route
):
    from unittest.mock import Mock

    token = kernel_service.verify_failpoint.set(True)
    try:
        original = commit_plan(kernel, **approved_commit[1])
    finally:
        kernel_service.verify_failpoint.reset(token)
    attempt = Mock()
    monkeypatch.setattr(kernel_service, "_post_verify", attempt)
    app = create_app(
        Settings(_env_file=None, database_url=database_url, verification_max_retries=0)
    )
    try:
        with TestClient(app, base_url="http://127.0.0.1") as configured_client:
            if route == "worker":
                response = configured_client.post("/ui/demo/worker-once")
            else:
                request = approved_commit[1]
                response = configured_client.post(
                    f"/plans/{request['plan_id']}/commit",
                    json={
                        key: request[key] for key in ("approval_id", "plan_hash", "idempotency_key")
                    },
                )
            assert response.status_code == 200
        with kernel() as session:
            operation = session.get(Operation, original.operation_id)
            assert operation.verification_manual_required is True
            assert operation.verification_retry_attempts == 0
            assert operation.committed_verification_pending is True
        attempt.assert_not_called()
    finally:
        app.state.engine.dispose()
