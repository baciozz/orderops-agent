"""HTTP bodies for the endpoints listed in plan section 8 and revision section 5.

Stage 0 froze these models. Later stages mount the ones they implement.
"""

from typing import Literal

from pydantic import Field

from schemas import SCHEMA_VERSION
from schemas.business import LineChange
from schemas.common import ApiModel, SourceRef
from schemas.enums import InputMode, OrderStatus, TaskStatus


class SourceUploadResponse(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    source_id: str
    parser_status: str
    limits: dict[str, int]


class InputSubmitRequest(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    thread_id: str
    text: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    client_event_key: str
    selected_order_id: str | None = None


class InputSubmitResponse(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    event_id: str
    task_id: str
    chain_id: str
    status: TaskStatus


class TaskGetResponse(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str
    chain_id: str
    order_id: str | None
    status: TaskStatus
    intent_revision: int
    questions: list[str] = Field(default_factory=list)
    plan_id: str | None = None
    error_code: str | None = None
    result_ref: str | None = None


class TaskCancelRequest(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    expected_intent_revision: int


class TaskCancelResponse(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str
    status: str
    message: str


class OrderLineView(ApiModel):
    sku_id: str
    ordered_qty: int
    fulfillment_mode: Literal["STOCK", "MTO"]
    shipped_qty: int
    cut_qty: int


class OrderGetResponse(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    order_id: str
    current_revision: int
    intent_revision: int
    status: OrderStatus
    lines: list[OrderLineView]


class StockQuery(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    sku_ids: list[str]
    warehouse_id: str


class StockRow(ApiModel):
    sku_id: str
    warehouse_id: str
    on_hand: int
    reserved: int
    available: int
    revision: int


class StockResponse(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    rows: list[StockRow]


class PlanChangeRequest(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    order_id: str
    base_revision: int
    intent_revision: int
    changes: list[LineChange]
    source_refs: list[SourceRef] = Field(default_factory=list)
    client_event_key: str | None = None


class PlanPreviewResponse(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str | None = None
    chain_id: str | None = None
    status: str
    plan_id: str
    base_revision: int
    intent_revision: int
    plan_hash: str
    stock_revisions: dict[str, int]
    requires_human_approval: bool
    issues: list[str] = Field(default_factory=list)


class ApproveRequest(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    plan_hash: str
    expected_intent_revision: int
    acknowledge_duplicate: bool = False


class ApproveResponse(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    approval_id: str
    status: Literal["ACTIVE"]
    expires_at: str


class CommitRequest(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    approval_id: str
    plan_hash: str
    idempotency_key: str


class CommitSuccess(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str | None = None
    chain_id: str | None = None
    status: Literal["SUCCEEDED"]
    operation_id: str
    order_id: str
    revision: int
    audit_id: str


class OperationGetResponse(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    operation_id: str
    status: str
    order_id: str | None = None
    revision: int | None = None
    committed_verification_pending: bool


class PurchaseProposeRequest(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str
    sku_id: str
    shortage_qty: int
    reason: str


class PurchaseProposeResponse(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    proposal_id: str
    status: Literal["DRAFT"]


class DispatchRequest(ApiModel):
    """Synchronous fixture dispatch. Stage 2's POST /inputs stays the inbox contract."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    text: str
    input_mode: InputMode = InputMode.TEXT
    client_event_key: str | None = None


class DispatchStepBody(ApiModel):
    step_no: int
    agent_id: str
    agent_version: str
    capability: str
    status: str
    error_code: str | None = None
    skip_reason: str | None = None


class DispatchResponse(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str
    chain_id: str
    status: str
    plan_id: str | None = None
    order_id: str | None = None
    intent_revision: int
    plan_hash: str | None = None
    requires_human_approval: bool
    issues: list[str] = Field(default_factory=list)
    error_code: str | None = None
    steps: list[DispatchStepBody] = Field(default_factory=list)
