"""Valid, isolated STOCK/MTO facts for readback scaling and regression tests."""

from datetime import UTC, datetime

from app.db.models import (
    DocRevision,
    DownstreamLink,
    OrderLine,
    OrderRevision,
    ProductionOrder,
    PurchaseOrder,
    Reservation,
    SalesOrder,
    StockBalance,
    StockMovement,
)
from app.db.session import begin_immediate

NOW = datetime(2026, 11, 6, tzinfo=UTC)


def add_orders(factory, count, kind, *, history=3):
    """Each current line is one unit; MTO has one MO and two POs sharing its need."""
    sku = "A-WHITE-M" if kind == "stock" else "C-BLACK-L"
    with factory() as session, begin_immediate(session):
        customer = session.get(SalesOrder, "SO-1001").customer_id
        session.add_all(
            SalesOrder(
                id=f"BENCH-{i}",
                customer_id=customer,
                current_revision=2,
                intent_revision=2,
                status="OPEN",
            )
            for i in range(count)
        )
        session.flush()
        session.add_all(
            OrderRevision(
                id=f"BR-{i}-{rev}",
                order_id=f"BENCH-{i}",
                revision_no=rev,
                base_revision=rev - 1,
                reason="synthetic scale",
                created_by="synthetic",
                created_at=NOW,
            )
            for i in range(count)
            for rev in (1, 2)
        )
        session.flush()
        session.add_all(
            OrderLine(
                revision_id=f"BR-{i}-{rev}",
                sku_id=sku,
                ordered_qty=1 if rev == 2 else 999,
                fulfillment_mode=kind.upper(),
            )
            for i in range(count)
            for rev in (1, 2)
        )
        if kind == "stock" and count:
            balance = session.get(StockBalance, ("W1", sku))
            before = balance.revision
            balance.on_hand += count
            balance.reserved += count
            balance.revision += 2
            session.add_all(
                [
                    StockMovement(
                        id="BENCH-RECEIVE",
                        operation_id="BENCH-SUPPLY",
                        seq=0,
                        movement_type="RECEIPT",
                        warehouse_id="W1",
                        sku_id=sku,
                        delta_on_hand=count,
                        delta_reserved=0,
                        before_revision=before,
                        after_revision=before + 1,
                        at=NOW,
                    ),
                    StockMovement(
                        id="BENCH-RESERVE",
                        operation_id="BENCH-SUPPLY",
                        seq=1,
                        movement_type="RESERVE",
                        warehouse_id="W1",
                        sku_id=sku,
                        delta_on_hand=0,
                        delta_reserved=count,
                        before_revision=before + 1,
                        after_revision=before + 2,
                        at=NOW,
                    ),
                ]
            )
            session.add_all(
                Reservation(
                    order_id=f"BENCH-{i}",
                    warehouse_id="W1",
                    sku_id=sku,
                    qty=1,
                    revision=balance.revision,
                )
                for i in range(count)
            )
        elif kind == "mto":
            session.add_all(
                ProductionOrder(
                    id=f"BM-{i}",
                    sku_id=sku,
                    planned_qty=1,
                    cut_qty=0,
                    status="NOT_STARTED",
                    doc_revision=history,
                )
                for i in range(count)
            )
            session.flush()
            session.add_all(
                PurchaseOrder(
                    id=f"BP-{i}-{j}",
                    supplier_id="SUP-01",
                    material_id="FAB-BLACK",
                    production_order_id=f"BM-{i}",
                    ordered_qty=1,
                    received_qty=0,
                    status="PLACED",
                    change_state="NONE",
                    doc_revision=history,
                )
                for i in range(count)
                for j in range(2)
            )
            session.flush()
            for i in range(count):
                for doc_type, doc_id in [
                    ("production_order", f"BM-{i}"),
                    ("purchase_order", f"BP-{i}-0"),
                    ("purchase_order", f"BP-{i}-1"),
                ]:
                    for rev in range(1, history + 1):
                        session.add(
                            DocRevision(
                                id=f"BD-{doc_id}-{rev}",
                                doc_type=doc_type,
                                doc_id=doc_id,
                                rev_no=rev,
                                snapshot_json=(
                                    {"sku_id": sku, "planned_qty": 1 if rev == history else 999}
                                    if doc_type == "production_order"
                                    else {
                                        "material_id": "FAB-BLACK",
                                        "ordered_qty": 1 if rev == history else 999,
                                        "received_qty": 0,
                                        "status": "PLACED",
                                        "change_state": "NONE",
                                    }
                                ),
                            )
                        )
                        session.add(
                            DownstreamLink(
                                id=f"BL-{doc_id}-{rev}",
                                doc_type=doc_type,
                                doc_id=doc_id,
                                order_id=f"BENCH-{i}",
                                order_revision=2 if rev == history else 1,
                                order_line_sku=sku,
                                link_seq=rev,
                            )
                        )
