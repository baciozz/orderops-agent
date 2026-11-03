"""Apply structured line changes to an in-memory order snapshot.

MTO line *changes* are stage 1B. Unchanged MTO lines are copied through.
"""

from dataclasses import dataclass, field

from schemas.enums import FulfillmentMode, LineMode

from app.errors import AppError

ZERO_QUANTITY_MESSAGE = (
    "数量 0 不表示取消该行；取消请走显式 cancel_remaining 流程，仅取消未执行部分"
)


@dataclass
class SnapLine:
    sku_id: str
    ordered_qty: int
    fulfillment_mode: str
    shipped_qty: int = 0
    cut_qty: int = 0


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
        sku: SnapLine(
            sku,
            line.ordered_qty,
            line.fulfillment_mode,
            line.shipped_qty,
            line.cut_qty,
        )
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
                    include_executed=bool(change.get("include_executed")),
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
        if line.fulfillment_mode == FulfillmentMode.MTO and line.ordered_qty < line.cut_qty:
            return _reject(
                f"{line.sku_id} 总量低于已裁剪 {line.cut_qty}，违反不变量 6",
                "INVARIANT_6",
            )
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


def _floor(line: SnapLine) -> int:
    if line.fulfillment_mode == FulfillmentMode.MTO:
        return line.cut_qty
    return line.shipped_qty


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
    return known_skus[sku_id]


def _replace(
    working: dict[str, SnapLine],
    *,
    from_sku: str,
    to_sku: str,
    qty: int,
    known_skus: dict[str, str],
    candidates_for: dict[str, list[str]],
    include_executed: bool,
) -> ChangeEval | None:
    if qty == 0:
        return _reject(ZERO_QUANTITY_MESSAGE, "BUSINESS_CONFLICT")
    if qty < 0:
        return _reject("replace_remaining 的数量必须是正整数", "BUSINESS_CONFLICT")
    source = working.get(from_sku)
    if source is None:
        return _clarify(f"订单上没有 {from_sku}", candidates_for.get(from_sku, []))
    locked = _floor(source)
    unexecuted = source.ordered_qty - locked
    if qty > unexecuted:
        if include_executed or source.fulfillment_mode == FulfillmentMode.STOCK:
            noun = "已裁剪" if source.fulfillment_mode == FulfillmentMode.MTO else "已发货"
            return _reject(
                f"{noun}的 {locked} 件不能改，{from_sku} 未执行只有 {unexecuted}",
                "EXECUTED_PORTION_IMMUTABLE",
            )
        return ChangeEval(
            kind="clarify",
            code="LOCKED_PORTION",
            message=f"未执行量只有 {unexecuted}",
            questions=[f"未执行量只有 {unexecuted}"],
            issues=[f"未执行量只有 {unexecuted}"],
        )
    target_mode = _require_sku(to_sku, known_skus, candidates_for)
    if isinstance(target_mode, ChangeEval):
        return target_mode
    if target_mode != source.fulfillment_mode:
        return _reject("不能在现货和按单生产之间改码", "BUSINESS_CONFLICT")
    source.ordered_qty -= qty
    target = working.get(to_sku)
    if target is None:
        working[to_sku] = SnapLine(to_sku, qty, target_mode, 0, cut_qty=0)
    else:
        target.ordered_qty += qty
    if source.ordered_qty == 0 and locked == 0:
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
    if qty == 0:
        return _reject(ZERO_QUANTITY_MESSAGE, "BUSINESS_CONFLICT")
    if qty < 0:
        return _reject("总量不能为负", "BUSINESS_CONFLICT")
    current = working.get(sku_id)
    shipped = current.shipped_qty if current else 0
    cut = current.cut_qty if current else 0
    if current is not None and current.fulfillment_mode == FulfillmentMode.MTO and qty < cut:
        return _reject(
            f"{sku_id} 总量 {qty} 低于已裁剪 {cut}，违反不变量 6",
            "INVARIANT_6",
        )
    if qty < shipped:
        return _reject(
            f"{sku_id} 总量 {qty} 低于已发货 {shipped}",
            "BUSINESS_CONFLICT",
        )
    mode = _require_sku(sku_id, known_skus, candidates_for)
    if isinstance(mode, ChangeEval):
        return mode
    if current is not None and mode != current.fulfillment_mode:
        return _reject("不能改变履约方式", "BUSINESS_CONFLICT")
    working[sku_id] = SnapLine(sku_id, qty, mode, shipped, cut_qty=cut)
    return None


def _increment(
    working: dict[str, SnapLine],
    *,
    sku_id: str,
    qty: int,
    known_skus: dict[str, str],
    candidates_for: dict[str, list[str]],
) -> ChangeEval | None:
    if qty == 0:
        return _reject(ZERO_QUANTITY_MESSAGE, "BUSINESS_CONFLICT")
    if qty < 0:
        return _reject("增加的数量必须是正整数", "BUSINESS_CONFLICT")
    current = working.get(sku_id)
    mode = _require_sku(sku_id, known_skus, candidates_for)
    if isinstance(mode, ChangeEval):
        return mode
    if current is not None and mode != current.fulfillment_mode:
        return _reject("不能改变履约方式", "BUSINESS_CONFLICT")
    shipped = current.shipped_qty if current else 0
    cut = current.cut_qty if current else 0
    ordered = (current.ordered_qty if current else 0) + qty
    working[sku_id] = SnapLine(sku_id, ordered, mode, shipped, cut_qty=cut)
    return None


def _cancel(working: dict[str, SnapLine], sku_id: str) -> ChangeEval | None:
    current = working.get(sku_id)
    if current is None:
        return _reject(f"订单上没有 {sku_id}", "BUSINESS_CONFLICT")
    current.ordered_qty = _floor(current)
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
