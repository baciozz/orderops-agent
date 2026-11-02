"""Golden seed loads once and matches the demo numbers literally."""

from app.db.models import (
    Customer,
    DocRevision,
    MaterialUsage,
    ProductionOrder,
    PurchaseOrder,
    Reservation,
    SalesOrder,
    ShipmentRecord,
    Sku,
    StockBalance,
    StockMovement,
)
from app.services.seed import load_seed, verify_seed
from seeds.golden import WAREHOUSE_ID
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def test_seed_matches_plan_numbers(session_factory, database_url: str) -> None:
    report = load_seed(session_factory, database_url=database_url)
    assert report.created is True
    session: Session = session_factory()
    try:
        assert verify_seed(session) == []
        balances = {
            row.sku_id: (row.on_hand, row.reserved, row.on_hand - row.reserved)
            for row in session.scalars(select(StockBalance)).all()
        }
        assert balances["A-BLACK-M"] == (140, 40, 100)
        assert balances["A-WHITE-M"] == (120, 0, 120)
        assert balances["B-BLUE-L"] == (30, 10, 20)
        assert set(balances) == {"A-BLACK-M", "A-WHITE-M", "B-BLUE-L"}

        order = session.get(SalesOrder, "SO-1001")
        assert order is not None
        assert order.customer_id == "CUST-HD"
        assert order.current_revision == 1
        assert order.intent_revision == 1
        assert order.status == "PARTIALLY_SHIPPED"
        customer = session.get(Customer, "CUST-HD")
        assert customer is not None and customer.display_name == "华东服饰"

        other = session.get(SalesOrder, "SO-1002")
        assert other is not None and other.customer_id == "CUST-HN"
        owner = session.get(Customer, "CUST-HN")
        assert owner is not None and owner.display_name == "华南制衣"
        blue = session.get(Reservation, ("SO-1002", WAREHOUSE_ID, "B-BLUE-L"))
        assert blue is not None and blue.qty == 10

        shipped = session.scalar(
            select(func.sum(ShipmentRecord.qty)).where(ShipmentRecord.sku_id == "A-BLACK-M")
        )
        assert int(shipped or 0) == 60
        assert session.scalar(select(func.count()).select_from(StockMovement)) == 6

        assert session.get(ProductionOrder, "MO-2001").cut_qty == 120  # type: ignore[union-attr]
        assert session.get(ProductionOrder, "MO-2002").planned_qty == 180  # type: ignore[union-attr]
        assert session.get(PurchaseOrder, "PUR-3001").received_qty == 240  # type: ignore[union-attr]
        assert session.get(PurchaseOrder, "PUR-3002").ordered_qty == 360  # type: ignore[union-attr]
        usage = session.get(MaterialUsage, ("C-NAVY-L", "FAB-NAVY"))
        assert usage is not None and usage.qty_per_piece == 2
        navy = session.get(Sku, "C-NAVY-L")
        assert navy is not None and navy.default_fulfillment_mode == "MTO"
        assert session.get(DocRevision, "DR-MO-2001-1") is not None
    finally:
        session.close()


def test_seed_is_idempotent(session_factory, database_url: str) -> None:
    assert load_seed(session_factory, database_url=database_url).created is True
    assert load_seed(session_factory, database_url=database_url).created is False
    session: Session = session_factory()
    try:
        assert session.scalar(select(func.count()).select_from(SalesOrder)) == 2
        assert session.scalar(select(func.count()).select_from(StockMovement)) == 6
    finally:
        session.close()


def test_cli_seed_command(tmp_path, monkeypatch) -> None:
    database = tmp_path / "cli.db"
    monkeypatch.setenv("OOS_DATABASE_URL", f"sqlite:///{database}")
    from app.cli import main
    from app.config import reset_settings_cache

    reset_settings_cache()
    assert main(["seed"]) == 0
    reset_settings_cache()
    assert main(["seed"]) == 0
    assert database.exists()
