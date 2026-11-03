"""Exact-string fixtures for the parser. A miss does not call a live model.

These keys are the golden utterances the dispatcher recognizes in fixture mode.
Sheet comparison supports only the explicit static column layouts below.
"""

from openpyxl.utils.cell import coordinate_from_string
from schemas.business import BusinessEvent, ExtractionResult, LineChange, NewOrderLine
from schemas.common import SourceRef
from schemas.enums import BusinessEventType, InputMode, LineMode

TEXT_BLACK_TO_WHITE = "SO-1001 把未发的 40 件黑色 M 改成白色 M"
TEXT_ALL_NAVY = "C 款黑色 L 全部改藏青"
TEXT_ONLY_UNCUT = "只改未开裁"
TEXT_NEW_ORDER = "新建订单 华东服饰 A 款白 M 30"
TEXT_BAD_SCHEMA = "SCHEMA-BROKEN"
TEXT_ATTACH_SHEET = "先上传订单表，再核对聊天数量"


def _source(text: str) -> SourceRef:
    return SourceRef(source_id="SRC-INPUT", line_start=1, line_end=1, text_excerpt=text)


def _extraction(
    text: str,
    event: BusinessEvent,
    *,
    customer: str | None = None,
    orders: list[str] | None = None,
) -> dict[str, object]:
    extracted = ExtractionResult(
        task_id="PENDING",
        intent_revision=0,
        input_mode=InputMode.TEXT,
        order_candidates=orders or [],
        customer_candidate=customer,
        line_changes=list(event.line_changes),
        new_lines=list(event.new_lines),
        source_refs=[_source(text)],
        business_event=event,
    )
    payload = extracted.model_dump(mode="json")
    payload.pop("task_id")
    payload.pop("intent_revision")
    return payload


def golden_fixtures() -> dict[str, dict[str, object]]:
    stock = BusinessEvent(
        event_type=BusinessEventType.CHANGE_ORDER,
        order_id="SO-1001",
        line_changes=[
            LineChange(
                mode=LineMode.REPLACE_REMAINING,
                from_sku="A-BLACK-M",
                to_sku="A-WHITE-M",
                qty=40,
                source_refs=[_source(TEXT_BLACK_TO_WHITE)],
            )
        ],
        source_refs=[_source(TEXT_BLACK_TO_WHITE)],
    )
    navy = BusinessEvent(
        event_type=BusinessEventType.CHANGE_ORDER,
        order_id="SO-1001",
        line_changes=[
            LineChange(
                mode=LineMode.REPLACE_REMAINING,
                from_sku="C-BLACK-L",
                to_sku="C-NAVY-L",
                qty=300,
                source_refs=[_source(TEXT_ALL_NAVY)],
            )
        ],
        source_refs=[_source(TEXT_ALL_NAVY)],
    )
    uncut = BusinessEvent(
        event_type=BusinessEventType.CHANGE_ORDER,
        order_id="SO-1001",
        clarification_choice="only_uncut",
        line_changes=[
            LineChange(
                mode=LineMode.REPLACE_REMAINING,
                from_sku="C-BLACK-L",
                to_sku="C-NAVY-L",
                source_refs=[_source(TEXT_ONLY_UNCUT)],
            )
        ],
        source_refs=[_source(TEXT_ONLY_UNCUT)],
    )
    created = BusinessEvent(
        event_type=BusinessEventType.NEW_ORDER,
        new_lines=[NewOrderLine(sku_id="A-WHITE-M", qty=30)],
        source_refs=[_source(TEXT_NEW_ORDER)],
    )
    return {
        TEXT_ATTACH_SHEET: ExtractionResult(
            task_id="PENDING",
            intent_revision=0,
            input_mode=InputMode.TEXT,
            questions=["请上传订单表，并提交完整的 fixture 聊天文本与 source_ids。"],
            source_refs=[_source(TEXT_ATTACH_SHEET)],
        ).model_dump(mode="json", exclude={"task_id", "intent_revision"}),
        TEXT_BLACK_TO_WHITE: _extraction(TEXT_BLACK_TO_WHITE, stock, orders=["SO-1001"]),
        TEXT_ALL_NAVY: _extraction(TEXT_ALL_NAVY, navy, orders=["SO-1001"]),
        TEXT_ONLY_UNCUT: _extraction(TEXT_ONLY_UNCUT, uncut, orders=["SO-1001"]),
        TEXT_NEW_ORDER: _extraction(TEXT_NEW_ORDER, created, customer="华东服饰"),
        TEXT_BAD_SCHEMA: {"schema_version": "1.0", "surprise": True},
    }


def compare_sheet_quantities(
    extracted: ExtractionResult, spans: list[SourceRef]
) -> ExtractionResult:
    """Compare exact fixture intent with static XLSX rows, without guessing.

    Change layout: 订单号 / 原SKU / 目标SKU / 数量.
    New order layout: 客户 / SKU / 数量.
    Headers must be in row 1. Every intent line needs at least one matching row;
    unrelated rows cannot supply a quantity. Unsupported evidence asks for help.
    """
    extracted = extracted.model_copy(deep=True)
    event = extracted.business_event
    if event is None:
        return extracted
    sheets: dict[tuple[str, str], dict[int, dict[str, SourceRef]]] = {}
    for span in spans:
        if span.sheet and span.cell_range:
            column, row = coordinate_from_string(span.cell_range)
            sheets.setdefault((span.source_id, span.sheet), {}).setdefault(row, {})[column] = span
        else:
            extracted.questions.append(f"来源 {span.source_id} 不是支持的静态 XLSX，请确认数量。")
    if event.event_type == BusinessEventType.NEW_ORDER:
        targets = [
            ({"客户": extracted.customer_candidate, "SKU": line.sku_id}, line.qty)
            for line in event.new_lines
        ]
    else:
        targets = [
            (
                {"订单号": event.order_id, "原SKU": line.from_sku, "目标SKU": line.to_sku},
                line.qty,
            )
            for line in event.line_changes
        ]
    evidence = list(extracted.source_refs)
    for identity, chat_qty in targets:
        matched = False
        label = " / ".join(str(value) for value in identity.values())
        for (source_id, sheet), rows in sorted(sheets.items()):
            names = [span.text_excerpt for span in rows.get(1, {}).values()]
            headers = {span.text_excerpt: column for column, span in rows.get(1, {}).items()}
            required = {*identity, "数量"}
            if any(names.count(name) != 1 for name in required):
                extracted.questions.append(
                    f"{source_id} / {sheet} 的第 1 行必须包含唯一列名"
                    f" {' / '.join([*identity, '数量'])}，请确认表格布局。"
                )
                continue
            for row_no, cells in sorted(rows.items()):
                if row_no == 1 or any(
                    value is None
                    or headers[key] not in cells
                    or cells[headers[key]].text_excerpt != value
                    for key, value in identity.items()
                ):
                    continue
                matched = True
                evidence.extend(cells[headers[key]] for key in identity)
                qty_ref = cells.get(headers["数量"])
                if qty_ref is not None:
                    evidence.append(qty_ref)
                raw_qty = (qty_ref.text_excerpt or "") if qty_ref else "缺失"
                if not raw_qty.isascii() or not raw_qty.isdigit() or chat_qty is None:
                    extracted.questions.append(f"{label} 的表格数量需为非负整数，请确认数量。")
                    continue
                sheet_qty = int(raw_qty)
                if sheet_qty != chat_qty:
                    location = f"{source_id} / {sheet}!{qty_ref.cell_range}"
                    extracted.conflicts.append(
                        f"{label}：聊天数量 {chat_qty}，表格数量 {sheet_qty}（{location}）。"
                    )
                    extracted.questions.append(
                        f"请确认 {label} 的数量：聊天 {chat_qty} 件还是表格 {sheet_qty} 件？"
                    )
        if not matched:
            extracted.questions.append(
                f"未找到 {label} 的表格行，请提供第 1 行列名为"
                f" {' / '.join([*identity, '数量'])} 的静态表格并确认数量。"
            )
    if not sheets or not targets:
        extracted.questions.append("此 fixture 仅支持带明确订单/SKU 和数量列的 XLSX，请确认输入。")
    if extracted.conflicts or extracted.questions:
        # Preserve evidence but remove the executable intent: neither value wins.
        extracted.business_event = None
        extracted.line_changes = []
        extracted.new_lines = []
        evidence.extend(spans)
    else:
        event.source_refs = list(evidence)
        for line in event.line_changes:
            line.source_refs = list(evidence)
        extracted.line_changes = list(event.line_changes)
    extracted.source_refs = list({ref.model_dump_json(): ref for ref in evidence}.values())
    return extracted
