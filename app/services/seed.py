"""Load the synthetic golden snapshot in one BEGIN IMMEDIATE transaction."""

from dataclasses import dataclass

from seeds.golden import EXPECTED_BALANCES, SEED_AT, WAREHOUSE_ID
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    Customer,
    DocRevision,
    DownstreamLink,
    Material,
    MaterialUsage,
    OrderLine,
    OrderRevision,
    ProductionOrder,
    PurchaseOrder,
    Reservation,
    SalesOrder,
    ShipmentRecord,
    Sku,
    StockBalance,
    StockMovement,
    Supplier,
    Warehouse,
)
from app.db.session import begin_immediate
from app.domain.inventory import MovementView, rebuild_balances
from app.errors import InventoryInvariantError, SeedIntegrityError

MTO_SKUS = ("C-BLACK-L", "C-NAVY-L")


@dataclass(frozen=True)
class SeedReport:
    created: bool
    database_url: str
    orders: tuple[str, ...]


def load_seed(session_factory: sessionmaker[Session], *, database_url: str) -> SeedReport:
    session = session_factory()
    try:
        with begin_immediate(session):
            if session.get(SalesOrder, "SO-1001") is not None:
                created = False
            else:
                _insert(session)
                violations = verify_seed(session)
                if violations:
                    raise SeedIntegrityError(violations)
                created = True
        return SeedReport(
            created=created,
            database_url=database_url,
            orders=("SO-1001", "SO-1002"),
        )
    finally:
        session.close()


def verify_seed(session: Session) -> list[str]:
    """Return human-readable violations. Empty means the snapshot is consistent."""
    problems: list[str] = []
    movements = list(session.scalars(select(StockMovement)).all())
    views = [
        MovementView(
            sku_id=row.sku_id,
            delta_on_hand=row.delta_on_hand,
            delta_reserved=row.delta_reserved,
            before_revision=row.before_revision,
            after_revision=row.after_revision,
        )
        for row in movements
    ]
    try:
        rebuilt = rebuild_balances(views)
    except InventoryInvariantError as exc:
        return [str(exc)]

    balances = list(session.scalars(select(StockBalance)).all())
    by_sku = {row.sku_id: row for row in balances}
    if set(by_sku) != set(EXPECTED_BALANCES):
        problems.append(f"库存 SKU 集合不符：{sorted(by_sku)}")
    for sku_id, (on_hand, reserved, revision) in EXPECTED_BALANCES.items():
        row = by_sku.get(sku_id)
        replay = rebuilt.get(sku_id)
        if row is None or replay is None:
            problems.append(f"{sku_id} 缺少余额或流水")
            continue
        if (row.on_hand, row.reserved, row.revision) != (on_hand, reserved, revision):
            problems.append(
                f"{sku_id} 余额 {row.on_hand}/{row.reserved}/r{row.revision} "
                f"!= {on_hand}/{reserved}/r{revision}"
            )
        if (replay.on_hand, replay.reserved, replay.revision) != (on_hand, reserved, revision):
            problems.append(f"{sku_id} 流水重建结果与黄金数字不一致")
        if row.warehouse_id != WAREHOUSE_ID:
            problems.append(f"{sku_id} 仓库不是 {WAREHOUSE_ID}")

    reserved_sums = dict(
        session.execute(
            select(Reservation.sku_id, func.sum(Reservation.qty)).group_by(Reservation.sku_id)
        ).all()
    )
    reserved_black = int(reserved_sums.get("A-BLACK-M") or 0)
    reserved_blue = int(reserved_sums.get("B-BLUE-L") or 0)
    if reserved_black != 40 or reserved_blue != 10:
        problems.append(f"预占合计不符：{reserved_sums}")
    if "A-WHITE-M" in reserved_sums or any(sku in reserved_sums for sku in MTO_SKUS):
        problems.append("白色或 MTO 行不应有预占")

    black_res = session.get(Reservation, ("SO-1001", WAREHOUSE_ID, "A-BLACK-M"))
    blue_res = session.get(Reservation, ("SO-1002", WAREHOUSE_ID, "B-BLUE-L"))
    if black_res is None or black_res.qty != 40:
        problems.append("SO-1001 黑色 M 预占应为 40")
    if blue_res is None or blue_res.qty != 10:
        problems.append("SO-1002 蓝色 L 预占应为 10")

    shipped = session.scalar(
        select(func.sum(ShipmentRecord.qty)).where(
            ShipmentRecord.order_id == "SO-1001",
            ShipmentRecord.sku_id == "A-BLACK-M",
        )
    )
    if int(shipped or 0) != 60:
        problems.append(f"SO-1001 黑色 M 已发应为 60，实际 {shipped}")

    lines = {
        (row.revision_id, row.sku_id): (row.ordered_qty, row.fulfillment_mode)
        for row in session.scalars(select(OrderLine)).all()
    }
    expected_lines = {
        ("SOR-1001-1", "A-BLACK-M"): (100, "STOCK"),
        ("SOR-1001-1", "C-BLACK-L"): (300, "MTO"),
        ("SOR-1002-1", "B-BLUE-L"): (10, "STOCK"),
    }
    if lines != expected_lines:
        problems.append(f"订单行不符：{lines}")

    order = session.get(SalesOrder, "SO-1001")
    if order is None or order.current_revision != 1 or order.status != "PARTIALLY_SHIPPED":
        problems.append("SO-1001 修订或状态不符")
    other = session.get(SalesOrder, "SO-1002")
    if other is None or other.status != "OPEN":
        problems.append("SO-1002 应为 OPEN")

    mo_2001 = session.get(ProductionOrder, "MO-2001")
    mo_2002 = session.get(ProductionOrder, "MO-2002")
    if mo_2001 is None or (mo_2001.planned_qty, mo_2001.cut_qty, mo_2001.status) != (
        120,
        120,
        "CUT",
    ):
        problems.append("MO-2001 应为计划 120、已裁 120、CUT")
    if mo_2002 is None or (mo_2002.planned_qty, mo_2002.cut_qty, mo_2002.status) != (
        180,
        0,
        "NOT_STARTED",
    ):
        problems.append("MO-2002 应为计划 180、已裁 0、NOT_STARTED")
    if mo_2001 and mo_2002 and mo_2001.planned_qty + mo_2002.planned_qty != 300:
        problems.append("C-BLACK-L 生产计划之和应为 300")

    pur_3001 = session.get(PurchaseOrder, "PUR-3001")
    pur_3002 = session.get(PurchaseOrder, "PUR-3002")
    if pur_3001 is None or (
        pur_3001.ordered_qty,
        pur_3001.received_qty,
        pur_3001.status,
        pur_3001.material_id,
    ) != (240, 240, "RECEIVED", "FAB-BLACK"):
        problems.append("PUR-3001 应为 FAB-BLACK 240 已到")
    if pur_3002 is None or (
        pur_3002.ordered_qty,
        pur_3002.received_qty,
        pur_3002.status,
        pur_3002.change_state,
    ) != (360, 0, "PLACED", "NONE"):
        problems.append("PUR-3002 应为 FAB-BLACK 360 已下、未到")

    usage = {
        (row.sku_id, row.material_id): row.qty_per_piece
        for row in session.scalars(select(MaterialUsage)).all()
    }
    if usage != {("C-BLACK-L", "FAB-BLACK"): 2, ("C-NAVY-L", "FAB-NAVY"): 2}:
        problems.append(f"单耗不符：{usage}")

    links = list(session.scalars(select(DownstreamLink)).all())
    if len(links) != 4:
        problems.append(f"下游链接应为 4 条，实际 {len(links)}")
    for link in links:
        if link.order_id != "SO-1001" or link.order_revision != 1 or link.link_seq != 1:
            problems.append(f"{link.doc_id} 链接没有指向 SO-1001 r1")
        if link.order_line_sku != "C-BLACK-L":
            problems.append(f"{link.doc_id} 应链接 C-BLACK-L")

    for sku_id in MTO_SKUS:
        if any(row.sku_id == sku_id for row in movements):
            problems.append(f"MTO SKU {sku_id} 不应出现在成品库存流水")
    material_ids = {row.id for row in session.scalars(select(Material)).all()}
    if any(row.sku_id in material_ids for row in movements):
        problems.append("采购单不应写成品库存流水")

    navy = session.get(Sku, "C-NAVY-L")
    if navy is None or navy.default_fulfillment_mode != "MTO" or not navy.active:
        problems.append("主数据应包含可售的 C-NAVY-L")
    navy_orders = session.scalar(
        select(func.count())
        .select_from(ProductionOrder)
        .where(ProductionOrder.sku_id == "C-NAVY-L")
    )
    if navy_orders:
        problems.append("seed 阶段 C-NAVY-L 还没有生产单")
    return problems


def _insert(session: Session) -> None:
    session.add(Warehouse(id=WAREHOUSE_ID, code="W1", name="主仓"))
    session.add_all(
        [
            Customer(id="CUST-HD", code="HD", display_name="华东服饰", active=True),
            Customer(id="CUST-HN", code="HN", display_name="华南制衣", active=True),
        ]
    )
    session.add(Supplier(id="SUP-01", code="SUP-01", name="合成供应商"))
    session.add_all(
        [
            Material(id="FAB-BLACK", code="FAB-BLACK", color="BLACK", unit="m"),
            Material(id="FAB-NAVY", code="FAB-NAVY", color="NAVY", unit="m"),
        ]
    )
    session.add_all(
        [
            Sku(
                id="A-BLACK-M",
                style="A",
                color="BLACK",
                size="M",
                active=True,
                default_fulfillment_mode="STOCK",
            ),
            Sku(
                id="A-WHITE-M",
                style="A",
                color="WHITE",
                size="M",
                active=True,
                default_fulfillment_mode="STOCK",
            ),
            Sku(
                id="B-BLUE-L",
                style="B",
                color="BLUE",
                size="L",
                active=True,
                default_fulfillment_mode="STOCK",
            ),
            Sku(
                id="C-BLACK-L",
                style="C",
                color="BLACK",
                size="L",
                active=True,
                default_fulfillment_mode="MTO",
            ),
            Sku(
                id="C-NAVY-L",
                style="C",
                color="NAVY",
                size="L",
                active=True,
                default_fulfillment_mode="MTO",
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            MaterialUsage(sku_id="C-BLACK-L", material_id="FAB-BLACK", qty_per_piece=2),
            MaterialUsage(sku_id="C-NAVY-L", material_id="FAB-NAVY", qty_per_piece=2),
        ]
    )
    session.flush()
    session.add_all(
        [
            SalesOrder(
                id="SO-1001",
                customer_id="CUST-HD",
                current_revision=1,
                intent_revision=1,
                status="PARTIALLY_SHIPPED",
            ),
            SalesOrder(
                id="SO-1002",
                customer_id="CUST-HN",
                current_revision=1,
                intent_revision=1,
                status="OPEN",
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            OrderRevision(
                id="SOR-1001-1",
                order_id="SO-1001",
                revision_no=1,
                base_revision=0,
                reason="seed snapshot",
                created_by="seed",
                source_event_id=None,
                created_at=SEED_AT,
            ),
            OrderRevision(
                id="SOR-1002-1",
                order_id="SO-1002",
                revision_no=1,
                base_revision=0,
                reason="seed snapshot",
                created_by="seed",
                source_event_id=None,
                created_at=SEED_AT,
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            OrderLine(
                revision_id="SOR-1001-1",
                sku_id="A-BLACK-M",
                ordered_qty=100,
                fulfillment_mode="STOCK",
            ),
            OrderLine(
                revision_id="SOR-1001-1",
                sku_id="C-BLACK-L",
                ordered_qty=300,
                fulfillment_mode="MTO",
            ),
            OrderLine(
                revision_id="SOR-1002-1",
                sku_id="B-BLUE-L",
                ordered_qty=10,
                fulfillment_mode="STOCK",
            ),
        ]
    )
    session.flush()
    session.add(
        ShipmentRecord(
            id="SHP-1001-BLACK",
            order_id="SO-1001",
            sku_id="A-BLACK-M",
            qty=60,
            shipped_at=SEED_AT,
            source="seed",
            operation_id="OP-SEED-A-BLACK-M",
        )
    )
    session.flush()
    session.add_all(
        [
            StockBalance(
                warehouse_id=WAREHOUSE_ID,
                sku_id="A-BLACK-M",
                on_hand=140,
                reserved=40,
                revision=3,
            ),
            StockBalance(
                warehouse_id=WAREHOUSE_ID,
                sku_id="A-WHITE-M",
                on_hand=120,
                reserved=0,
                revision=1,
            ),
            StockBalance(
                warehouse_id=WAREHOUSE_ID,
                sku_id="B-BLUE-L",
                on_hand=30,
                reserved=10,
                revision=2,
            ),
        ]
    )
    session.add_all(
        [
            Reservation(
                order_id="SO-1001",
                warehouse_id=WAREHOUSE_ID,
                sku_id="A-BLACK-M",
                qty=40,
                revision=3,
            ),
            Reservation(
                order_id="SO-1002",
                warehouse_id=WAREHOUSE_ID,
                sku_id="B-BLUE-L",
                qty=10,
                revision=2,
            ),
        ]
    )
    session.add_all(_movements())
    session.flush()
    session.add_all(
        [
            ProductionOrder(
                id="MO-2001",
                sku_id="C-BLACK-L",
                planned_qty=120,
                cut_qty=120,
                status="CUT",
                doc_revision=1,
            ),
            ProductionOrder(
                id="MO-2002",
                sku_id="C-BLACK-L",
                planned_qty=180,
                cut_qty=0,
                status="NOT_STARTED",
                doc_revision=1,
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            PurchaseOrder(
                id="PUR-3001",
                supplier_id="SUP-01",
                material_id="FAB-BLACK",
                production_order_id="MO-2001",
                ordered_qty=240,
                received_qty=240,
                status="RECEIVED",
                change_state="NONE",
                doc_revision=1,
            ),
            PurchaseOrder(
                id="PUR-3002",
                supplier_id="SUP-01",
                material_id="FAB-BLACK",
                production_order_id="MO-2002",
                ordered_qty=360,
                received_qty=0,
                status="PLACED",
                change_state="NONE",
                doc_revision=1,
            ),
        ]
    )
    session.flush()
    docs = [
        (
            "production_order",
            "MO-2001",
            {"sku_id": "C-BLACK-L", "planned_qty": 120, "cut_qty": 120, "status": "CUT"},
        ),
        (
            "production_order",
            "MO-2002",
            {"sku_id": "C-BLACK-L", "planned_qty": 180, "cut_qty": 0, "status": "NOT_STARTED"},
        ),
        (
            "purchase_order",
            "PUR-3001",
            {
                "material_id": "FAB-BLACK",
                "production_order_id": "MO-2001",
                "ordered_qty": 240,
                "received_qty": 240,
                "status": "RECEIVED",
                "change_state": "NONE",
            },
        ),
        (
            "purchase_order",
            "PUR-3002",
            {
                "material_id": "FAB-BLACK",
                "production_order_id": "MO-2002",
                "ordered_qty": 360,
                "received_qty": 0,
                "status": "PLACED",
                "change_state": "NONE",
            },
        ),
    ]
    for doc_type, doc_id, snapshot in docs:
        session.add(
            DocRevision(
                id=f"DR-{doc_id}-1",
                doc_type=doc_type,
                doc_id=doc_id,
                rev_no=1,
                snapshot_json=snapshot,
                operation_id=None,
            )
        )
        session.add(
            DownstreamLink(
                id=f"LNK-{doc_id}-1",
                doc_type=doc_type,
                doc_id=doc_id,
                order_id="SO-1001",
                order_revision=1,
                order_line_sku="C-BLACK-L",
                link_seq=1,
                operation_id=None,
            )
        )
    session.flush()


def _movements() -> list[StockMovement]:
    specs = [
        ("A-BLACK-M", "OP-SEED-A-BLACK-M", "RECEIPT", 200, 0, 0, 1),
        ("A-BLACK-M", "OP-SEED-A-BLACK-M", "RESERVE", 0, 100, 1, 2),
        ("A-BLACK-M", "OP-SEED-A-BLACK-M", "SHIP", -60, -60, 2, 3),
        ("A-WHITE-M", "OP-SEED-A-WHITE-M", "RECEIPT", 120, 0, 0, 1),
        ("B-BLUE-L", "OP-SEED-B-BLUE-L", "RECEIPT", 30, 0, 0, 1),
        ("B-BLUE-L", "OP-SEED-B-BLUE-L", "RESERVE", 0, 10, 1, 2),
    ]
    rows: list[StockMovement] = []
    seq_by_op: dict[str, int] = {}
    for sku_id, operation_id, movement_type, delta_on, delta_res, before, after in specs:
        seq_by_op[operation_id] = seq_by_op.get(operation_id, 0) + 1
        seq = seq_by_op[operation_id]
        rows.append(
            StockMovement(
                id=f"SM-{sku_id}-{seq}",
                operation_id=operation_id,
                seq=seq,
                movement_type=movement_type,
                warehouse_id=WAREHOUSE_ID,
                sku_id=sku_id,
                delta_on_hand=delta_on,
                delta_reserved=delta_res,
                before_revision=before,
                after_revision=after,
                at=SEED_AT,
            )
        )
    return rows
