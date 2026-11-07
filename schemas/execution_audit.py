"""A bounded historical field comparison, not an order or database pass."""

from typing import Literal

from pydantic import Field

from schemas.common import ApiModel

ExecutionField = Literal["revision_lines", "operation_links", "reservation_movements"]


class ExecutionFieldResult(ApiModel):
    field: ExecutionField
    status: Literal["consistent", "findings"]
    expected_count: int
    observed_count: int
    reason_codes: list[str] = Field(default_factory=list)


class UncoveredScope(ApiModel):
    object_type: str
    object_id: str
    reason_code: str


class ObservedDependency(ApiModel):
    object_type: str
    object_id: str
    observed_order_ids: list[str]
    membership_completeness: Literal["not_established"] = "not_established"


class ExecutionAudit(ApiModel):
    schema_version: Literal["1.0"] = "1.0"
    operation_id: str
    scope: Literal["retained_execution_fields"] = "retained_execution_fields"
    status: Literal["consistent_for_retained_fields", "findings", "uncertain"]
    reason_code: str | None = None
    order_id: str | None = None
    order_revision: int | None = None
    evidence_digest: str | None = None
    fields: list[ExecutionFieldResult] = Field(default_factory=list)
    uncovered: list[UncoveredScope] = Field(default_factory=list)
    observed_dependencies: list[ObservedDependency] = Field(default_factory=list)
    observed_unrequested_order_ids: list[str] = Field(default_factory=list)
    inventory_basis: Literal["observed_orders_in_read_snapshot"] = (
        "observed_orders_in_read_snapshot"
    )
    observation: Literal["single_sqlite_read_snapshot"] = "single_sqlite_read_snapshot"
    parent_graph: Literal["not_bound_by_approval"] = "not_bound_by_approval"
    dependency_closure: Literal["not_established"] = "not_established"
    global_completeness: Literal["not_assessed"] = "not_assessed"
    authorizes_business_action: Literal[False] = False
