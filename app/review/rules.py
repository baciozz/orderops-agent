"""Reuse the deterministic change kernel on imported, explicitly bounded facts."""

from app.domain.changes import SnapLine, evaluate_changes
from app.review.contracts import Proposal, Snapshot


def check_proposal(
    snapshot: Snapshot,
    selected: str,
    instruction: str,
    proposal: Proposal,
    *,
    human: bool = False,
    source_texts: list[str] | None = None,
) -> dict:
    issues: list[str] = []
    order = next((row for row in snapshot.orders if row.order_id == selected), None)
    if order is None or proposal.order_id != selected:
        return _blocked(["提案订单与人工选择不一致，请确认订单；不会自动改绑"])
    if proposal.disposition != "propose":
        if proposal.changes:
            return _blocked(["澄清或无需变更提案不能同时携带执行动作"])
        return {
            "status": (
                "clarify"
                if proposal.disposition == "clarify" or proposal.questions
                else "no_change"
            ),
            "issues": proposal.questions,
            "rows": [],
            "downstream": [],
            "scope": scope(snapshot, order.revision),
        }
    if proposal.questions or not proposal.changes:
        return _blocked(proposal.questions or ["没有明确变更动作，请补充需求"])
    catalog: dict[str, str] = {row.sku_id: row.fulfillment_mode for row in snapshot.catalog}
    current = {row.sku_id: row for row in order.lines}
    for sku, line in current.items():
        if line.shipped_qty is not None and line.shipped_qty > line.ordered_qty:
            issues.append(f"{sku} 已发量超过订购量，请核对快照")
        if line.cut_qty is not None and (
            line.cut_qty > line.ordered_qty
            or (
                catalog[sku] == "MTO"
                and line.shipped_qty is not None
                and line.shipped_qty > line.cut_qty
            )
        ):
            issues.append(f"{sku} 已裁/已发/订购快照矛盾，请核对")
        if catalog[sku] == "STOCK" and line.cut_qty not in (None, 0):
            issues.append(f"{sku} 现货行不应带生产已裁量，请核对商品履约方式")
    input_lines = instruction.splitlines() if source_texts is None else source_texts
    changes: list[dict[str, object]] = []
    no_effect: list[str] = []
    touched: set[str] = set()
    available = {row.sku_id: row.available_qty for row in snapshot.inventory}
    for change in proposal.changes:
        sku = change.sku_id
        if sku in touched or (change.to_sku and change.to_sku in touched):
            issues.append("同一 SKU 有多项重叠动作，请人工合并意图后重新核对")
        touched.add(sku)
        if change.to_sku:
            touched.add(change.to_sku)
        if change.unit != "件":
            issues.append("单位未明确换算为件，请确认每手/包的件数")
        if not human and (
            not change.citations
            or any(
                ref.line > len(input_lines) or ref.quote not in input_lines[ref.line - 1]
                for ref in change.citations
            )
        ):
            issues.append("模型来源无法对照原文行，请重新提取或人工纠正并注明原因")
        if sku not in current:
            issues.append("原订单不存在所选 SKU，请确认来源行")
            continue
        old = current[sku]
        if change.before_qty != old.ordered_qty:
            issues.append("模型旧数量与订单快照不一致，请纠正提案")
        expected = (
            change.quantity
            if change.mode == "set_total"
            else old.ordered_qty + change.quantity
            if change.mode == "increment" and change.quantity is not None
            else old.ordered_qty - change.quantity
            if change.mode == "replace_remaining" and change.quantity is not None
            else max(old.shipped_qty or 0, old.cut_qty or 0)
            if change.mode == "cancel_remaining"
            else None
        )
        if change.after_qty != expected:
            issues.append("提案新数量与数量语义不一致，请纠正")
        if change.mode == "cancel_remaining" and change.quantity is not None:
            issues.append("取消剩余不能另带猜测数量")
        if change.mode != "replace_remaining" and change.to_sku is not None:
            issues.append("非换款动作不能同时带目标 SKU")
        if change.mode == "replace_remaining" and change.to_sku == sku:
            issues.append("换款目标与原SKU相同，请确认目标款色码")
        if (
            change.to_sku is None
            and change.before_qty == old.ordered_qty
            and change.after_qty == old.ordered_qty
            and expected == old.ordered_qty
        ):
            no_effect.append(sku)
        if old.shipped_qty is None:
            issues.append(f"{sku} 已发量未知；未知不等于零，请更新履约快照")
        if catalog[sku] == "MTO" and old.cut_qty is None:
            issues.append(f"{sku} 已裁量未知，请更新生产快照")
        for changed_sku in (sku, change.to_sku):
            if changed_sku and catalog.get(changed_sku) == "STOCK":
                if available.get(changed_sku) is None:
                    issues.append(f"{changed_sku} 可用库存未知，请补充库存快照")
        # The existing kernel's increment means positive addition only. Normalize
        # an explicit signed reduction to its checked total without changing the
        # raw customer/model quantity semantics retained above.
        reducing = (
            change.mode == "increment" and change.quantity is not None and change.quantity < 0
        )
        changes.append(
            {
                "mode": "set_total" if reducing else change.mode,
                "sku_id": sku,
                "from_sku": sku,
                "to_sku": change.to_sku or "",
                "qty": expected if reducing else change.quantity,
            }
        )
    for sku in touched:
        if catalog.get(sku) != "MTO":
            continue
        docs = [doc for doc in snapshot.documents if doc.order_id == selected and doc.sku_id == sku]
        if {doc.kind for doc in docs} != {"MO", "PO"}:
            issues.append(f"{sku} 生产/采购关联快照不完整，不能认定可执行")
        if any(doc.order_revision != order.revision for doc in docs):
            issues.append(f"{sku} 单据关联版本与订单快照不同，请更新快照")
    if issues:
        return _blocked(issues)
    evaluated = evaluate_changes(
        lines={
            sku: SnapLine(
                sku, line.ordered_qty, catalog[sku], line.shipped_qty or 0, line.cut_qty or 0
            )
            for sku, line in current.items()
        },
        changes=changes,
        stock_available={sku: qty for sku, qty in available.items() if qty is not None},
        known_skus=catalog,
        candidates_for={},
    )
    if evaluated.kind != "validated":
        return _blocked(
            evaluated.questions or evaluated.issues or [evaluated.message or "业务核对未通过"]
        )
    rows = []
    for sku in sorted(set(current) | set(evaluated.lines)):
        old_qty = current[sku].ordered_qty if sku in current else 0
        new_qty = evaluated.lines[sku].ordered_qty if sku in evaluated.lines else 0
        if old_qty != new_qty:
            catalog_index = next(i for i, item in enumerate(snapshot.catalog) if item.sku_id == sku)
            rows.append(
                {
                    "sku_id": sku,
                    "old_qty": old_qty,
                    "new_qty": new_qty,
                    "delta": new_qty - old_qty,
                    "baseline_source": (
                        f"snapshot:/orders/{snapshot.orders.index(order)}/lines/"
                        f"{list(current).index(sku)}/ordered_qty"
                        if sku in current
                        else "新增行，原数量0为规则计算；目录定位 "
                        f"snapshot:/catalog/{catalog_index}"
                    ),
                    "origin": "人工纠正" if human else "模型提取",
                }
            )
    if not rows:
        return _blocked(["提案未产生数量变化；请核对是否无需变更或纠正冗余动作"])
    downstream = [
        f"{sku} 库存预占建议变化 {qty:+d} 件；未实际预占"
        for sku, qty in sorted(evaluated.reservation_delta.items())
    ]
    if any(catalog.get(sku) == "MTO" for sku in touched):
        downstream.append("生产/采购需人工逐单确认；本入口不修改下游单据，供应商未确认不等于同意")
    result = {
        "status": "ready",
        "issues": [],
        "rows": rows,
        "downstream": downstream,
        "scope": scope(snapshot, order.revision),
    }
    if no_effect:
        result["warnings"] = [
            f"{sku} 的动作未产生数量变化；原始提案保留，请核对客户是否要求此动作。"
            for sku in no_effect
        ]
    return result


def scope(snapshot: Snapshot, revision: int) -> str:
    return (
        f"仅依据导入快照 {snapshot.captured_at} / 修订 {revision} 核对；"
        "未连接 ERP，未验证完整来源、父版本和共享依赖，未预占库存或提交订单。"
    )


def _blocked(issues: list[str]) -> dict:
    return {
        "status": "blocked",
        "issues": list(dict.fromkeys(issues)),
        "rows": [],
        "downstream": [],
        "scope": "核对未通过；不能确认处理清单，未执行业务。",
    }
