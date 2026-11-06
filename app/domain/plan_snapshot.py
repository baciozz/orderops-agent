"""Pure approval snapshots, independent of persistence and orchestration."""

from typing import Any

from app.domain.changes import ChangeEval


def build_plan_payload(
    *,
    plan_kind: str,
    order_id: str | None,
    customer_id: str,
    base_revision: int,
    intent_revision: int,
    evaluation: ChangeEval,
    stock_revisions: dict[str, int],
    verdicts: list[dict[str, object]] | None = None,
    expected_docs: dict[str, dict[str, object]] | None = None,
    sheet_order_v1: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "plan_kind": plan_kind,
        "order_id": order_id,
        "customer_id": customer_id,
        "base_revision": base_revision,
        "intent_revision": intent_revision,
        "lines": [
            {
                "sku_id": line.sku_id,
                "ordered_qty": line.ordered_qty,
                "fulfillment_mode": line.fulfillment_mode,
            }
            for line in sorted(evaluation.lines.values(), key=lambda item: item.sku_id)
        ],
        "reservation_delta": [
            {"sku_id": sku, "qty": evaluation.reservation_delta[sku]}
            for sku in sorted(evaluation.reservation_delta)
        ],
        "stock_revisions": {sku: stock_revisions[sku] for sku in sorted(stock_revisions)},
        "downstream": sorted(verdicts or [], key=lambda item: str(item["doc_id"])),
        "expected_doc_revisions": expected_docs or {},
    }

    # Omission deliberately preserves every legacy authorization hash byte.
    if sheet_order_v1 is not None:
        payload["sheet_order_v1"] = sheet_order_v1
    return payload


def document_fingerprint(
    documents: dict[str, list[dict[str, Any]]],
    verdicts: list[dict[str, object]] | None = None,
) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for row in documents["production"]:
        found[str(row["doc_id"])] = {
            "doc_type": "production_order",
            "doc_revision": int(row["doc_revision"]),
            "status": row["status"],
            "cut_qty": int(row["cut_qty"]),
            "planned_qty": int(row["planned_qty"]),
            "sku_id": row["sku_id"],
        }
    for row in documents["purchase"]:
        found[str(row["doc_id"])] = {
            "doc_type": "purchase_order",
            "doc_revision": int(row["doc_revision"]),
            "status": row["status"],
            "received_qty": int(row["received_qty"]),
            "ordered_qty": int(row["ordered_qty"]),
            "change_state": row["change_state"],
            "material_id": row["material_id"],
            "supplier_id": row["supplier_id"],
            "production_order_id": row["production_order_id"],
        }
    for verdict in verdicts or []:
        if verdict["before"] is None:
            found[str(verdict["doc_id"])] = {"doc_type": "purchase_order", "exists": False}
    return found
