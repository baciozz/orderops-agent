"""Inventory-check sub-agent I/O. The deterministic precheck recomputes this."""

from typing import Literal

from pydantic import Field

from schemas import SCHEMA_VERSION
from schemas.common import ApiModel
from schemas.enums import FulfillmentMode


class InventoryLineCandidate(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    sku_id: str
    fulfillment_mode: FulfillmentMode
    demand_qty: int


class InventoryCheckInput(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str
    order_id: str | None = None
    warehouse_id: str
    lines: list[InventoryLineCandidate] = Field(default_factory=list)


class SkuInventoryResult(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    sku_id: str
    on_hand: int | None = None
    reserved: int | None = None
    available: int | None = None
    stock_revision: int | None = None
    reservation_delta: int
    shortage_qty: int
    purchase_proposal: bool


class InventoryCheckOutput(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    lines: list[SkuInventoryResult] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
