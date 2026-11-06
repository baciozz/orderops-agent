"""Missing current MTO demand must not hide active, possibly executed documents."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, get_ident

import pytest
from app.config import Settings
from app.db.base import Base
from app.db.models import DocRevision, OrderLine, ProductionOrder, PurchaseOrder, SalesOrder
from app.db.session import begin_immediate, make_engine, make_session_factory
from app.services import kernel as service
from app.services import verification
from sqlalchemy import select
from tests import test_commit_verification_pending as support
from tests import test_verification_result_fencing as fencing
from tests.verification_scale import NOW, add_orders

kernel = support.kernel
approved_commit = support.approved_commit
SKU = "C-BLACK-L"


@pytest.fixture()
def mto(kernel):
    add_orders(kernel, 1, "mto")
    with kernel() as session, begin_immediate(session):
        session.get(OrderLine, ("BR-0-2", SKU)).ordered_qty = 2
        session.get(ProductionOrder, "BM-0").planned_qty = 2
        revision = session.get(DocRevision, "BD-BM-0-3")
        revision.snapshot_json = {"sku_id": SKU, "planned_qty": 2}
        for i in range(2):
            session.get(PurchaseOrder, f"BP-0-{i}").ordered_qty = 2
            snapshot = session.get(DocRevision, f"BD-BP-0-{i}-3")
            snapshot.snapshot_json = {**snapshot.snapshot_json, "ordered_qty": 2}
    return kernel


def execution(factory, cut, received):
    service.record_production_cut(factory, doc_id="BM-0", cut_qty=cut, now=NOW)
    with factory() as session, begin_immediate(session):
        for i in range(2):
            row = session.get(PurchaseOrder, f"BP-0-{i}")
            row.received_qty = received
            row.status = (
                "RECEIVED" if received == 2 else "PARTIALLY_RECEIVED" if received else "PLACED"
            )


def remove_line(factory):
    with factory() as session, begin_immediate(session):
        session.delete(session.get(OrderLine, ("BR-0-2", SKU)))


def restore_line(factory):
    # Reverse only the injected fault, using the known current fixture, not history (999).
    with factory() as session, begin_immediate(session):
        session.add(
            OrderLine(revision_id="BR-0-2", sku_id=SKU, ordered_qty=2, fulfillment_mode="MTO")
        )


def facts(factory):
    """All non-operation tables; a verification attempt may only change its receipt."""
    with factory() as session:
        return {
            table.name: list(session.execute(select(table).order_by(*table.primary_key.columns)))
            for table in Base.metadata.sorted_tables
            if table.name != "operation"
        }


@pytest.mark.parametrize("cut", [0, 1, 2])
@pytest.mark.parametrize("received", [0, 1, 2])
def test_missing_current_line_reports_active_production_at_every_execution_stage(
    mto, cut, received
):
    execution(mto, cut, received)
    with mto() as session:
        assert service.consistency_problems(session) + service.downstream_problems(session) == []
    remove_line(mto)
    before = facts(mto)
    report = service.inspect_downstream(mto)
    assert len(report) == 1
    assert report[0]["doc_id"] == SKU
    assert report[0]["problem"] == f"BENCH-0/{SKU} 活跃生产计划 2 缺少当前 MTO 订单行"
    assert report[0]["repair"] == {
        "action": "manual_review",
        "order_id": "BENCH-0",
        "order_revision": 2,
        "sku_id": SKU,
        "production_order_ids": ["BM-0"],
    }
    assert facts(mto) == before


def test_stock_mode_cannot_hide_existing_production(mto):
    with mto() as session, begin_immediate(session):
        session.get(OrderLine, ("BR-0-2", SKU)).fulfillment_mode = "STOCK"
    assert service.inspect_downstream(mto)[0]["repair"]["action"] == "manual_review"


def test_split_executed_production_is_reported_once_with_both_documents(kernel):
    # Golden seed: 120 already cut + 180 uncut, both for the missing current SKU.
    with kernel() as session, begin_immediate(session):
        session.delete(session.get(OrderLine, ("SOR-1001-1", SKU)))
    report = service.inspect_downstream(kernel)
    assert len(report) == 1
    assert report[0]["repair"]["production_order_ids"] == ["MO-2001", "MO-2002"]
    assert "活跃生产计划 300" in report[0]["problem"]


def plan_cancel(factory):
    return service.plan_change(
        factory,
        order_id="BENCH-0",
        base_revision=2,
        intent_revision=2,
        changes=[{"mode": "cancel_remaining", "sku_id": SKU}],
        client_event_key="mto-cancel",
        now=NOW,
    )


@pytest.mark.parametrize(("cut", "received"), [(0, 0), (1, 0), (1, 1), (2, 0), (2, 2)])
def test_approved_cancellation_preserves_executed_demand_and_ignores_history(mto, cut, received):
    execution(mto, cut, received)
    before = facts(mto)
    plan = plan_cancel(mto)
    assert plan.status == "VALIDATED"
    approval = service.approve_plan(
        mto,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
        now=NOW,
    )
    # Plans and approval may change orchestration, never business facts.
    planned = facts(mto)
    for name in (
        "order_line",
        "production_order",
        "purchase_order",
        "doc_revision",
        "downstream_link",
    ):
        assert planned[name] == before[name]
    outcome = service.commit_plan(
        mto,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        approval_id=approval.approval_id,
        idempotency_key="mto-cancel-commit",
        now=NOW,
        acknowledge=True,
    )
    assert not fencing.operation(mto, outcome.operation_id)["committed_verification_pending"]
    with mto() as session:
        order = session.get(SalesOrder, "BENCH-0")
        assert order.current_revision == 3
        lines = service._current_order_lines(session)[order.id] if cut else {}
        if cut:
            assert lines[SKU].ordered_qty == cut
        else:
            assert order.id not in service._current_order_lines(session)
            assert session.get(ProductionOrder, "BM-0").status == "CANCELLED"
            assert all(
                session.get(PurchaseOrder, f"BP-0-{i}").status == "CANCELLED" for i in range(2)
            )
        assert session.get(OrderLine, ("BR-0-1", SKU)).ordered_qty == 999
        assert service.consistency_problems(session) + service.downstream_problems(session) == []


@pytest.mark.parametrize("received", [1, 2])
def test_cancellation_cannot_discard_received_materials(mto, received):
    execution(mto, 0, received)
    before = facts(mto)
    assert plan_cancel(mto).status == "NEEDS_CLARIFICATION"
    after = facts(mto)
    for name in (
        "order_line",
        "production_order",
        "purchase_order",
        "doc_revision",
        "downstream_link",
    ):
        assert after[name] == before[name]
    assert service.inspect_downstream(mto) == []


@pytest.mark.parametrize("planned", [0, 2])
def test_cancelled_documents_do_not_resurrect_old_demand(mto, planned):
    remove_line(mto)
    with mto() as session, begin_immediate(session):
        mo = session.get(ProductionOrder, "BM-0")
        mo.status, mo.planned_qty = "CANCELLED", planned
        session.get(DocRevision, "BD-BM-0-3").snapshot_json = {
            "sku_id": SKU,
            "planned_qty": planned,
            "status": "CANCELLED",
        }
        for i in range(2):
            session.get(PurchaseOrder, f"BP-0-{i}").status = "CANCELLED"
            snapshot = session.get(DocRevision, f"BD-BP-0-{i}-3")
            snapshot.snapshot_json = {**snapshot.snapshot_json, "status": "CANCELLED"}
    assert service.inspect_downstream(mto) == []


def test_cancelled_production_does_not_hide_active_purchase(mto):
    remove_line(mto)
    with mto() as session, begin_immediate(session):
        session.get(ProductionOrder, "BM-0").status = "CANCELLED"
        snapshot = session.get(DocRevision, "BD-BM-0-3")
        snapshot.snapshot_json = {**snapshot.snapshot_json, "status": "CANCELLED"}
    report = service.inspect_downstream(mto)
    assert len(report) == 1 and "面料采购" in report[0]["problem"]


def test_zero_quantity_documents_without_demand_do_not_create_an_obligation(mto):
    remove_line(mto)
    with mto() as session, begin_immediate(session):
        session.get(ProductionOrder, "BM-0").planned_qty = 0
        session.get(DocRevision, "BD-BM-0-3").snapshot_json = {"sku_id": SKU, "planned_qty": 0}
        for i in range(2):
            session.get(PurchaseOrder, f"BP-0-{i}").ordered_qty = 0
            snapshot = session.get(DocRevision, f"BD-BP-0-{i}-3")
            snapshot.snapshot_json = {**snapshot.snapshot_json, "ordered_qty": 0}
    assert service.inspect_downstream(mto) == []


def test_mto_commit_fault_stays_pending_and_replay_does_not_rewrite_documents(mto, monkeypatch):
    plan = service.plan_change(
        mto,
        order_id="BENCH-0",
        base_revision=2,
        intent_revision=2,
        changes=[{"mode": "increment", "sku_id": SKU, "qty": 1}],
        client_event_key="mto-commit-fault",
        now=NOW,
    )
    approval = service.approve_plan(
        mto,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
        now=NOW,
    )
    request = dict(
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        approval_id=approval.approval_id,
        idempotency_key="mto-commit-fault",
        now=NOW,
        acknowledge=True,
    )
    original = service._post_verify
    removed = {}

    def lose_current_line(factory, operation_id):
        with factory() as session, begin_immediate(session):
            assert session.get(SalesOrder, "BENCH-0").current_revision == 3
            line = service._current_order_lines(session)["BENCH-0"][SKU]
            removed.update(
                {column.name: getattr(line, column.name) for column in OrderLine.__table__.columns}
            )
            session.delete(line)
        original(factory, operation_id)

    with monkeypatch.context() as patch:
        patch.setattr(service, "_post_verify", lose_current_line)
        outcome = service.commit_plan(mto, **request)
    assert fencing.operation(mto, outcome.operation_id)["committed_verification_pending"]
    before = facts(mto)
    config = Settings(_env_file=None, verification_max_retries=1)
    assert verification.retry_pending(mto, outcome.operation_id, now=NOW, settings=config) == 1
    replay = service.commit_plan(mto, **request)
    assert replay.operation_id == outcome.operation_id and replay.replayed
    assert facts(mto) == before
    assert not verification.verify_manually(mto, outcome.operation_id, now=NOW, settings=config)
    # An operator uses the verified input quantity (3), not the old history (999).
    # Recovery is a separately approved new revision; verification never repairs it.
    recovery = service.plan_change(
        mto,
        order_id="BENCH-0",
        base_revision=3,
        intent_revision=3,
        changes=[{"mode": "set_total", "sku_id": SKU, "qty": removed["ordered_qty"]}],
        client_event_key="mto-recovery",
        now=NOW,
    )
    assert recovery.status == "VALIDATED"
    recovery_approval = service.approve_plan(
        mto,
        plan_id=recovery.plan_id,
        plan_hash=recovery.plan_hash,
        expected_intent_revision=recovery.intent_revision,
        now=NOW,
    )
    repaired_operation = service.commit_plan(
        mto,
        plan_id=recovery.plan_id,
        plan_hash=recovery.plan_hash,
        approval_id=recovery_approval.approval_id,
        idempotency_key="mto-recovery",
        now=NOW,
        acknowledge=True,
    )
    assert repaired_operation.operation_id != outcome.operation_id
    assert repaired_operation.revision == 4
    assert not fencing.operation(mto, repaired_operation.operation_id)[
        "committed_verification_pending"
    ]
    repaired = facts(mto)
    for name in ("production_order", "purchase_order", "doc_revision"):
        assert repaired[name] == before[name]
    with mto() as session:
        assert session.get(OrderLine, (removed["revision_id"], SKU)) is None
        assert service._current_order_lines(session)["BENCH-0"][SKU].ordered_qty == 3
    assert verification.verify_manually(mto, outcome.operation_id, now=NOW, settings=config)
    assert facts(mto) == repaired


@pytest.mark.parametrize("initial", [False, True])
def test_readback_keeps_pending_until_fault_is_restored_after_restart(
    mto, approved_commit, database_url, initial
):
    execution(mto, 2, 2)
    operation_id = fencing.pending(mto, approved_commit[1])
    remove_line(mto)
    before = facts(mto)
    config = Settings(_env_file=None, verification_max_retries=1)
    if initial:
        service._post_verify(mto, operation_id)
        assert fencing.operation(mto, operation_id)["committed_verification_pending"]
    assert verification.retry_pending(mto, operation_id, now=NOW, settings=config) == 1
    state = fencing.operation(mto, operation_id)
    assert state["committed_verification_pending"] and state["verification_manual_required"]
    assert facts(mto) == before
    engine = make_engine(database_url)
    restarted = make_session_factory(engine)
    try:
        assert not verification.verify_manually(restarted, operation_id, now=NOW, settings=config)
        assert facts(restarted) == before
        restore_line(restarted)
        repaired = facts(restarted)
        assert verification.verify_manually(restarted, operation_id, now=NOW, settings=config)
        assert facts(restarted) == repaired
        done = fencing.operation(restarted, operation_id)
        assert (
            not done["committed_verification_pending"] and not done["verification_manual_required"]
        )
        assert done["verification_retry_attempts"] == 1
        assert done["result_json"] == state["result_json"]
        assert verification.verify_manually(restarted, operation_id, now=NOW, settings=config)
        assert fencing.operation(restarted, operation_id) == done
    finally:
        engine.dispose()


@pytest.mark.parametrize("takeover", [False, True])
@pytest.mark.parametrize("fault", ["line", "snapshot", "purchase_snapshot", "purchase_content"])
def test_missing_fact_after_success_cannot_be_cleared_by_stale_result(
    mto, approved_commit, monkeypatch, takeover, fault
):
    operation_id = fencing.pending(mto, approved_commit[1])
    config = Settings(_env_file=None, verification_lease_seconds=10, verification_max_retries=2)
    arrived, release = Event(), Event()
    old_thread, paused = [], []
    current = [NOW]
    original = service.downstream_problems

    def readback(session):
        result = original(session)
        if old_thread and get_ident() == old_thread[0] and not paused:
            assert result == []
            paused.append(True)
            arrived.set()
            assert release.wait(15)
        return result

    def old_attempt():
        old_thread.append(get_ident())
        return verification.retry_pending(
            mto, operation_id, settings=config, clock=lambda: current[0]
        )

    monkeypatch.setattr(service, "downstream_problems", readback)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(old_attempt)
        try:
            assert arrived.wait(10)
            if fault == "line":
                remove_line(mto)
            elif fault == "snapshot":
                with mto() as session, begin_immediate(session):
                    session.delete(session.get(DocRevision, "BD-BM-0-3"))
            elif fault == "purchase_snapshot":
                with mto() as session, begin_immediate(session):
                    session.delete(session.get(DocRevision, "BD-BP-0-0-3"))
            else:
                with mto() as session, begin_immediate(session):
                    # Aggregate material quantity stays valid; both individual snapshots drift.
                    session.get(PurchaseOrder, "BP-0-0").ordered_qty = 3
                    session.get(PurchaseOrder, "BP-0-1").ordered_qty = 1
            before = facts(mto)
            if takeover:
                current[0] += timedelta(seconds=11)
                assert (
                    verification.retry_pending(mto, operation_id, now=current[0], settings=config)
                    == 1
                )
                newer = fencing.operation(mto, operation_id)
                assert (
                    newer["committed_verification_pending"]
                    and newer["verification_manual_required"]
                )
        finally:
            release.set()
        assert future.result(timeout=10) == 1
    state = fencing.operation(mto, operation_id)
    assert state["committed_verification_pending"]
    assert facts(mto) == before
    if takeover:
        assert state == newer
