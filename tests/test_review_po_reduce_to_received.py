"""Reducing a purchase order to its received quantity must complete it."""

from datetime import UTC, datetime

import pytest
from app.db.models import (
    DocRevision,
    DownstreamLink,
    OrderLine,
    OrderRevision,
    ProductionOrder,
    PurchaseOrder,
    SalesOrder,
)
from app.services.kernel import (
    approve_plan,
    commit_plan,
    consistency_problems,
    downstream_problems,
    plan_change,
)
from app.services.seed import load_seed

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture()
def kernel(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    return session_factory


def test_reduce_po_to_received_qty_commits_cleanly(kernel) -> None:
    order_id = "SO-REVIEW-PO"
    mo_id = "MO-REVIEW-PO"
    po_id = "PUR-REVIEW-PO"
    revision_id = "SOR-REVIEW-PO-1"
    sku_id = "C-NAVY-L"
    with kernel() as session:
        session.add(
            SalesOrder(
                id=order_id,
                customer_id="CUST-HD",
                current_revision=1,
                intent_revision=1,
                status="OPEN",
            )
        )
        session.add(
            ProductionOrder(
                id=mo_id,
                sku_id=sku_id,
                planned_qty=10,
                cut_qty=0,
                status="NOT_STARTED",
                doc_revision=1,
            )
        )
        session.flush()
        session.add(
            OrderRevision(
                id=revision_id,
                order_id=order_id,
                revision_no=1,
                base_revision=0,
                reason="synthetic review fixture",
                created_by="review-test",
                created_at=NOW,
            )
        )
        # The seeded navy SKU consumes 2 metres per piece: 10 -> 4 pieces means 20 -> 8.
        session.add(
            PurchaseOrder(
                id=po_id,
                supplier_id="SUP-01",
                material_id="FAB-NAVY",
                production_order_id=mo_id,
                ordered_qty=20,
                received_qty=8,
                status="PARTIALLY_RECEIVED",
                change_state="NONE",
                doc_revision=1,
            )
        )
        session.flush()
        session.add(
            OrderLine(
                revision_id=revision_id,
                sku_id=sku_id,
                ordered_qty=10,
                fulfillment_mode="MTO",
            )
        )
        for doc_type, doc_id, snapshot in [
            (
                "production_order",
                mo_id,
                {"sku_id": sku_id, "planned_qty": 10, "cut_qty": 0, "status": "NOT_STARTED"},
            ),
            (
                "purchase_order",
                po_id,
                {
                    "material_id": "FAB-NAVY",
                    "ordered_qty": 20,
                    "received_qty": 8,
                    "status": "PARTIALLY_RECEIVED",
                    "change_state": "NONE",
                },
            ),
        ]:
            session.add(
                DocRevision(
                    id=f"DR-{doc_id}-1",
                    doc_type=doc_type,
                    doc_id=doc_id,
                    rev_no=1,
                    snapshot_json=snapshot,
                )
            )
            session.add(
                DownstreamLink(
                    id=f"LNK-{doc_id}-1",
                    doc_type=doc_type,
                    doc_id=doc_id,
                    order_id=order_id,
                    order_revision=1,
                    order_line_sku=sku_id,
                    link_seq=1,
                )
            )
        session.commit()
        assert consistency_problems(session) == []
        assert downstream_problems(session) == []

    outcome = plan_change(
        kernel,
        order_id=order_id,
        base_revision=1,
        intent_revision=1,
        changes=[{"mode": "set_total", "sku_id": sku_id, "qty": 4}],
        client_event_key="review-po-reduce",
        now=NOW,
    )
    assert outcome.status == "VALIDATED"
    po_verdict = next(item for item in outcome.verdicts if item["doc_id"] == po_id)
    assert po_verdict["verdict"] == "CHANGE_DRAFT"
    assert po_verdict["after"]["ordered_qty"] == 8
    approval = approve_plan(
        kernel,
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
        now=NOW,
    )
    # A valid boundary reduction must commit without an IntegrityError (HTTP 500).
    committed = commit_plan(
        kernel,
        plan_id=outcome.plan_id,
        approval_id=approval.approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="review-po-reduce-commit",
        now=NOW,
    )
    assert committed.revision == 2
    with kernel() as session:
        po = session.get(PurchaseOrder, po_id)
        assert po is not None
        assert (po.ordered_qty, po.received_qty, po.status) == (8, 8, "RECEIVED")
        assert consistency_problems(session) == []
        assert downstream_problems(session) == []
