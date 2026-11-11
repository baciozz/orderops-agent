"""Human-confirmed table layouts, with exact cell provenance and no file-name rules."""

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Literal

from openpyxl.utils.cell import range_boundaries
from pydantic import Field, ValidationError

from app.review.contracts import ReviewModel, Snapshot
from app.review.documents import Document, Table, invalid

ROLES = {
    "unknown": "尚未确定角色",
    "orders": "订单基线（款色码、订购量与履约方式）",
    "inventory": "可用库存快照",
    "fulfillment": "已发/已裁总量快照",
    "documents": "生产采购关联快照",
    "instruction": "客户要求/聊天/变更表",
    "excluded": "明确不采用（保留原件）",
}
FIELDS = [
    "order_id",
    "revision",
    "sku_id",
    "style",
    "color",
    "size",
    "fulfillment_mode",
    "ordered_qty",
    "shipped_qty",
    "cut_qty",
    "available_qty",
    "captured_at",
    "doc_id",
    "kind",
    "order_revision",
    "quantity",
    "status",
    "unit",
]
ALIASES = {
    "order_id": ["订单号", "订单", "单号", "销售订单号"],
    "revision": ["版本", "修订", "订单版本"],
    "sku_id": ["sku", "skuid", "商品编码"],
    "style": ["款号", "款", "货号", "款式"],
    "color": ["颜色", "色", "色号"],
    "size": ["尺码", "码", "尺寸"],
    "fulfillment_mode": ["履约方式", "供货方式", "现货生产"],
    "ordered_qty": ["订购量", "订购数量", "原数量", "数量"],
    "shipped_qty": ["已发", "已发量", "已发数量", "发货总量"],
    "cut_qty": ["已裁", "已裁量", "已裁数量"],
    "available_qty": ["可用库存", "可用量", "可用数量"],
    "captured_at": ["快照时间", "采集时间"],
    "doc_id": ["单据号", "生产采购单号"],
    "kind": ["单据类型", "类型"],
    "order_revision": ["关联订单版本"],
    "quantity": ["单据数量", "数量"],
    "status": ["状态", "单据状态"],
    "unit": ["单位"],
}


class MappingSpec(ReviewModel):
    table_id: str = "T1"
    layout: Literal["rows", "sizes", "matrix"] = "rows"
    header_row: int = Field(default=1, ge=1, le=1000)
    first_data_row: int | None = Field(default=None, ge=1, le=1000)
    last_data_row: int | None = Field(default=None, ge=1, le=1000)
    columns: dict[str, int] = Field(default_factory=dict)
    constants: dict[str, str] = Field(default_factory=dict)
    quantity_columns: list[int] = Field(default_factory=list)
    quantity_field: Literal["ordered_qty", "available_qty", "shipped_qty", "cut_qty"] = (
        "ordered_qty"
    )
    expand_merged: bool = True


def suggested_columns(headers: list[str]) -> dict[str, int]:
    def canonical(value: str) -> str:
        return re.sub(r"[\s_\-]", "", value).casefold()

    result = {}
    for field in FIELDS:
        matches = [
            i
            for i, header in enumerate(headers)
            if canonical(header)
            in {canonical(field), *(canonical(alias) for alias in ALIASES.get(field, []))}
        ]
        if len(matches) == 1:
            result[field] = matches[0]
    return result


def mapped_records(document: Document, spec: MappingSpec, source_id: str) -> list[dict]:
    table = next((table for table in document.tables if table.table_id == spec.table_id), None)
    if table is None or not 1 <= spec.header_row <= len(table.rows):
        raise invalid("请选择存在的工作表/表格及表头行", "MAPPING_REQUIRED")
    if set(spec.columns) - set(FIELDS) or set(spec.constants) - set(FIELDS):
        raise invalid("列映射含未知字段", "MAPPING_REQUIRED")
    if any(type(index) is not int or not 0 <= index < 64 for index in spec.columns.values()):
        raise invalid("列映射超出范围", "MAPPING_REQUIRED")
    start = spec.first_data_row or spec.header_row + 1
    end = spec.last_data_row or len(table.rows)
    if start <= spec.header_row or end < start or end > len(table.rows):
        raise invalid("数据行范围无效，请保留表头并选择实际业务行", "MAPPING_REQUIRED")
    headers = [
        _cell(table, spec.header_row, col, spec)[0]
        for col in range(max(len(row) for row in table.rows))
    ]
    if spec.layout != "rows" and not spec.quantity_columns:
        raise invalid("横排/矩阵布局须明确选择尺码数量列", "MAPPING_REQUIRED")
    records = []
    for row_index in range(start, end + 1):
        if not any(table.rows[row_index - 1]):
            continue
        values: dict[str, str] = {}
        origins: dict[str, dict] = {}
        for field, index in spec.columns.items():
            value, locator = _cell(table, row_index, index, spec)
            values[field] = value.strip()
            origins[field] = {"source_id": source_id, "locator": locator}
        for field, value in spec.constants.items():
            if value.strip():
                if values.get(field):
                    raise invalid("同一字段不能同时有非空列值和人工常量", "MAPPING_REQUIRED")
                values[field] = value.strip()
                origins[field] = {
                    "source_id": source_id,
                    "locator": "人工补充常量；非原件读取",
                    "human": True,
                }
        if spec.layout == "rows":
            records.append({"values": values, "origins": origins})
        else:
            quantity_field = spec.quantity_field
            if quantity_field not in {"ordered_qty", "available_qty", "shipped_qty", "cut_qty"}:
                raise invalid("请选择横排数量是订购/可用/已发/已裁总量", "MAPPING_REQUIRED")
            for column in spec.quantity_columns:
                if type(column) is not int or not 0 <= column < len(headers) or not headers[column]:
                    raise invalid("尺码列或表头无效，请确认", "MAPPING_REQUIRED")
                quantity, locator = _cell(table, row_index, column, spec)
                if not quantity.strip():
                    # Blank is unknown, never zero; importing it will fail if quantity is required.
                    quantity = ""
                record = {
                    **values,
                    "size": headers[column].strip(),
                    quantity_field: quantity.strip(),
                }
                refs = {
                    **origins,
                    "size": {
                        "source_id": source_id,
                        "locator": _cell(table, spec.header_row, column, spec)[1],
                    },
                    quantity_field: {"source_id": source_id, "locator": locator},
                }
                records.append({"values": record, "origins": refs})
    if not records:
        raise invalid("所选范围没有数据行", "MAPPING_REQUIRED")
    return records


def _cell(table: Table, row: int, column: int, spec: MappingSpec) -> tuple[str, str]:
    if column < len(table.rows[row - 1]):
        value = table.rows[row - 1][column]
        locator = table.locators[row - 1][column]
    else:
        value, locator = "", f"{table.label} 第{row}行第{column + 1}列（空）"
    if not value and spec.expand_merged:
        for merged in table.merged_ranges:
            left, top, right, bottom = range_boundaries(merged)
            if top <= row <= bottom and left <= column + 1 <= right:
                if top <= len(table.rows) and left <= len(table.rows[top - 1]):
                    return table.rows[top - 1][left - 1], f"{table.label}!{merged}（合并区左上值）"
    return value, locator


def identity(values: dict) -> str:
    if values.get("sku_id"):
        return values["sku_id"]
    parts = [values.get(field, "") for field in ("style", "color", "size")]
    if not all(parts):
        raise invalid("请映射SKU，或同时确认款号、颜色、尺码；不会按数量猜归属", "MAPPING_REQUIRED")
    return "K-" + hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


def integer(value: str | None, *, unknown: bool = False) -> int | None:
    if value in (None, "") and unknown:
        return None
    if value is None or not re.fullmatch(r"[0-9]{1,7}", value):
        raise invalid(
            "关键数量/版本须为明确非负整数，空白是未知；请纠正映射或补充资料", "MAPPING_REQUIRED"
        )
    number = int(value)
    if number > 1_000_000:
        raise invalid("数量超过1000000上限", "MAPPING_REQUIRED")
    return number


def build_snapshot(
    assets: list[dict], *, captured_at: str, synthetic: bool
) -> tuple[Snapshot, dict]:
    orders = defaultdict(list)
    versions: dict[str, int] = {}
    catalog: dict[str, dict] = {}
    inventory = []
    inventory_raw = []
    documents = []
    fulfillment = []
    origins = {}
    for asset in assets:
        if not asset.get("confirmed") or asset["role"] not in {
            "orders",
            "inventory",
            "fulfillment",
            "documents",
        }:
            continue
        for record in asset["records"]:
            raw = record["values"]
            sku = identity(raw)
            origin = record["origins"]
            if asset["role"] == "orders":
                mode = {"现货": "STOCK", "按单生产": "MTO", "生产": "MTO"}.get(
                    raw.get("fulfillment_mode", ""), raw.get("fulfillment_mode")
                )
                if mode not in {"STOCK", "MTO"}:
                    raise invalid(
                        "履约方式未知；请明确STOCK/现货或MTO/按单生产", "MAPPING_REQUIRED"
                    )
                if not all(raw.get(field) for field in ("order_id", "style", "color", "size")):
                    raise invalid("订单号、款号、颜色和尺码缺失，请确认映射", "MAPPING_REQUIRED")
                entry = {
                    "sku_id": sku,
                    "style": raw["style"],
                    "color": raw["color"],
                    "size": raw["size"],
                    "fulfillment_mode": mode,
                }
                if sku in catalog and catalog[sku] != entry:
                    raise invalid("同一SKU对应不同款色码/履约方式，不能合并", "SOURCE_CONFLICT")
                catalog[sku] = entry
                version = integer(raw.get("revision"))
                if version is None or version < 1:
                    raise invalid("订单版本须明确且大于零", "MAPPING_REQUIRED")
                order_id = raw["order_id"]
                if order_id in versions and versions[order_id] != version:
                    raise invalid("同一订单存在冲突版本，请选择正确来源", "SOURCE_CONFLICT")
                versions[order_id] = version
                orders[order_id].append(
                    {
                        "sku_id": sku,
                        "ordered_qty": integer(raw.get("ordered_qty")),
                        "shipped_qty": integer(raw.get("shipped_qty"), unknown=True),
                        "cut_qty": integer(raw.get("cut_qty"), unknown=True),
                    }
                )
                origins[f"{order_id}/{sku}"] = origin
            elif asset["role"] == "inventory":
                inventory_raw.append((raw, sku, origin))
            elif asset["role"] == "fulfillment":
                fulfillment.append((raw, sku, origin))
            else:
                documents.append(
                    {
                        "doc_id": raw.get("doc_id"),
                        "kind": raw.get("kind"),
                        "order_id": raw.get("order_id"),
                        "order_revision": integer(raw.get("order_revision")),
                        "sku_id": sku,
                        "quantity": integer(raw.get("quantity")),
                        "status": raw.get("status"),
                    }
                )
    if not orders:
        raise invalid("请先确认至少一份订单基线及其字段映射", "BASELINE_REQUIRED")
    for raw, sku, origin in inventory_raw:
        if sku not in catalog:
            if not all(raw.get(field) for field in ("style", "color", "size")):
                raise invalid("库存含目录外SKU，请提供明确款号、颜色与尺码", "MAPPING_REQUIRED")
            catalog[sku] = {
                "sku_id": sku,
                "style": raw["style"],
                "color": raw["color"],
                "size": raw["size"],
                "fulfillment_mode": "STOCK",
            }
        elif any(
            raw.get(field) and raw[field] != catalog[sku][field]
            for field in ("style", "color", "size")
        ):
            raise invalid("库存与目录款色码矛盾，不按SKU合并冲突资料", "SOURCE_CONFLICT")
        inventory.append(
            {"sku_id": sku, "available_qty": integer(raw.get("available_qty"), unknown=True)}
        )
        origins[f"inventory/{sku}"] = origin
    seen_fulfillment = set()
    for raw, sku, origin in fulfillment:
        key = (raw.get("order_id"), sku)
        if key in seen_fulfillment:
            raise invalid("履约快照重复，不把明细擅自累加成总量", "SOURCE_CONFLICT")
        seen_fulfillment.add(key)
        if versions.get(key[0]) != integer(raw.get("revision")):
            raise invalid("履约快照的订单/版本不匹配，请确认来源", "SOURCE_CONFLICT")
        line = next((line for line in orders[key[0]] if line["sku_id"] == sku), None)
        if line is None:
            raise invalid("履约事实没有对应订单行，不按SKU猜订单归属", "SOURCE_CONFLICT")
        for field in ("shipped_qty", "cut_qty"):
            value = integer(raw.get(field), unknown=True)
            if value is not None:
                if line[field] is not None and line[field] != value:
                    raise invalid("基线与履约文件数量冲突，请选择正确快照", "SOURCE_CONFLICT")
                line[field] = value
                origins[f"{key[0]}/{sku}"][field] = origin.get(field)
    if not captured_at.strip():
        raise invalid("请确认业务快照时间；上传时间不能冒充业务时间", "MAPPING_REQUIRED")
    raw_snapshot = {
        "synthetic": synthetic,
        "captured_at": captured_at,
        "catalog": list(catalog.values()),
        "orders": [
            {"order_id": key, "revision": versions[key], "lines": lines}
            for key, lines in orders.items()
        ],
        "inventory": inventory,
        "documents": documents,
    }
    try:
        snapshot = Snapshot.model_validate(raw_snapshot)
    except ValidationError:
        raise invalid(
            "映射后的基线不完整、含重复行或非法字段；请核对各预览，不自动合并或补零",
            "MAPPING_REQUIRED",
        ) from None
    origins["imported_at"] = datetime.now(UTC).isoformat()
    return snapshot, origins
