"""Order views count only active production linked to that order."""

from datetime import UTC, datetime

import pytest
from app.config import Settings
from app.db.models import DownstreamLink, OrderLine, OrderRevision, ProductionOrder, SalesOrder
from app.main import create_app
from app.services.seed import load_seed
from fastapi.testclient import TestClient

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture()
def orders_client(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    app = create_app(Settings(_env_file=None, database_url=database_url))
    with TestClient(app) as client:
        yield client, session_factory


def test_order_view_cut_qty_scoped_to_order(orders_client) -> None:
    client, factory = orders_client
    sku_id = "C-NAVY-L"
    order_ids = ("SO-REVIEW-CUT-1", "SO-REVIEW-CUT-2")
    with factory() as session:
        for order_id in order_ids:
            session.add(
                SalesOrder(
                    id=order_id,
                    customer_id="CUST-HD",
                    current_revision=1,
                    intent_revision=1,
                    status="OPEN",
                )
            )
        session.flush()
        for order_id in order_ids:
            session.add(
                OrderRevision(
                    id=f"SOR-{order_id}-1",
                    order_id=order_id,
                    revision_no=1,
                    base_revision=0,
                    reason="synthetic review fixture",
                    created_by="review-test",
                    created_at=NOW,
                )
            )
        session.flush()
        for order_id in order_ids:
            session.add(
                OrderLine(
                    revision_id=f"SOR-{order_id}-1",
                    sku_id=sku_id,
                    ordered_qty=10,
                    fulfillment_mode="MTO",
                )
            )
        for doc_id, order_id, cut_qty, status in [
            ("MO-REVIEW-CUT-1", order_ids[0], 5, "PARTIALLY_CUT"),
            ("MO-REVIEW-CUT-2", order_ids[1], 7, "PARTIALLY_CUT"),
            ("MO-REVIEW-CANCELLED", order_ids[0], 3, "CANCELLED"),
        ]:
            session.add(
                ProductionOrder(
                    id=doc_id,
                    sku_id=sku_id,
                    planned_qty=10,
                    cut_qty=cut_qty,
                    status=status,
                    doc_revision=1,
                )
            )
            session.add(
                DownstreamLink(
                    id=f"LNK-{doc_id}-1",
                    doc_type="production_order",
                    doc_id=doc_id,
                    order_id=order_id,
                    order_revision=1,
                    order_line_sku=sku_id,
                    link_seq=1,
                )
            )
        session.commit()

    cuts = {}
    for order_id in order_ids:
        response = client.get(f"/orders/{order_id}")
        assert response.status_code == 200
        line = next(row for row in response.json()["lines"] if row["sku_id"] == sku_id)
        cuts[order_id] = line["cut_qty"]
    assert cuts == {order_ids[0]: 5, order_ids[1]: 7}
