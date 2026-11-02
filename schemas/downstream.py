"""Downstream document check I/O (revision section 1.5)."""

from typing import Any, Literal

from pydantic import Field

from schemas import SCHEMA_VERSION
from schemas.common import ApiModel
from schemas.enums import DocType, FulfillmentMode, Verdict


class OrderLineSnapshot(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    sku_id: str
    ordered_qty: int
    fulfillment_mode: FulfillmentMode


class DocSnapshot(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    doc_type: DocType
    doc_id: str
    doc_revision: int
    status: str
    content: dict[str, Any]


class DocCheckInput(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str
    order_id: str
    base_revision: int
    old_lines: list[OrderLineSnapshot]
    new_lines: list[OrderLineSnapshot]
    documents: list[DocSnapshot] = Field(default_factory=list)


class DocVerdict(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    doc_type: DocType
    doc_id: str
    base_doc_revision: int
    verdict: Verdict
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    reason: str


class DocCheckOutput(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    verdicts: list[DocVerdict] = Field(default_factory=list)
