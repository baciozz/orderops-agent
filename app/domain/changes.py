"""Apply structured line changes to an in-memory order snapshot.

MTO line *changes* are stage 1B. Unchanged MTO lines are copied through.
"""

from dataclasses import dataclass, field

from schemas.enums import FulfillmentMode, LineMode

from app.errors import AppError


@dataclass
class SnapLine:
    sku_id: str
    ordered_qty: int
    fulfillment_mode: str
    shipped_qty: int = 0


@dataclass
class ChangeEval:
    kind: str
    lines: dict[str, SnapLine] = field(default_factory=dict)
    reservation_delta: dict[str, int] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    shortages: dict[str, int] = field(default_factory=dict)
    code: str | None = None
    message: str | None = None


def evaluate_changes(
    *,
    lines: dict[str, SnapLine],
    changes: list[dict[str, object]],
    stock_available: dict[str, int],
    known_skus: dict[str, str],
    candidates_for: dict[str, list[str]],
) -> ChangeEval:
    """known_skus maps sku_id to fulfillment mode. candidates_for is for unknown ids."""
    working = {
        sku: SnapLine(sku, line.ordered_qty, line.fulfillment_mode, line.shipped_qty)
        for sku, line in lines.items()
    }
    for change in changes:
        mode = str(change.get("mode") or "")
        qty = change.get("qty")
        if mode == LineMode.CANCEL_REMAINING:
            sku_id = str(change.get("sku_id") or change.get("from_sku") or "")
            problem = _cancel(working, sku_id)
        else:
            if not isinstance(qty, int) or isinstance(qty, bool):
                return _reject("数量必须是整数", "BUSINESS_CONFLICT")
            if mode == LineMode.REPLACE_REMAINING:
                problem = _replace(
                    working,
                    from_sku=str(change.get("from_sku") or ""),
                    to_sku=str(change.get("to_sku") or ""),
                    qty=qty,
                    known_skus=known_skus,
                    candidates_for=candidates_for,
                )
            elif mode == LineMode.SET_TOTAL:
                problem = _set_total(
                    working,
                    sku_id=str(change.get("sku_id") or change.get("to_sku") or ""),
                    qty=qty,
                    known_skus=known_skus,
                    candidates_for=candidates_for,
                )
            elif mode == LineMode.INCREMENT:
                problem = _increment(
                    working,
                    sku_id=str(change.get("sku_id") or change.get("to_sku") or ""),
                    qty=qty,
                    known_skus=known_skus,
                    candidates_for=candidates_for,
                )
            else:
                return _reject(f"未知变更模式 {mode}", "BUSINESS_CONFLICT")
        if isinstance(problem, ChangeEval):
            return problem
    for line in working.values():
        if line.fulfillment_mode == FulfillmentMode.STOCK and line.ordered_qty < line.shipped_qty:
            return _reject(
                f"{line.sku_id} 的订购量不能低于已发货 {line.shipped_qty}",
                "BUSINESS_CONFLICT",
            )
        if line.fulfillment_mode == FulfillmentMode.MTO and _touched(line.sku_id, lines, line):
            return _reject("MTO 行的变更属于阶段 1B", "STAGE_1B_REQUIRED")
    deltas: dict[str, int] = {}
    skus = set(lines) | set(working)
    for sku in skus:
        old = lines.get(sku)
        new = working.get(sku)
        old_res = _reserved(old)
        new_res = _reserved(new)
        if new_res != old_res:
            deltas[sku] = new_res - old_res
    shortages: dict[str, int] = {}
    for sku, delta in deltas.items():
        if delta > stock_available.get(sku, 0):
            shortages[sku] = delta - stock_available.get(sku, 0)
    if shortages:
        parts = [f"{sku} 缺口 {qty}" for sku, qty in sorted(shortages.items())]
        return ChangeEval(
            kind="shortage",
            lines=working,
            reservation_delta=deltas,
            shortages=shortages,
            code="SHORTAGE",
            message="整单拒绝，只生成采购建议：" + "，".join(parts),
            issues=["整单拒绝，不预占、不产生负库存"],
        )
    return ChangeEval(kind="validated", lines=working, reservation_delta=deltas)


def _touched(sku_id: str, before: dict[str, SnapLine], after: SnapLine) -> bool:
    old = before.get(sku_id)
    if old is None:
        return True
    return old.ordered_qty != after.ordered_qty


def _reserved(line: SnapLine | None) -> int:
    if line is None or line.fulfillment_mode != FulfillmentMode.STOCK:
        return 0
    return line.ordered_qty - line.shipped_qty


def _reject(message: str, code: str) -> ChangeEval:
    return ChangeEval(kind="reject", code=code, message=message, issues=[message])


def _clarify(message: str, candidates: list[str]) -> ChangeEval:
    return ChangeEval(
        kind="clarify",
        message=message,
        questions=[message],
        candidates=candidates,
        issues=[message],
    )


def _require_sku(
    sku_id: str,
    known_skus: dict[str, str],
    candidates_for: dict[str, list[str]],
) -> ChangeEval | str:
    if not sku_id or sku_id not in known_skus:
        found = candidates_for.get(sku_id, [])
        return _clarify(f"未知 SKU {sku_id or '（空）'}，请从候选中选择", found)
    if known_skus[sku_id] == FulfillmentMode.MTO:
        return _reject("MTO 行的变更属于阶段 1B", "STAGE_1B_REQUIRED")
    return known_skus[sku_id]


def _replace(
    working: dict[str, SnapLine],
    *,
    from_sku: str,
    to_sku: str,
    qty: int,
    known_skus: dict[str, str],
    candidates_for: dict[str, list[str]],
) -> ChangeEval | None:
    if qty <= 0:
        return _reject("replace_remaining 的数量必须是正整数", "BUSINESS_CONFLICT")
    source = working.get(from_sku)
    if source is None:
        return _clarify(f"订单上没有 {from_sku}", candidates_for.get(from_sku, []))
    if source.fulfillment_mode == FulfillmentMode.MTO:
        return _reject("MTO 行的变更属于阶段 1B", "STAGE_1B_REQUIRED")
    unexecuted = source.ordered_qty - source.shipped_qty
    if qty > unexecuted:
        return _reject(
            f"已发货的 {source.shipped_qty} 件不能改，{from_sku} 未发货只有 {unexecuted}",
            "EXECUTED_PORTION_IMMUTABLE",
        )
    target_mode = _require_sku(to_sku, known_skus, candidates_for)
    if isinstance(target_mode, ChangeEval):
        return target_mode
    source.ordered_qty -= qty
    target = working.get(to_sku)
    if target is None:
        working[to_sku] = SnapLine(to_sku, qty, target_mode, 0)
    else:
        if target.fulfillment_mode != FulfillmentMode.STOCK:
            return _reject("不能把现货改到 MTO 行上", "STAGE_1B_REQUIRED")
        target.ordered_qty += qty
    if source.ordered_qty == 0 and source.shipped_qty == 0:
        del working[from_sku]
    return None


def _set_total(
    working: dict[str, SnapLine],
    *,
    sku_id: str,
    qty: int,
    known_skus: dict[str, str],
    candidates_for: dict[str, list[str]],
) -> ChangeEval | None:
    if qty < 0:
        return _reject("总量不能为负", "BUSINESS_CONFLICT")
    current = working.get(sku_id)
    if current is not None and current.fulfillment_mode == FulfillmentMode.MTO:
        return _reject("MTO 行的变更属于阶段 1B", "STAGE_1B_REQUIRED")
    shipped = current.shipped_qty if current else 0
    if qty < shipped:
        return _reject(
            f"{sku_id} 总量 {qty} 低于已发货 {shipped}",
            "BUSINESS_CONFLICT",
        )
    mode = _require_sku(sku_id, known_skus, candidates_for)
    if isinstance(mode, ChangeEval):
        return mode
    if qty == 0 and shipped == 0:
        working.pop(sku_id, None)
        return None
    working[sku_id] = SnapLine(sku_id, qty, mode, shipped)
    return None


def _increment(
    working: dict[str, SnapLine],
    *,
    sku_id: str,
    qty: int,
    known_skus: dict[str, str],
    candidates_for: dict[str, list[str]],
) -> ChangeEval | None:
    if qty <= 0:
        return _reject("增加的数量必须是正整数", "BUSINESS_CONFLICT")
    current = working.get(sku_id)
    if current is not None and current.fulfillment_mode == FulfillmentMode.MTO:
        return _reject("MTO 行的变更属于阶段 1B", "STAGE_1B_REQUIRED")
    mode = _require_sku(sku_id, known_skus, candidates_for)
    if isinstance(mode, ChangeEval):
        return mode
    shipped = current.shipped_qty if current else 0
    ordered = (current.ordered_qty if current else 0) + qty
    working[sku_id] = SnapLine(sku_id, ordered, mode, shipped)
    return None


def _cancel(working: dict[str, SnapLine], sku_id: str) -> ChangeEval | None:
    current = working.get(sku_id)
    if current is None:
        return _reject(f"订单上没有 {sku_id}", "BUSINESS_CONFLICT")
    if current.fulfillment_mode == FulfillmentMode.MTO:
        return _reject("MTO 行的变更属于阶段 1B", "STAGE_1B_REQUIRED")
    current.ordered_qty = current.shipped_qty
    if current.ordered_qty == 0:
        del working[sku_id]
    return None


def business_error(evaluation: ChangeEval) -> AppError | None:
    if evaluation.kind != "reject":
        return None
    return AppError(
        evaluation.message or "业务冲突",
        code=evaluation.code or "BUSINESS_CONFLICT",
        http_status=422,
        details={"issues": evaluation.issues},
    )
