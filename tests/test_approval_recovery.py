"""Approval renewal and absent downstream dependencies retain commit safeguards."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from app.config import Settings
from app.db.models import Approval, Operation, PurchaseOrder, SalesOrder
from app.db.schema import init_db
from app.db.session import begin_immediate, make_engine, make_session_factory
from app.errors import AppError
from app.main import create_app
from app.services.kernel import approve_plan, commit_plan, plan_change, record_receipt
from app.services.seed import load_seed
from app.services.task_view import build_task_view
from fastapi.testclient import TestClient
from sqlalchemy import func, select

NOW = datetime(2026, 11, 3, tzinfo=UTC)


@pytest.fixture()
def recovery(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'recovery.db'}"
    engine = make_engine(url)
    init_db(engine)
    factory = make_session_factory(engine)
    load_seed(factory, database_url=url)
    clock = [NOW]
    monkeypatch.setattr("app.api.tasks.utcnow", lambda: clock[0])
    monkeypatch.setattr("app.services.kernel._now", lambda value: value or clock[0])
    app = create_app(Settings(_env_file=None, database_url=url))
    with TestClient(app) as client:
        yield factory, client, clock
    engine.dispose()


def plan(factory, *, supplement=False):
    if supplement:
        with factory() as session, begin_immediate(session):
            row = session.get(PurchaseOrder, "PUR-3002")
            row.received_qty = row.ordered_qty
            row.status = "RECEIVED"
        changes = [{"mode": "set_total", "sku_id": "C-BLACK-L", "qty": 400}]
    else:
        changes = [
            {
                "mode": "replace_remaining",
                "from_sku": "A-BLACK-M",
                "to_sku": "A-WHITE-M",
                "qty": 40,
            }
        ]
    return plan_change(
        factory,
        order_id="SO-1001",
        base_revision=1,
        intent_revision=1,
        changes=changes,
        client_event_key="recovery-plan",
        now=NOW,
    )


def approve(factory, outcome, now=NOW):
    return approve_plan(
        factory,
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
        now=now,
        ttl_seconds=60,
    )


def commit(factory, outcome, approval, now=NOW):
    return commit_plan(
        factory,
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        approval_id=approval.approval_id,
        idempotency_key=f"recovery-{approval.approval_id}",
        now=now,
    )


def view(factory, outcome, now=NOW):
    with factory() as session:
        return build_task_view(
            session, outcome.task_id, request_id="review", correlation_id="review", now=now
        )


def test_supplement_purchase_plan_is_approvable_and_commits(recovery):
    factory, client, _ = recovery
    outcome = plan(factory, supplement=True)
    assert any(row["before"] is None for row in outcome.verdicts)
    block = view(factory, outcome).approval
    assert block.approvable and block.disabled_reason is None
    assert client.get(f"/tasks/{outcome.task_id}/view").json()["approval"]["approvable"]
    result = commit(factory, outcome, approve(factory, outcome))
    with factory() as session:
        assert session.get(PurchaseOrder, "PUR-3002-S1").ordered_qty == 200
        assert not session.get(Operation, result.operation_id).committed_verification_pending


def test_supplement_id_collision_remains_stale(recovery):
    factory, _, _ = recovery
    outcome = plan(factory, supplement=True)
    approval = approve(factory, outcome)
    with factory() as session, begin_immediate(session):
        session.add(
            PurchaseOrder(
                id="PUR-3002-S1",
                supplier_id="SUP-01",
                production_order_id="MO-2002",
                material_id="FAB-BLACK",
                ordered_qty=1,
                received_qty=0,
                status="PLACED",
                change_state="PENDING_SUPPLIER",
                doc_revision=1,
            )
        )
    assert view(factory, outcome).approval.disabled_reason == "STALE"
    with pytest.raises(AppError) as error:
        commit(factory, outcome, approval)
    assert error.value.code == "STALE_DEPENDENCY"
    with factory() as session:
        assert session.get(SalesOrder, "SO-1001").current_revision == 1


@pytest.mark.parametrize("offset", [60, 61])
def test_expired_approval_renews_without_failed_commit_first(recovery, offset):
    factory, _, _ = recovery
    outcome = plan(factory)
    old = approve(factory, outcome)
    moment = NOW + timedelta(seconds=offset)
    renewed = approve(factory, outcome, moment)
    assert renewed.approval_id != old.approval_id
    assert renewed.expires_at == moment + timedelta(seconds=60)
    assert approve(factory, outcome, moment).approval_id == renewed.approval_id
    with factory() as session:
        assert session.get(Approval, old.approval_id).status == "EXPIRED"
        assert session.scalar(select(func.count()).select_from(Operation)) == 0
    with pytest.raises(AppError) as error:
        commit(factory, outcome, old, moment)
    assert error.value.code == "EXPIRED"
    assert commit(factory, outcome, renewed, moment).revision == 2


def test_unexpired_approval_is_idempotent(recovery):
    factory, _, _ = recovery
    outcome = plan(factory)
    first = approve(factory, outcome)
    assert approve(factory, outcome, NOW + timedelta(seconds=59)) == first


def test_concurrent_expired_approval_renewal_creates_one_active_approval(recovery):
    factory, _, _ = recovery
    outcome = plan(factory)
    old = approve(factory, outcome)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: approve(factory, outcome, old.expires_at), range(2)))
    assert results[0].approval_id == results[1].approval_id != old.approval_id
    with factory() as session:
        rows = list(session.scalars(select(Approval)))
        assert sorted(row.status for row in rows) == ["ACTIVE", "EXPIRED"]


def test_renewal_after_expired_commit_and_bad_hash(recovery):
    factory, _, _ = recovery
    outcome = plan(factory)
    old = approve(factory, outcome)
    with pytest.raises(AppError) as error:
        approve_plan(
            factory,
            plan_id=outcome.plan_id,
            plan_hash="sha256:wrong",
            expected_intent_revision=outcome.intent_revision,
            now=old.expires_at,
        )
    assert error.value.code == "HASH_MISMATCH"
    with factory() as session:
        assert session.get(Approval, old.approval_id).status == "ACTIVE"
    with pytest.raises(AppError) as error:
        commit(factory, outcome, old, old.expires_at)
    assert error.value.code == "EXPIRED"
    assert view(factory, outcome, old.expires_at).approval.disabled_reason == "EXPIRED"
    renewed = approve(factory, outcome, old.expires_at)
    assert renewed.approval_id != old.approval_id
    assert commit(factory, outcome, renewed, old.expires_at).revision == 2


def test_expired_approval_ui_requires_explicit_renewal(recovery):
    factory, client, clock = recovery
    outcome = plan(factory)
    old = approve(factory, outcome)
    clock[0] = old.expires_at
    block = client.get(f"/tasks/{outcome.task_id}/view").json()["approval"]
    assert not block["approvable"]
    assert block["disabled_reason"] == "EXPIRED"
    page = client.get(f"/ui/tasks/{outcome.task_id}").text
    assert 'id="renew"' in page and "重新批准当前计划" in page
    assert 'data-approval-id=""' in page
    assert 'id="commit" disabled' in page
    assert "已批准，可以提交生效。" not in page
    response = client.post(
        f"/plans/{outcome.plan_id}/approve",
        json={"plan_hash": outcome.plan_hash, "expected_intent_revision": outcome.intent_revision},
    )
    assert response.status_code == 200
    assert response.json()["approval_id"] != old.approval_id
    block = client.get(f"/tasks/{outcome.task_id}/view").json()["approval"]
    assert block["approvable"]
    assert 'id="renew"' not in client.get(f"/ui/tasks/{outcome.task_id}").text
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Operation)) == 0


@pytest.mark.parametrize("mutation", ["stock", "cancel", "intent", "revoked", "consumed"])
def test_expiration_does_not_hide_other_invalidations(recovery, mutation):
    factory, client, clock = recovery
    outcome = plan(factory)
    old = approve(factory, outcome)
    if mutation == "stock":
        record_receipt(factory, sku_id="A-WHITE-M", qty=1, now=NOW)
    elif mutation == "consumed":
        commit(factory, outcome, old)
    else:
        from app.db.models import Task

        with factory() as session, begin_immediate(session):
            if mutation == "cancel":
                session.get(Task, outcome.task_id).cancel_requested = True
            elif mutation == "intent":
                session.get(SalesOrder, "SO-1001").intent_revision += 1
            else:
                session.get(Approval, old.approval_id).status = "REVOKED"
    block = view(factory, outcome, old.expires_at).approval
    assert not block.approvable
    clock[0] = old.expires_at
    assert 'id="renew"' not in client.get(f"/ui/tasks/{outcome.task_id}").text
    assert block.disabled_reason == ("CONSUMED" if mutation == "consumed" else "STALE")
