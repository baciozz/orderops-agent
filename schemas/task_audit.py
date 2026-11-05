"""Independent v1 audit export. The frozen TaskView contract is unchanged."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StringConstraints

from schemas.common import ApiModel
from schemas.enums import ApprovalStatus, PlanKind, PlanStatus, TaskStatus

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64)]
PlanHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[StrictInt, Field(ge=0)]


class AuditTask(ApiModel):
    task_id: Identifier
    order_id: Identifier | None
    status: TaskStatus
    intent_revision: Count
    cancel_requested: bool


class AuditInput(ApiModel):
    event_id: Identifier
    input_sequence: StrictInt | None
    kind: Literal["input.submit", "clarify_answer", "unknown"]
    received_at: datetime
    intent_revision: Count | None


class AuditApproval(ApiModel):
    status: ApprovalStatus
    intent_revision: Count
    expires_at: datetime
    consumed_operation_id: Identifier | None


class AuditPlan(ApiModel):
    report_ref: Identifier
    kind: PlanKind
    status: PlanStatus
    base_order_revision: Count
    intent_revision: Count
    chain_intent_revision: Count | None
    plan_hash: PlanHash
    source_ids: list[Identifier]
    approvals: list[AuditApproval] = Field(default_factory=list)


class CommitReceipt(ApiModel):
    order_id: Identifier
    revision: StrictInt = Field(ge=1)
    audit_id: Identifier


class CurrentVerification(ApiModel):
    pending: bool
    retry_attempts: Count
    next_attempt_at: datetime | None
    manual_required: bool


class AuditOperation(ApiModel):
    operation_id: Identifier
    plan_ref: Identifier
    receipt: CommitReceipt
    current_verification: CurrentVerification


class BusinessAudit(ApiModel):
    audit_id: Identifier
    operation_id: Identifier
    plan_ref: Identifier
    action: Literal["commit"]
    actor_type: Literal["human", "subagent", "system", "unknown"]
    object_id: Identifier
    at: datetime
    source_ids: list[Identifier]


class AuditSource(ApiModel):
    source_id: Identifier
    sha256: Digest
    media_type: Literal[
        "text/plain",
        "text/markdown",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ]
    size: Count
    parser_version: Literal["static-v1"]


class AuditSemantics(ApiModel):
    observation: Literal["single_read_transaction"] = "single_read_transaction"
    history: Literal["not_a_complete_historical_snapshot"] = "not_a_complete_historical_snapshot"
    runtime_mode: Literal["not_attested_by_this_report"] = "not_attested_by_this_report"
    receipt: Literal["stored_commit_receipt"] = "stored_commit_receipt"
    verification: Literal["recorded_current_status_not_rechecked"] = (
        "recorded_current_status_not_rechecked"
    )
    audit_scope: Literal["commit_business_audits_only"] = "commit_business_audits_only"
    legacy_audit_hashes: Literal["plan_hash_not_business_before_after_state"] = (
        "plan_hash_not_business_before_after_state"
    )
    input_plan_link: Literal["not_recorded_not_inferred"] = "not_recorded_not_inferred"
    input_order: Literal["sequence_then_stable_legacy_fallback"] = (
        "sequence_then_stable_legacy_fallback"
    )
    causal_sequence_complete: bool
    sources: Literal["task_owned_metadata_only_not_content_verified"] = (
        "task_owned_metadata_only_not_content_verified"
    )


class AuditLimits(ApiModel):
    rows_per_collection: int
    output_bytes: int


class TaskAudit(ApiModel):
    export_version: Literal["1.0"] = "1.0"
    semantics: AuditSemantics
    limits: AuditLimits
    task: AuditTask
    inputs: list[AuditInput]
    plans: list[AuditPlan]
    committed_operations: list[AuditOperation]
    business_audits: list[BusinessAudit]
    sources: list[AuditSource]
