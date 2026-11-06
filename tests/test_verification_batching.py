"""Global verification retains its scope while query count stays bounded."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, get_ident

import pytest
from app.config import Settings
from app.db.models import (
    DocRevision,
    DownstreamLink,
    OrderLine,
    OrderRevision,
    ProductionOrder,
    PurchaseOrder,
    SalesOrder,
)
from app.db.session import begin_immediate
from app.orchestration.state import aware
from app.services import kernel as service
from app.services import verification
from sqlalchemy import event, select
from tests import test_commit_verification_pending as support
from tests import test_verification_result_fencing as fencing
from tests.verification_scale import NOW, add_orders

kernel = support.kernel
approved_commit = support.approved_commit


@pytest.mark.parametrize(
    ("kind", "count"), [("stock", 20), ("stock", 100), ("mto", 20), ("mto", 100)]
)
def test_full_verification_has_bounded_select_count(kernel, engine, kind, count):
    add_orders(kernel, count, kind)
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        with kernel() as session:
            assert service.consistency_problems(session) == []
            assert service.downstream_problems(session) == []
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) <= 20, len(statements)


def test_current_lines_and_current_doc_snapshots_exclude_history(kernel):
    add_orders(kernel, 3, "mto", history=5)
    with kernel() as session:
        # Historical lines and snapshots contain 999; only current quantities are 1.
        assert service.consistency_problems(session) == []
        assert service.downstream_problems(session) == []
    with kernel() as session, begin_immediate(session):
        row = session.get(DocRevision, "BD-BM-2-5")
        row.snapshot_json = {**row.snapshot_json, "planned_qty": 2}
    with kernel() as session:
        findings = service.downstream_findings(session)
        assert [f["problem"] for f in findings] == ["BM-2 当前内容与单据修订 5 不一致"]


def test_latest_links_remain_scoped_per_order(kernel):
    add_orders(kernel, 2, "mto")
    with kernel() as session, begin_immediate(session):
        # A newer association in another order must not hide this order's latest link.
        session.add(
            DownstreamLink(
                id="cross-order",
                doc_type="production_order",
                doc_id="BM-0",
                order_id="BENCH-1",
                order_revision=2,
                order_line_sku="C-BLACK-L",
                link_seq=4,
            )
        )
        row = session.get(DownstreamLink, "BL-BM-0-3")
        row.order_revision = 1
    with kernel() as session:
        problems = service.downstream_problems(session)
        assert "BM-0 仍链接到修订 1，当前修订是 2" in problems
        assert "C-BLACK-L 生产计划 2 与订单行 1 不一致" in problems


@pytest.mark.parametrize("revision", [0, 999])
def test_missing_current_revision_does_not_reuse_historical_lines(kernel, revision):
    add_orders(kernel, 1, "stock")
    with kernel() as session, begin_immediate(session):
        session.get(SalesOrder, "BENCH-0").current_revision = revision
    with kernel() as session:
        problems = service.consistency_problems(session)
        assert "BENCH-0/W1/A-WHITE-M 当前订单未发数量与预占不一致" in problems
        assert not any("订单数量低于已发数量" in p for p in problems)


def test_stock_line_is_not_hidden_by_unrelated_order(kernel):
    add_orders(kernel, 30, "stock")
    with kernel() as session, begin_immediate(session):
        revision = session.scalar(
            select(OrderRevision).where(
                OrderRevision.order_id == "BENCH-29", OrderRevision.revision_no == 2
            )
        )
        session.get(OrderLine, (revision.id, "A-WHITE-M")).ordered_qty += 1
    with kernel() as session:
        assert service.consistency_problems(session) == [
            "BENCH-29/W1/A-WHITE-M 当前订单未发数量与预占不一致"
        ]


def test_unlinked_docs_and_historical_snapshots_are_not_materialized(kernel):
    add_orders(kernel, 2, "mto", history=5)
    with kernel() as session, begin_immediate(session):
        session.add(
            ProductionOrder(
                id="UNLINKED",
                sku_id="C-BLACK-L",
                planned_qty=7,
                cut_qty=0,
                status="NOT_STARTED",
                doc_revision=1,
            )
        )
        session.flush()
        session.add(
            DocRevision(
                id="UNLINKED-SNAPSHOT",
                doc_type="production_order",
                doc_id="UNLINKED",
                rev_no=1,
                snapshot_json={"planned_qty": 999},
            )
        )
    loaded = []

    def record(session, instance):
        if isinstance(instance, (DocRevision, ProductionOrder, PurchaseOrder)):
            loaded.append(instance.id)

    with kernel() as session:
        event.listen(session, "loaded_as_persistent", record)
        assert service.downstream_problems(session) == []
    assert "UNLINKED" not in loaded and "UNLINKED-SNAPSHOT" not in loaded
    assert "BD-BM-0-5" in loaded and "BD-BM-0-1" not in loaded


@pytest.mark.parametrize("missing", [False, True])
def test_shared_purchase_demand_and_missing_docs_remain_checked(kernel, missing):
    add_orders(kernel, 4, "mto")
    with kernel() as session, begin_immediate(session):
        row = session.get(PurchaseOrder, "BP-3-1")
        if missing:
            session.delete(row)
        else:
            row.ordered_qty += 1
    with kernel() as session:
        expected_actual = 1 if missing else 3
        assert service.downstream_problems(session) == [
            f"BM-3 面料采购 {{'FAB-BLACK': {expected_actual}}} 与单耗 {{'FAB-BLACK': 2}} 不一致"
        ]


@pytest.mark.parametrize("takeover", [False, True])
def test_scaled_readback_refreshes_facts_and_fences_old_owner(
    kernel, approved_commit, monkeypatch, takeover
):
    add_orders(kernel, 80, "mto")
    operation_id = fencing.pending(kernel, approved_commit[1])
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
            kernel, operation_id, settings=config, clock=lambda: current[0]
        )

    monkeypatch.setattr(service, "downstream_problems", readback)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(old_attempt)
        try:
            assert arrived.wait(10)
            with kernel() as session, begin_immediate(session):
                session.get(PurchaseOrder, "BP-79-1").ordered_qty = 2
            if takeover:
                current[0] += timedelta(seconds=11)
                assert (
                    verification.retry_pending(
                        kernel, operation_id, settings=config, now=current[0]
                    )
                    == 1
                )
                newer = fencing.operation(kernel, operation_id)
                assert (
                    newer["committed_verification_pending"]
                    and newer["verification_manual_required"]
                )
        finally:
            release.set()
        assert future.result(timeout=10) == 1
    state = fencing.operation(kernel, operation_id)
    assert state["committed_verification_pending"]
    if takeover:
        assert state == newer
    with kernel() as session, begin_immediate(session):
        session.get(PurchaseOrder, "BP-79-1").ordered_qty = 1
    if takeover:
        assert verification.verify_manually(kernel, operation_id, settings=config, now=current[0])
    else:
        assert (
            verification.retry_pending(
                kernel,
                operation_id,
                settings=config,
                now=aware(state["verification_next_attempt_at"]),
            )
            == 1
        )
    assert not fencing.operation(kernel, operation_id)["committed_verification_pending"]
