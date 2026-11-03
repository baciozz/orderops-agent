"""Impact of an order-line change on linked production and purchase orders.

Fully cut or fully received documents are not rewritten. Uncut production and
unreceived fabric can take a change draft. Stage 1B does not open a new
production order to cover leftover quantity.
"""

from typing import Any

from schemas.enums import FulfillmentMode

from app.domain.changes import SnapLine

UNAFFECTED = "UNAFFECTED_RELINK"
CHANGE = "CHANGE_DRAFT"
CANCEL = "CANCEL_DRAFT"


def assess_downstream(
    *,
    new_lines: dict[str, SnapLine],
    production: list[dict[str, Any]],
    purchase: list[dict[str, Any]],
    usage: dict[str, tuple[str, int]],
    reserved_purchase_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return verdicts, or an error message when the change cannot be covered."""
    demand = {
        sku: line.ordered_qty
        for sku, line in new_lines.items()
        if line.fulfillment_mode == FulfillmentMode.MTO and line.ordered_qty > 0
    }
    active = [row for row in production if row["status"] != "CANCELLED"]
    full = [
        row
        for row in active
        if int(row["cut_qty"]) > 0 and int(row["cut_qty"]) == int(row["planned_qty"])
    ]
    partial = [row for row in active if 0 < int(row["cut_qty"]) < int(row["planned_qty"])]
    zero = [row for row in active if int(row["cut_qty"]) == 0]
    verdicts: list[dict[str, Any]] = []
    assigned: dict[str, str] = {}

    def take(sku: str, qty: int) -> bool:
        if demand.get(sku, 0) < qty:
            return False
        demand[sku] -= qty
        if demand[sku] == 0:
            del demand[sku]
        return True

    for row in sorted(full, key=lambda item: str(item["doc_id"])):
        sku = str(row["sku_id"])
        planned = int(row["planned_qty"])
        if not take(sku, planned):
            return [], "已裁剪的生产单无法保持在原 SKU 上"
        verdicts.append(_unchanged_mo(row))
        assigned[str(row["doc_id"])] = sku

    for row in sorted(partial, key=lambda item: str(item["doc_id"])):
        sku = str(row["sku_id"])
        cut = int(row["cut_qty"])
        if not take(sku, cut):
            return [], "已裁剪数量高于该 SKU 的新订购量"
        extra = min(int(row["planned_qty"]) - cut, demand.get(sku, 0))
        if extra:
            take(sku, extra)
        planned = cut + extra
        if planned == int(row["planned_qty"]):
            verdicts.append(_unchanged_mo(row))
        else:
            status = "CUT" if planned == cut else "PARTIALLY_CUT"
            verdicts.append(_change_mo(row, sku, planned, status))
        assigned[str(row["doc_id"])] = sku

    for row in sorted(zero, key=lambda item: str(item["doc_id"])):
        sku = str(row["sku_id"])
        planned = int(row["planned_qty"])
        if demand.get(sku, 0) >= planned:
            take(sku, planned)
            verdicts.append(_unchanged_mo(row))
            assigned[str(row["doc_id"])] = sku
            continue
        target = next((item for item in sorted(demand) if demand[item] >= planned), None)
        if target is not None:
            take(target, planned)
            verdicts.append(_change_mo(row, target, planned, "NOT_STARTED"))
            assigned[str(row["doc_id"])] = target
            continue
        host = sku if demand.get(sku, 0) > 0 else None
        if host is None and demand:
            host = max(demand, key=lambda item: demand[item])
        if host is not None and demand.get(host, 0) > 0:
            kept = demand[host]
            take(host, kept)
            verdicts.append(_change_mo(row, host, kept, "NOT_STARTED"))
            assigned[str(row["doc_id"])] = host
            continue
        verdicts.append(_cancel_mo(row))
        assigned[str(row["doc_id"])] = sku

    if demand:
        return [], "P0 不支持为剩余数量追加生产单"

    by_id = {str(item["doc_id"]): item for item in verdicts}
    purchases_by_mo: dict[str, list[dict[str, Any]]] = {}
    occupied = {str(row["doc_id"]) for row in purchase} | (reserved_purchase_ids or set())
    for row in sorted(purchase, key=lambda item: str(item["doc_id"])):
        if row["status"] == "CANCELLED":
            continue
        mo_id = str(row["production_order_id"])
        if mo_id not in by_id:
            return [], f"采购单 {row['doc_id']} 没有对应的生产单"
        purchases_by_mo.setdefault(mo_id, []).append(row)
    for mo_id, rows in sorted(purchases_by_mo.items()):
        mo = by_id[mo_id]
        new_sku = assigned[mo_id]
        material = usage.get(new_sku)
        if material is None:
            return [], f"{new_sku} 没有面料单耗"
        material_id, per_piece = material
        new_qty = int(mo["after"]["planned_qty"]) * per_piece
        po_verdicts, error = _assess_purchases(rows, mo, new_sku, material_id, new_qty, occupied)
        if error:
            return [], error
        verdicts.extend(po_verdicts)
    return verdicts, None


def _assess_purchases(
    rows: list[dict[str, Any]],
    mo: dict[str, Any],
    sku: str,
    material_id: str,
    needed: int,
    occupied: set[str],
) -> tuple[list[dict[str, Any]], str | None]:
    """Allocate one MO's fabric need once, preserving every fully received PO."""
    for row in rows:
        received = int(row["received_qty"])
        if mo["verdict"] == CANCEL and received:
            return [], f"{row['doc_id']} 已到料，不能取消"
        if received and material_id != row["material_id"]:
            return [], f"{row['doc_id']} 已到料部分不能改面料或减量"
    if mo["verdict"] == CANCEL:
        return [_cancel_po(row, sku) for row in rows], None

    fixed = [row for row in rows if row["status"] == "RECEIVED"]
    adjustable = sorted(
        [row for row in rows if row["status"] != "RECEIVED"],
        key=lambda row: (
            int(row["received_qty"]) > 0,
            not str(row["doc_id"]).rpartition("-S")[2].isdigit(),
            str(row["doc_id"]),
        ),
    )
    floor = sum(int(row["received_qty"]) for row in rows)
    if needed < floor:
        locked = next(row for row in rows if int(row["received_qty"]) > 0)
        return [], f"{locked['doc_id']} 已到料部分不能改面料或减量"
    quantities = {str(row["doc_id"]): int(row["ordered_qty"]) for row in adjustable}
    delta = needed - sum(int(row["ordered_qty"]) for row in rows)
    if delta > 0 and adjustable:
        quantities[str(adjustable[0]["doc_id"])] += delta
    elif delta < 0:
        for row in adjustable:
            doc_id = str(row["doc_id"])
            reduction = min(-delta, quantities[doc_id] - int(row["received_qty"]))
            quantities[doc_id] -= reduction
            delta += reduction

    verdicts = [_unchanged_po(row, sku) for row in fixed]
    for row in adjustable:
        qty = quantities[str(row["doc_id"])]
        if qty == 0 and int(row["ordered_qty"]) > 0:
            verdicts.append(_cancel_po(row, sku))
        elif (
            material_id == row["material_id"]
            and qty == int(row["ordered_qty"])
            and mo["verdict"] == UNAFFECTED
        ):
            verdicts.append(_unchanged_po(row, sku))
        else:
            verdicts.append(_change_po(row, sku, material_id, qty))
    if delta > 0 and not adjustable:
        # All active POs are fully received. Cancelled IDs remain reserved.
        anchor = fixed[0]
        suffix = 1
        while f"{anchor['doc_id']}-S{suffix}" in occupied:
            suffix += 1
        doc_id = f"{anchor['doc_id']}-S{suffix}"
        occupied.add(doc_id)
        verdicts.append(
            {
                "doc_type": "purchase_order",
                "doc_id": doc_id,
                "verdict": CHANGE,
                "before": None,
                "after": {
                    "material_id": material_id,
                    "ordered_qty": delta,
                    "received_qty": 0,
                    "status": "PLACED",
                    "change_state": "PENDING_SUPPLIER",
                },
                "production_order_id": anchor["production_order_id"],
                "supplier_id": anchor["supplier_id"],
                "order_line_sku": sku,
                "reason": "已收货采购单保持不变，仅为面料缺口生成补购草稿",
            }
        )
    return verdicts, None


def locked_preview(
    production: list[dict[str, Any]],
    purchase: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Classify documents when a color change overlaps the cut or received part."""
    changeable_mos = {
        str(row["doc_id"])
        for row in production
        if row["status"] != "CANCELLED" and int(row["cut_qty"]) == 0
    }
    preview: list[dict[str, Any]] = []
    for row in sorted(production, key=lambda item: str(item["doc_id"])):
        if row["status"] == "CANCELLED":
            continue
        locked = str(row["doc_id"]) not in changeable_mos
        preview.append(
            {
                "doc_id": str(row["doc_id"]),
                "doc_type": "production_order",
                "verdict": "EXECUTED_LOCKED" if locked else "CHANGEABLE",
            }
        )
    for row in sorted(purchase, key=lambda item: str(item["doc_id"])):
        if row["status"] == "CANCELLED":
            continue
        received = (
            int(row["received_qty"]) >= int(row["ordered_qty"]) and int(row["ordered_qty"]) > 0
        )
        mo_locked = str(row["production_order_id"]) not in changeable_mos
        preview.append(
            {
                "doc_id": str(row["doc_id"]),
                "doc_type": "purchase_order",
                "verdict": "EXECUTED_LOCKED" if received or mo_locked else "CHANGEABLE",
            }
        )
    return preview


def _before_mo(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sku_id": row["sku_id"],
        "planned_qty": int(row["planned_qty"]),
        "cut_qty": int(row["cut_qty"]),
        "status": row["status"],
    }


def _unchanged_mo(row: dict[str, Any]) -> dict[str, Any]:
    body = _before_mo(row)
    return {
        "doc_type": "production_order",
        "doc_id": row["doc_id"],
        "verdict": UNAFFECTED,
        "before": body,
        "after": body,
        "order_line_sku": row["sku_id"],
        "reason": "内容不变，只重新链接",
    }


def _change_mo(row: dict[str, Any], sku: str, planned: int, status: str) -> dict[str, Any]:
    return {
        "doc_type": "production_order",
        "doc_id": row["doc_id"],
        "verdict": CHANGE,
        "before": _before_mo(row),
        "after": {
            "sku_id": sku,
            "planned_qty": planned,
            "cut_qty": int(row["cut_qty"]),
            "status": status,
        },
        "order_line_sku": sku,
        "reason": "未裁剪部分改到新 SKU 或新计划量",
    }


def _cancel_mo(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "doc_type": "production_order",
        "doc_id": row["doc_id"],
        "verdict": CANCEL,
        "before": _before_mo(row),
        "after": {**_before_mo(row), "status": "CANCELLED", "planned_qty": int(row["cut_qty"])},
        "order_line_sku": row["sku_id"],
        "reason": "未执行的生产单取消",
    }


def _before_po(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "material_id": row["material_id"],
        "ordered_qty": int(row["ordered_qty"]),
        "received_qty": int(row["received_qty"]),
        "status": row["status"],
        "change_state": row["change_state"],
    }


def _unchanged_po(row: dict[str, Any], garment_sku: str) -> dict[str, Any]:
    body = _before_po(row)
    return {
        "doc_type": "purchase_order",
        "doc_id": row["doc_id"],
        "verdict": UNAFFECTED,
        "before": body,
        "after": body,
        "order_line_sku": garment_sku,
        "reason": "内容不变，只重新链接",
    }


def _change_po(
    row: dict[str, Any],
    garment_sku: str,
    material_id: str,
    ordered_qty: int,
) -> dict[str, Any]:
    received_qty = int(row["received_qty"])
    # Receipt status follows quantities; the reduction still awaits supplier confirmation.
    status = "RECEIVED" if ordered_qty == received_qty > 0 else row["status"]
    return {
        "doc_type": "purchase_order",
        "doc_id": row["doc_id"],
        "verdict": CHANGE,
        "before": _before_po(row),
        "after": {
            "material_id": material_id,
            "ordered_qty": ordered_qty,
            "received_qty": received_qty,
            "status": status,
            "change_state": "PENDING_SUPPLIER",
        },
        "order_line_sku": garment_sku,
        "reason": "未到料部分改面料或数量，等待供应商确认",
    }


def _cancel_po(row: dict[str, Any], garment_sku: str) -> dict[str, Any]:
    return {
        "doc_type": "purchase_order",
        "doc_id": row["doc_id"],
        "verdict": CANCEL,
        "before": _before_po(row),
        "after": {
            "material_id": row["material_id"],
            "ordered_qty": int(row["ordered_qty"]),
            "received_qty": int(row["received_qty"]),
            "status": "CANCELLED",
            "change_state": "PENDING_SUPPLIER",
        },
        "order_line_sku": garment_sku,
        "reason": "未到料采购取消，等待供应商确认",
    }
