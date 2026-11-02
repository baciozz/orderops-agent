"""Business events and the extraction result from plan section 9."""

from typing import Literal

from pydantic import Field

from schemas import SCHEMA_VERSION
from schemas.common import ApiModel, SourceRef
from schemas.enums import BusinessEventType, InputMode, LineMode


class LineChange(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    mode: LineMode
    sku_id: str | None = None
    from_sku: str | None = None
    to_sku: str | None = None
    qty: int | None = None
    inferred: bool = False
    inference_basis: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class NewOrderLine(ApiModel):
    """P0 new orders are STOCK only. MTO creation is P1."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    customer_code: str | None = None
    style: str | None = None
    color: str | None = None
    size: str | None = None
    sku_id: str | None = None
    qty: int | None = None
    unit: Literal["piece"] = "piece"
    fulfillment_mode: Literal["STOCK"] = "STOCK"
    source_ref: SourceRef | None = None


class BusinessEvent(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    event_type: BusinessEventType
    order_id: str | None = None
    chain_id: str | None = None
    customer_code: str | None = None
    line_changes: list[LineChange] = Field(default_factory=list)
    new_lines: list[NewOrderLine] = Field(default_factory=list)
    clarification_choice: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class ExtractionResult(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str
    intent_revision: int
    input_mode: InputMode
    order_candidates: list[str] = Field(default_factory=list)
    customer_candidate: str | None = None
    line_changes: list[LineChange] = Field(default_factory=list)
    new_lines: list[NewOrderLine] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    business_event: BusinessEvent | None = None


class ParserInput(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str
    intent_revision: int
    input_mode: InputMode
    spans: list[SourceRef] = Field(default_factory=list)
