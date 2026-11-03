"""Exact-string fixtures for the parser. A miss does not call a live model.

Stage 3 owns natural-language resolution. These keys are the golden utterances
the dispatcher must recognize in fixture mode.
"""

from schemas.business import BusinessEvent, ExtractionResult, LineChange, NewOrderLine
from schemas.common import SourceRef
from schemas.enums import BusinessEventType, InputMode, LineMode

TEXT_BLACK_TO_WHITE = "SO-1001 把未发的 40 件黑色 M 改成白色 M"
TEXT_ALL_NAVY = "C 款黑色 L 全部改藏青"
TEXT_ONLY_UNCUT = "只改未开裁"
TEXT_NEW_ORDER = "新建订单 华东服饰 A 款白 M 30"
TEXT_BAD_SCHEMA = "SCHEMA-BROKEN"


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
        TEXT_BLACK_TO_WHITE: _extraction(TEXT_BLACK_TO_WHITE, stock, orders=["SO-1001"]),
        TEXT_ALL_NAVY: _extraction(TEXT_ALL_NAVY, navy, orders=["SO-1001"]),
        TEXT_ONLY_UNCUT: _extraction(TEXT_ONLY_UNCUT, uncut, orders=["SO-1001"]),
        TEXT_NEW_ORDER: _extraction(TEXT_NEW_ORDER, created, customer="华东服饰"),
        TEXT_BAD_SCHEMA: {"schema_version": "1.0", "surprise": True},
    }
