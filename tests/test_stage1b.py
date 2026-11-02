"""Stage 1B downstream impact. B19–B27."""

from datetime import UTC, datetime

import pytest
from app.db.models import (
    Approval,
    DocRevision,
    DownstreamLink,
    Operation,
    OrderRevision,
    OutboxDelivery,
    ProductionOrder,
    PurchaseOrder,
    SalesOrder,
    StockBalance,
)
from app.db.schema import init_db
from app.db.session import make_engine, make_session_factory
from app.errors import AppError
from app.services.kernel import (
    approve_plan,
    commit_failpoint,
    commit_plan,
    inspect_downstream,
    plan_change,
    record_production_cut,
    verify_failpoint,
)
from app.services.seed import load_seed
from seeds.golden import WAREHOUSE_ID
from sqlalchemy import func, select

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture()
def kernel(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'kernel.db'}", busy_timeout_ms=5000)
    init_db(engine)
    factory = make_session_factory(engine)
    load_seed(factory, database_url=str(tmp_path / "kernel.db"))
    yield factory
    engine.dispose()


def _color_change(factory, qty: int, key: str, *, include_executed: bool = False):
    return plan_change(
        factory,
        order_id="SO-1001",
        base_revision=1,
        intent_revision=1,
        changes=[
            {
                "mode": "replace_remaining",
                "from_sku": "C-BLACK-L",
                "to_sku": "C-NAVY-L",
                "qty": qty,
                "include_executed": include_executed,
            }
        ],
        client_event_key=key,
        now=NOW,
    )


def _lines(factory) -> dict[str, int]:
    session = factory()
    try:
        order = session.get(SalesOrder, "SO-1001")
        revision = session.scalar(
            select(OrderRevision).where(
                OrderRevision.order_id == "SO-1001",
                OrderRevision.revision_no == order.current_revision,
            )
        )
        from app.db.models import OrderLine

        rows = session.scalars(select(OrderLine).where(OrderLine.revision_id == revision.id)).all()
        return {row.sku_id: row.ordered_qty for row in rows}
    finally:
        session.close()


def _current_links(factory) -> dict[str, DownstreamLink]:
    session = factory()
    try:
        best: dict[str, DownstreamLink] = {}
        for link in session.scalars(select(DownstreamLink)).all():
            current = best.get(link.doc_id)
            if current is None or link.link_seq > current.link_seq:
                best[link.doc_id] = link
        return best
    finally:
        session.close()


def test_B19_all_to_navy_needs_clarification(kernel) -> None:
    outcome = _color_change(kernel, 300, "b19")
    assert outcome.status == "NEEDS_CLARIFICATION"
    text = " ".join(outcome.issues + outcome.questions)
    assert "MO-2001 EXECUTED_LOCKED" in text
    assert "PUR-3001 EXECUTED_LOCKED" in text
    assert "MO-2002 CHANGEABLE" in text
    assert "PUR-3002 CHANGEABLE" in text
    assert "180" in text
    assert _lines(kernel) == {"A-BLACK-M": 100, "C-BLACK-L": 300}
    session = kernel()
    try:
        assert session.get(SalesOrder, "SO-1001").current_revision == 1
        assert session.get(ProductionOrder, "MO-2002").sku_id == "C-BLACK-L"
        assert session.get(PurchaseOrder, "PUR-3002").material_id == "FAB-BLACK"
        assert session.scalar(select(func.count()).select_from(DocRevision)) == 4
        assert all(link.order_revision == 1 for link in _current_links(kernel).values())
        assert session.scalar(select(func.count()).select_from(Operation)) == 0
        black = session.get(StockBalance, (WAREHOUSE_ID, "A-BLACK-M"))
        assert (black.on_hand, black.reserved) == (140, 40)
    finally:
        session.close()


def test_B20_only_uncut_180_commits_and_verifies(kernel) -> None:
    outcome = plan_change(
        kernel,
        order_id="SO-1001",
        base_revision=1,
        intent_revision=1,
        changes=[
            {
                "mode": "replace_remaining",
                "from_sku": "C-BLACK-L",
                "to_sku": "C-NAVY-L",
                "choice": "only_uncut",
            }
        ],
        client_event_key="b20",
        now=NOW,
    )
    assert outcome.requires_human_approval is True
    approval = approve_plan(
        kernel,
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
        now=NOW,
    )
    committed = commit_plan(
        kernel,
        plan_id=outcome.plan_id,
        approval_id=approval.approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="b20",
        now=NOW,
    )
    assert committed.revision == 2
    assert _lines(kernel) == {
        "A-BLACK-M": 100,
        "C-BLACK-L": 120,
        "C-NAVY-L": 180,
    }
    session = kernel()
    try:
        untouched = session.get(ProductionOrder, "MO-2001")
        changed = session.get(ProductionOrder, "MO-2002")
        fabric_old = session.get(PurchaseOrder, "PUR-3001")
        fabric_new = session.get(PurchaseOrder, "PUR-3002")
        assert (
            untouched.sku_id,
            untouched.planned_qty,
            untouched.cut_qty,
            untouched.doc_revision,
        ) == (
            "C-BLACK-L",
            120,
            120,
            1,
        )
        assert (
            changed.sku_id,
            changed.planned_qty,
            changed.cut_qty,
            changed.status,
            changed.doc_revision,
        ) == (
            "C-NAVY-L",
            180,
            0,
            "NOT_STARTED",
            2,
        )
        assert fabric_old.material_id == "FAB-BLACK" and fabric_old.doc_revision == 1
        assert (
            fabric_new.material_id,
            fabric_new.ordered_qty,
            fabric_new.change_state,
            fabric_new.status,
            fabric_new.doc_revision,
        ) == ("FAB-NAVY", 360, "PENDING_SUPPLIER", "PLACED", 2)
        links = _current_links(kernel)
        assert set(links) == {"MO-2001", "MO-2002", "PUR-3001", "PUR-3002"}
        assert all(link.order_revision == 2 for link in links.values())
        assert links["MO-2002"].order_line_sku == "C-NAVY-L"
        assert links["PUR-3002"].order_line_sku == "C-NAVY-L"
        black = session.get(StockBalance, (WAREHOUSE_ID, "A-BLACK-M"))
        white = session.get(StockBalance, (WAREHOUSE_ID, "A-WHITE-M"))
        assert (black.on_hand, black.reserved) == (140, 40)
        assert (white.on_hand, white.reserved) == (120, 0)
        operation = session.get(Operation, committed.operation_id)
        assert operation.committed_verification_pending is False
        assert session.scalar(select(func.count()).select_from(Operation)) == 1
    finally:
        session.close()


def test_B21_verification_failure_does_not_rewrite(kernel) -> None:
    outcome = _color_change(kernel, 180, "b21")
    approval = approve_plan(
        kernel,
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
        now=NOW,
    )
    token = verify_failpoint.set(True)
    try:
        committed = commit_plan(
            kernel,
            plan_id=outcome.plan_id,
            approval_id=approval.approval_id,
            plan_hash=outcome.plan_hash,
            idempotency_key="b21",
            now=NOW,
        )
    finally:
        verify_failpoint.reset(token)
    session = kernel()
    try:
        operation = session.get(Operation, committed.operation_id)
        assert operation.committed_verification_pending is True
        assert session.get(SalesOrder, "SO-1001").current_revision == 2
        assert session.get(ProductionOrder, "MO-2002").sku_id == "C-NAVY-L"
        assert (
            session.scalar(
                select(func.count())
                .select_from(OrderRevision)
                .where(OrderRevision.order_id == "SO-1001")
            )
            == 2
        )
    finally:
        session.close()


def test_B22_explicit_cut_change_is_rejected(kernel) -> None:
    with pytest.raises(AppError) as exc:
        _color_change(kernel, 300, "b22", include_executed=True)
    assert exc.value.code == "EXECUTED_PORTION_IMMUTABLE"
    assert exc.value.http_status == 422
    assert _lines(kernel)["C-BLACK-L"] == 300
    session = kernel()
    try:
        assert session.get(ProductionOrder, "MO-2001").sku_id == "C-BLACK-L"
        assert session.scalar(select(func.count()).select_from(Operation)) == 0
    finally:
        session.close()


def test_B23_cut_after_approval_is_stale_and_130_remain(kernel) -> None:
    outcome = _color_change(kernel, 180, "b23")
    approval = approve_plan(
        kernel,
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
        now=NOW,
    )
    record_production_cut(kernel, doc_id="MO-2002", cut_qty=50, now=NOW)
    with pytest.raises(AppError) as exc:
        commit_plan(
            kernel,
            plan_id=outcome.plan_id,
            approval_id=approval.approval_id,
            plan_hash=outcome.plan_hash,
            idempotency_key="b23",
            now=NOW,
        )
    assert exc.value.code == "STALE_DEPENDENCY"
    session = kernel()
    try:
        assert session.get(Approval, approval.approval_id).status == "REVOKED"
        assert session.get(SalesOrder, "SO-1001").current_revision == 1
        intent = session.get(SalesOrder, "SO-1001").intent_revision
    finally:
        session.close()
    again = plan_change(
        kernel,
        order_id="SO-1001",
        base_revision=1,
        intent_revision=intent,
        changes=[
            {
                "mode": "replace_remaining",
                "from_sku": "C-BLACK-L",
                "to_sku": "C-NAVY-L",
                "qty": 180,
            }
        ],
        client_event_key="b23-again",
        now=NOW,
    )
    assert again.status == "NEEDS_CLARIFICATION"
    assert "130" in " ".join(again.questions + again.issues)


def test_B24_total_below_cut_violates_invariant_6(kernel) -> None:
    with pytest.raises(AppError) as exc:
        plan_change(
            kernel,
            order_id="SO-1001",
            base_revision=1,
            intent_revision=1,
            changes=[{"mode": "set_total", "sku_id": "C-BLACK-L", "qty": 100}],
            client_event_key="b24",
            now=NOW,
        )
    assert exc.value.http_status == 422
    assert exc.value.code == "INVARIANT_6"
    assert _lines(kernel)["C-BLACK-L"] == 300


def test_B25_stock_change_relinks_without_editing_documents(kernel) -> None:
    outcome = plan_change(
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
        client_event_key="b25",
        now=NOW,
    )
    verdicts = {item["doc_id"]: item["verdict"] for item in outcome.verdicts}
    assert verdicts == {
        "MO-2001": "UNAFFECTED_RELINK",
        "MO-2002": "UNAFFECTED_RELINK",
        "PUR-3001": "UNAFFECTED_RELINK",
        "PUR-3002": "UNAFFECTED_RELINK",
    }
    approval = approve_plan(
        kernel,
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
        now=NOW,
    )
    commit_plan(
        kernel,
        plan_id=outcome.plan_id,
        approval_id=approval.approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="b25",
        now=NOW,
    )
    session = kernel()
    try:
        assert session.scalar(select(func.count()).select_from(DocRevision)) == 4
        mo = session.get(ProductionOrder, "MO-2002")
        assert (mo.sku_id, mo.planned_qty, mo.doc_revision) == ("C-BLACK-L", 180, 1)
        pur = session.get(PurchaseOrder, "PUR-3002")
        assert (pur.material_id, pur.ordered_qty, pur.doc_revision, pur.change_state) == (
            "FAB-BLACK",
            360,
            1,
            "NONE",
        )
        assert all(link.order_revision == 2 for link in _current_links(kernel).values())
    finally:
        session.close()


def test_B26_inspection_reports_drift_and_does_not_write(kernel) -> None:
    outcome = plan_change(
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
        client_event_key="b26",
        now=NOW,
    )
    approval = approve_plan(
        kernel,
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
        now=NOW,
    )
    commit_plan(
        kernel,
        plan_id=outcome.plan_id,
        approval_id=approval.approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="b26",
        now=NOW,
    )
    session = kernel()
    try:
        mo = session.get(ProductionOrder, "MO-2002")
        mo.planned_qty = 999
        session.add(
            DownstreamLink(
                id="LNK-DRIFT",
                doc_type="production_order",
                doc_id="MO-2002",
                order_id="SO-1001",
                order_revision=1,
                order_line_sku="C-BLACK-L",
                link_seq=3,
                operation_id=None,
            )
        )
        session.commit()
        docs_before = session.scalar(select(func.count()).select_from(DocRevision))
        links_before = session.scalar(select(func.count()).select_from(DownstreamLink))
    finally:
        session.close()
    report = inspect_downstream(kernel)
    assert any(item["doc_id"] == "MO-2002" for item in report)
    assert any(item.get("repair", {}).get("action") == "relink" for item in report)
    session = kernel()
    try:
        assert session.scalar(select(func.count()).select_from(DocRevision)) == docs_before
        assert session.scalar(select(func.count()).select_from(DownstreamLink)) == links_before
        assert session.get(ProductionOrder, "MO-2002").planned_qty == 999
    finally:
        session.close()


def test_B27_downstream_failpoint_rolls_everything_back(kernel) -> None:
    outcome = _color_change(kernel, 180, "b27")
    approval = approve_plan(
        kernel,
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
        now=NOW,
    )
    token = commit_failpoint.set("before_downstream")
    try:
        with pytest.raises(RuntimeError, match="failpoint"):
            commit_plan(
                kernel,
                plan_id=outcome.plan_id,
                approval_id=approval.approval_id,
                plan_hash=outcome.plan_hash,
                idempotency_key="b27",
                now=NOW,
            )
    finally:
        commit_failpoint.reset(token)
    session = kernel()
    try:
        assert session.get(SalesOrder, "SO-1001").current_revision == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(OrderRevision)
                .where(OrderRevision.order_id == "SO-1001")
            )
            == 1
        )
        assert session.get(ProductionOrder, "MO-2002").sku_id == "C-BLACK-L"
        assert session.get(PurchaseOrder, "PUR-3002").material_id == "FAB-BLACK"
        assert session.scalar(select(func.count()).select_from(DocRevision)) == 4
        assert session.scalar(select(func.count()).select_from(DownstreamLink)) == 4
        assert session.get(Approval, approval.approval_id).status == "ACTIVE"
        assert session.scalar(select(func.count()).select_from(OutboxDelivery)) == 0
    finally:
        session.close()
