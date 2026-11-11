"""Explicit snapshots and proposals; absent fulfilment facts stay unknown."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Qty = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")]


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    @field_validator("*", mode="before")
    @classmethod
    def reject_invalid_text(cls, value):
        if isinstance(value, str) and any(ord(c) < 32 and c not in "\t\r\n" for c in value):
            raise ValueError("文本包含无效控制字符")
        return value


class CatalogSku(ReviewModel):
    sku_id: Identifier
    style: Identifier
    color: Identifier
    size: Identifier
    fulfillment_mode: Literal["STOCK", "MTO"]


class OrderLine(ReviewModel):
    sku_id: Identifier
    ordered_qty: Qty
    shipped_qty: Qty | None = None
    cut_qty: Qty | None = None


class OrderSnapshot(ReviewModel):
    order_id: Identifier
    revision: Annotated[int, Field(strict=True, ge=1)]
    lines: Annotated[list[OrderLine], Field(min_length=1, max_length=100)]


class InventorySnapshot(ReviewModel):
    sku_id: Identifier
    available_qty: Qty | None = None


class DocumentSnapshot(ReviewModel):
    doc_id: Identifier
    kind: Literal["MO", "PO"]
    order_id: Identifier
    order_revision: Annotated[int, Field(strict=True, ge=1)]
    sku_id: Identifier
    quantity: Qty
    status: Literal["DRAFT", "ACTIVE", "CUT", "RECEIVED", "PENDING_SUPPLIER", "CANCELLED"]


class Snapshot(ReviewModel):
    synthetic: Annotated[bool, Field(strict=True)]
    captured_at: Annotated[str, Field(min_length=1, max_length=100)]
    catalog: Annotated[list[CatalogSku], Field(min_length=1, max_length=200)]
    orders: Annotated[list[OrderSnapshot], Field(min_length=1, max_length=30)]
    inventory: Annotated[list[InventorySnapshot], Field(max_length=200)]
    documents: Annotated[list[DocumentSnapshot], Field(max_length=300)] = []

    @model_validator(mode="after")
    def validate_keys(self) -> "Snapshot":
        groups = [
            [row.sku_id for row in self.catalog],
            [row.order_id for row in self.orders],
            [row.sku_id for row in self.inventory],
            [row.doc_id for row in self.documents],
        ] + [[row.sku_id for row in order.lines] for order in self.orders]
        if any(len(set(keys)) != len(keys) for keys in groups):
            raise ValueError("快照含重复标识或订单行，请先纠正，不自动合并")
        catalog = {sku.sku_id for sku in self.catalog}
        if any(row.sku_id not in catalog for order in self.orders for row in order.lines):
            raise ValueError("订单行须在商品目录中存在")
        if any(row.sku_id not in catalog for row in self.inventory):
            raise ValueError("库存 SKU 须在商品目录中存在")
        return self


class Citation(ReviewModel):
    line: Annotated[int, Field(strict=True, ge=1)]
    quote: Annotated[str, Field(min_length=1, max_length=1000)]


class ProposedChange(ReviewModel):
    mode: Literal["set_total", "increment", "replace_remaining", "cancel_remaining"]
    sku_id: Identifier
    to_sku: Identifier | None
    quantity: Annotated[int, Field(strict=True, ge=-1_000_000, le=1_000_000)] | None = Field(
        description="总量目标、增减量或换出的件数，按mode区分；cancel_remaining为null"
    )
    unit: str
    before_qty: Qty | None = Field(description="原SKU在所选订单快照中的订购总量")
    after_qty: Qty | None = Field(
        description="原SKU变更后的订购总量；换款为before_qty-quantity，不是目标SKU总量"
    )
    citations: Annotated[list[Citation], Field(max_length=20)]


class Proposal(ReviewModel):
    order_id: Identifier
    disposition: Literal["propose", "clarify", "no_change"]
    changes: Annotated[list[ProposedChange], Field(max_length=100)]
    questions: Annotated[list[str], Field(max_length=30)]
    summary: Annotated[str, Field(max_length=3000)]


class CreateReview(ReviewModel):
    request_key: Annotated[str, Field(min_length=1, max_length=100)]
    selected_order_id: Identifier
    instruction: Annotated[str, Field(min_length=1, max_length=15000)]
    snapshot: Snapshot


class RevisionRequest(ReviewModel):
    revision: Annotated[int, Field(strict=True, ge=1)]


class Correction(RevisionRequest):
    proposal: Proposal
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class UpdateEvidence(RevisionRequest):
    instruction: Annotated[str, Field(min_length=1, max_length=15000)]
    snapshot: Snapshot


class ExtractRequest(RevisionRequest):
    request_key: Annotated[str, Field(min_length=1, max_length=100)]
    acknowledge_transmission: Literal[True]


def model_schema() -> dict:
    """Transport subset only. Full Pydantic validation runs after model output."""
    supported = {
        "$defs",
        "$ref",
        "type",
        "properties",
        "items",
        "enum",
        "const",
        "anyOf",
        "additionalProperties",
        "required",
        "title",
        "description",
    }

    def clean(value):
        if not isinstance(value, dict):
            return value
        return {
            key: (
                {name: clean(child) for name, child in child_value.items()}
                if key in {"$defs", "properties"}
                else [clean(child) for child in child_value]
                if key == "anyOf"
                else clean(child_value)
            )
            for key, child_value in value.items()
            if key in supported
        }

    return clean(Proposal.model_json_schema())
