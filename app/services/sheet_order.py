"""Owned, bounded sheet-order command language; never model or fixture interpretation.

This private persistence contract is reconstructed from receipts and immutable files
at every acceptance/approval/commit. Numeric grammar applies to persisted cell text.
"""

import json
import re
from copy import deepcopy
from io import BytesIO
from typing import Any
from zipfile import ZipFile

from defusedxml.ElementTree import iterparse
from schemas.business import BusinessEvent, ExtractionResult, NewOrderLine
from schemas.common import SourceRef
from schemas.enums import BusinessEventType, PlanKind
from seeds.golden import WAREHOUSE_ID
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.uploads import MAX_FILE_BYTES, MAX_FILES, MAX_SOURCE_SPANS, static_workbook
from app.db.models import (
    Customer,
    Event,
    InputPayload,
    InputSource,
    Plan,
    PurchaseProposal,
    Sku,
    SourceSpan,
    Task,
    TaskSource,
)
from app.domain.hashing import canonical_hash
from app.errors import AppError

KEY = "sheet_order_v1"
ACTIVATION = "新建现货订单（表格）"
INITIAL = "按附件新建订单"
MAX_INT = 9223372036854775807
MAX_RECEIPTS = 64
_COLORS = {"黑": "BLACK", "白": "WHITE", "蓝": "BLUE", "藏青": "NAVY"}
_CORRECTION = re.compile(r"(黑|白|蓝|藏青) +(S|M|L|XL|XXL) +改成 +([1-9][0-9]*)")
_HELP = (
    "固定表格模式（确定性解析，无模型）：请上传订单工作表，A1:E1 为客户/款/颜色/尺码/数量，"
    "再选择来源并回答「按附件新建订单」。更正仅支持「蓝 L 改成 20」格式，审批另行进行。"
    "每任务最多三次处理；预算耗尽或更换文件请新建任务并重新上传。"
)


def _invalid(message="表格订单证据无法验证，请新建任务并重新上传"):
    return AppError(message, code="SHEET_CONTEXT_INVALID", http_status=409)


def _bounded(value):
    """Bound JSON traversal before serialization, including hostile stored records."""
    pending = [(value, 0)]
    nodes, size = 0, 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 750_000 or depth > 12 or size > 16 * 1024 * 1024:
            raise _invalid()
        if isinstance(item, dict):
            if len(item) > 32 or any(not isinstance(key, str) for key in item):
                raise _invalid()
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif isinstance(item, list):
            if len(item) > MAX_FILES * MAX_SOURCE_SPANS:
                raise _invalid()
            pending.extend((part, depth + 1) for part in item)
        elif isinstance(item, str):
            if len(item.encode("utf-8")) > MAX_FILE_BYTES:
                raise _invalid()
            size += len(item.encode("utf-8"))
        elif item is not None and type(item) not in (int, bool):
            raise _invalid()
        else:
            size += 24


def _equal(left, right):
    _bounded(left)
    _bounded(right)
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(
        right, sort_keys=True, ensure_ascii=False
    )


def quantity(text: str) -> int:
    if (
        not isinstance(text, str)
        or re.fullmatch(r"[1-9][0-9]*", text) is None
        or len(text) > 19
        or (len(text) == 19 and text > str(MAX_INT))
    ):
        raise _invalid("数量须为数据库范围内的正 ASCII 整数")
    return int(text)


def is_sheet_task(session: Session, task: Task) -> bool:
    """Recognize malformed markers too, so they cannot fall back to a provider."""
    bodies = list(
        session.scalars(
            select(InputPayload.body).where(InputPayload.task_id == task.id).limit(MAX_RECEIPTS + 1)
        )
    )
    return len(bodies) > MAX_RECEIPTS or any(
        not isinstance(body, dict) or body.get("text") == ACTIVATION or KEY in body
        for body in bodies
    )


def _receipts(session, task):
    records = list(
        session.scalars(
            select(InputPayload).where(InputPayload.task_id == task.id).limit(MAX_RECEIPTS + 1)
        )
    )
    if (
        not records
        or len(records) > MAX_RECEIPTS
        or task.order_id is not None
        or any(not isinstance(row.body, dict) for row in records)
    ):
        raise _invalid()
    sequences = [row.body.get("input_sequence") for row in records]
    if any(type(n) is not int or n <= 0 for n in sequences):
        raise _invalid()
    records.sort(key=lambda row: row.body["input_sequence"])
    if [row.body["input_sequence"] for row in records] != list(range(1, len(records) + 1)):
        raise _invalid()
    for index, row in enumerate(records):
        event = session.get(Event, row.event_id)
        if (
            event is None
            or event.order_id is not None
            or event.kind != ("input.submit" if index == 0 else "clarify_answer")
        ):
            raise _invalid()
    first = records[0]
    if first.body.get("text") != ACTIVATION or first.body.get("source_ids") != []:
        raise _invalid("本任务不是未选择已有订单的固定表格新单入口")
    if task.intent_revision != len(records):
        raise _invalid()
    return records


def _source_ids(body):
    ids = body.get("source_ids")
    if (
        not isinstance(ids, list)
        or len(ids) > 100
        or any(not isinstance(item, str) or not item or len(item) > 64 for item in ids)
    ):
        raise _invalid()
    result = sorted(set(ids))
    if len(result) > MAX_FILES:
        raise _invalid()
    return result


def _original_rows(session, task, ids):
    from app.services.kernel import _resolve_sku_id

    rows, sources, skus, customers = [], [], set(), set()
    for source_id in ids:
        source = session.get(InputSource, source_id)
        if source is None or session.get(TaskSource, (task.id, source_id)) is None:
            raise _invalid()
        if (
            not source.original_name.lower().endswith(".xlsx")
            or source.media_type
            != "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            or source.parser_version != "static-v1"
            or type(source.size) is not int
            or not 0 < source.size <= MAX_FILE_BYTES
        ):
            raise _invalid()
        try:
            from app.services.source_files import original_content

            content = original_content(source)
        except AppError as exc:
            raise _invalid() from exc
        spans = list(
            session.scalars(
                select(SourceSpan)
                .where(SourceSpan.source_id == source_id)
                .limit(MAX_SOURCE_SPANS + 1)
            )
        )
        if not spans or len(spans) > MAX_SOURCE_SPANS:
            raise _invalid()
        cells = {}
        for span in spans:
            if (
                span.sheet != "订单"
                or not isinstance(span.cell_range, str)
                or re.fullmatch(r"[A-E][1-9][0-9]{0,4}", span.cell_range) is None
                or span.cell_range in cells
                or not isinstance(span.text_excerpt, str)
                or len(span.text_excerpt) >= 500
                or any(
                    getattr(span, key) is not None
                    for key in ("line_start", "line_end", "message_index")
                )
            ):
                raise _invalid("只支持完整的订单工作表 A:E 五列表格")
            cells[span.cell_range] = span.text_excerpt
        # Reuse all ingestion resource/formula guards before bounded XML merge inspection.
        actual = {}
        with static_workbook(content) as workbook:
            with ZipFile(BytesIO(content)) as archive:
                for sheet in workbook.worksheets:
                    with archive.open(sheet._worksheet_path) as xml:
                        for _, element in iterparse(xml, events=("end",)):
                            if element.tag.endswith("}mergeCell"):
                                raise _invalid("固定表格不支持合并单元格")
                            element.clear()
                    for physical in sheet.iter_rows():
                        for cell in physical:
                            if cell.value is not None and str(cell.value).strip():
                                if sheet.title != "订单":
                                    raise _invalid("不支持其他含内容的工作表")
                                actual[cell.coordinate] = str(cell.value)
        if actual != cells:
            raise _invalid("工作簿与已存来源片段不一致或证据截断")
        if [cells.get(f"{col}1") for col in "ABCDE"] != ["客户", "款", "颜色", "尺码", "数量"]:
            raise _invalid("表头须严格为客户/款/颜色/尺码/数量")
        numbers = sorted({int(cell[1:]) for cell in cells} - {1})
        if not numbers:
            raise _invalid("表格没有订单行")
        for number in numbers:
            values = [cells.get(f"{col}{number}") for col in "ABCDE"]
            if any(value is None or not value.strip() for value in values):
                raise _invalid("每个非空行必须填写全部五列，不支持自动填充")
            name, style, color, size, qty = [cells[f"{col}{number}"] for col in "ABCDE"]
            found = list(
                session.scalars(
                    select(Customer).where(Customer.display_name == name, Customer.active.is_(True))
                )
            )
            if len(found) != 1:
                raise _invalid("客户名称必须唯一且有效")
            customer = found[0]
            sku_id = _resolve_sku_id(session, style=style, color=color, size=size)
            sku = session.get(Sku, sku_id) if sku_id else None
            if sku is None or sku.default_fulfillment_mode != "STOCK" or sku.id in skus:
                raise _invalid("商品必须唯一、有效且为现货，不能重复商品行")
            skus.add(sku.id)
            customers.add(customer.id)
            amount = quantity(qty)
            refs = [
                SourceRef(
                    source_id=source_id,
                    sheet="订单",
                    cell_range=f"{col}{number}",
                    text_excerpt=cells[f"{col}{number}"],
                ).model_dump(mode="json")
                for col in "ABCDE"
            ]
            rows.append(
                dict(
                    source_id=source_id,
                    sheet="订单",
                    row_number=number,
                    customer_id=customer.id,
                    customer_name=name,
                    customer_code=customer.code,
                    sku_id=sku.id,
                    style=sku.style,
                    color=sku.color,
                    size=sku.size,
                    base_qty=amount,
                    effective_qty=amount,
                    refs=refs,
                )
            )
        sources.append(dict(source_id=source_id, sha256=source.sha256))
    if not rows or len(customers) != 1 or sum(row["effective_qty"] for row in rows) > MAX_INT:
        raise _invalid("全部来源须为同一客户且总数量在整数范围内")
    return rows, sources


def _shortage(session, task, record, shortage):
    if not isinstance(shortage, dict) or set(shortage) != {
        "plan_id",
        "input_event_id",
        "intent_revision",
        "facts",
    }:
        raise _invalid()
    if (
        shortage["input_event_id"] != record["current_event_id"]
        or type(shortage["intent_revision"]) is not int
        or shortage["intent_revision"] != record["current_intent"]
    ):
        raise _invalid()
    if not isinstance(shortage["plan_id"], str):
        raise _invalid()
    plan = session.get(Plan, shortage["plan_id"])
    if (
        plan is None
        or plan.task_id != task.id
        or plan.plan_kind != PlanKind.CREATE
        or plan.intent_revision != shortage["intent_revision"]
        or not _equal(plan.normalized_changes.get(KEY), {**record, "shortage": shortage})
    ):
        raise _invalid()
    facts = shortage["facts"]
    if not isinstance(facts, list) or not 0 < len(facts) <= len(record["rows"]):
        raise _invalid()
    seen = set()
    seen_skus = set()
    for fact in facts:
        if not isinstance(fact, dict) or set(fact) != {
            "proposal_id",
            "sku_id",
            "requested_qty",
            "available_qty",
            "shortage_qty",
            "warehouse_id",
            "stock_revision",
        }:
            raise _invalid()
        if any(not isinstance(fact[k], str) for k in ("proposal_id", "sku_id", "warehouse_id")):
            raise _invalid()
        proposal = session.get(PurchaseProposal, fact["proposal_id"])
        row = next((r for r in record["rows"] if r["sku_id"] == fact["sku_id"]), None)
        if (
            proposal is None
            or row is None
            or proposal.task_id != task.id
            or proposal.sku_id != fact["sku_id"]
            or proposal.id in seen
            or proposal.sku_id in seen_skus
            or proposal.shortage_qty != fact["shortage_qty"]
            or fact["requested_qty"] != row["effective_qty"]
            or any(
                type(fact[k]) is not int or fact[k] < 0
                for k in ("requested_qty", "available_qty", "shortage_qty", "stock_revision")
            )
            or fact["shortage_qty"] <= 0
            or fact["requested_qty"] - fact["available_qty"] != fact["shortage_qty"]
            or fact["warehouse_id"] != WAREHOUSE_ID
        ):
            raise _invalid()
        seen.add(proposal.id)
        seen_skus.add(proposal.sku_id)
    if plan.plan_hash != canonical_hash(
        {
            "kind": "shortage",
            "shortages": {fact["sku_id"]: fact["shortage_qty"] for fact in facts},
            KEY: {**record, "shortage": shortage},
        }
    ):
        raise _invalid()
    events = list(session.scalars(select(Event).where(Event.payload_ref == plan.id).limit(2)))
    if (
        len(events) != 1
        or events[0].kind != "plan"
        or events[0].thread_id != "demo"
        or events[0].order_id is not None
        or events[0].intent_revision != record["current_intent"]
        or events[0].source_event_key != record["current_event_id"]
    ):
        raise _invalid()
    return deepcopy(shortage)


def _build(session, task, receipts, current):
    prior = [
        r
        for r in receipts
        if r.body["input_sequence"] < current.body["input_sequence"] and KEY in r.body
    ]
    if prior:
        if len(prior) != 1:
            raise _invalid("仅支持一次有证据的单行更正")
        initial = prior[0]
    else:
        initial = current
    if initial.body.get("text") != INITIAL:
        raise _invalid("请先选择来源并回答「按附件新建订单」")
    ids = _source_ids(initial.body)
    if not ids:
        raise _invalid("请明确选择已上传的表格来源")
    rows, sources = _original_rows(session, task, ids)
    record = dict(
        version=1,
        task_id=task.id,
        activation_event_id=receipts[0].event_id,
        activation_sequence=1,
        initial_event_id=initial.event_id,
        initial_sequence=initial.body["input_sequence"],
        current_event_id=initial.event_id,
        current_sequence=initial.body["input_sequence"],
        current_intent=initial.body["input_sequence"],
        sources=sources,
        rows=rows,
        correction=None,
        previous_shortage=None,
    )
    if prior:
        stored = initial.body[KEY]
        if not isinstance(stored, dict):
            raise _invalid()
        expected = dict(record)
        if "shortage" in stored:
            expected["shortage"] = _shortage(session, task, record, stored["shortage"])
        if not _equal(stored, expected):
            raise _invalid()
        selected = _source_ids(current.body)
        if selected and selected != ids:
            raise _invalid("更正不能更换或增减来源，请新建任务并重新上传")
        text = current.body.get("text")
        match = _CORRECTION.fullmatch(text.strip(" ")) if isinstance(text, str) else None
        if match is None:
            raise _invalid("只支持完整的「蓝 L 改成 20」格式，不支持否定或额外动作")
        color, size, lexeme = match.groups()
        matches = [row for row in rows if row["color"] == _COLORS[color] and row["size"] == size]
        if len(matches) != 1:
            raise _invalid("颜色和尺码必须唯一匹配完整表格中的一行")
        target = matches[0]
        value = quantity(lexeme)
        record.update(
            current_event_id=current.event_id,
            current_sequence=current.body["input_sequence"],
            current_intent=current.body["input_sequence"],
            previous_shortage=expected.get("shortage"),
            correction=dict(
                source_id=target["source_id"],
                row_number=target["row_number"],
                old_qty=target["effective_qty"],
                new_qty=value,
                event_id=current.event_id,
                input_sequence=current.body["input_sequence"],
                text=text,
            ),
        )
        target["effective_qty"] = value
        if sum(row["effective_qty"] for row in rows) > MAX_INT:
            raise _invalid()
    return record


def validate_provenance(session: Session, task: Task, record: dict) -> dict:
    _bounded(record)
    receipts = _receipts(session, task)
    expected = _build(session, task, receipts, receipts[-1])
    if not isinstance(record, dict):
        raise _invalid()
    if "shortage" in record:
        expected["shortage"] = _shortage(session, task, expected, record["shortage"])
    if not _equal(record, expected):
        raise _invalid()
    return expected


def accepted_provenance(session: Session, task: Task) -> dict | None:
    if not is_sheet_task(session, task):
        return None
    receipts = _receipts(session, task)
    record = receipts[-1].body.get(KEY)
    if not isinstance(record, dict):
        raise _invalid()
    return validate_provenance(session, task, record)


def validated_provenance(session: Session, task: Task, *, plan_record: dict | None = None):
    record = accepted_provenance(session, task)
    if plan_record is not None and not _equal(record, plan_record):
        raise _invalid()
    return record


def derive(session: Session, task: Task) -> tuple[ExtractionResult, dict | None]:
    base: dict[str, Any] = dict(
        task_id=task.id, intent_revision=task.intent_revision, input_mode="text"
    )
    try:
        receipts = _receipts(session, task)
        current = receipts[-1]
        if task.last_event_id != current.event_id:
            raise _invalid()
        if len(receipts) == 1:
            return ExtractionResult(**base, questions=[_HELP]), None
        record = _build(session, task, receipts, current)
        refs = [SourceRef.model_validate(ref) for row in record["rows"] for ref in row["refs"]]
        correction = record["correction"]
        if correction:
            refs.append(
                SourceRef(
                    source_id="SRC-INPUT",
                    field="quantity",
                    message_index=correction["input_sequence"],
                    text_excerpt=correction["text"],
                )
            )
        lines = [
            NewOrderLine(
                customer_code=row["customer_code"],
                sku_id=row["sku_id"],
                style=row["style"],
                color=row["color"],
                size=row["size"],
                qty=row["effective_qty"],
                source_ref=SourceRef.model_validate(row["refs"][4]),
            )
            for row in record["rows"]
        ]
        event = BusinessEvent(
            event_type=BusinessEventType.NEW_ORDER,
            customer_code=record["rows"][0]["customer_code"],
            new_lines=lines,
            source_refs=refs,
        )
        return ExtractionResult(
            **base,
            customer_candidate=record["rows"][0]["customer_name"],
            new_lines=lines,
            source_refs=refs,
            business_event=event,
        ), record
    except (AppError, ValueError, OSError, KeyError, TypeError) as exc:
        message = exc.message if isinstance(exc, AppError) else "表格证据无效，请核对固定模板"
        return ExtractionResult(**base, questions=[message + "。" + _HELP]), None
