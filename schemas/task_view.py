"""Task view with explicit limited verification observation and legacy unknown state."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from schemas import SCHEMA_VERSION
from schemas.common import ApiModel, SourceRef
from schemas.enums import (
    DisabledReason,
    DocType,
    LockedKind,
    RuntimeMode,
    TaskStatus,
    Verdict,
    VerificationState,
    WaitingOn,
)
from schemas.verification_observation import VerificationObservation


class TaskCard(ApiModel):
    task_id: str
    order_id: str | None
    status: TaskStatus
    intent_revision: int
    waiting_on: WaitingOn
    mode: RuntimeMode


class DispatchStepView(ApiModel):
    step_no: int
    agent_id: str
    agent_version: str
    capability: str
    status: str
    duration_ms: int | None = None
    tokens: int | None = None
    cost_usd: str | None = None
    error_code: str | None = None
    skip_reason: str | None = None


class LineDiff(ApiModel):
    sku_id: str
    old_qty: int | None
    new_qty: int | None
    delta: int | None
    locked_qty: int
    locked_kind: LockedKind
    unexecuted_qty: int | None
    reservation_delta: int | None
    available_qty: int | None


class DownstreamImpact(ApiModel):
    doc_id: str
    doc_type: DocType
    linked_revision: int | None
    status: str
    verdict: Verdict | None
    before: dict[str, object] | None = None
    after: dict[str, object] | None = None
    executable: bool
    reason: str | None = None


class ApprovalBlock(ApiModel):
    plan_hash: str | None
    generated_at: datetime | None
    expires_at: datetime | None
    approvable: bool
    disabled_reason: DisabledReason | None


class TaskResultBlock(ApiModel):
    operation_id: str | None
    revision: int | None
    verification: VerificationState | None
    verification_manual_required: bool = False
    verification_retry_attempts: int = 0
    verification_next_attempt_at: datetime | None = None
    verification_details: VerificationObservation | None = None
    download_url: str | None


class TaskView(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    card: TaskCard
    dispatch: list[DispatchStepView] = Field(default_factory=list)
    line_diffs: list[LineDiff] = Field(default_factory=list)
    downstream: list[DownstreamImpact] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    approval: ApprovalBlock
    result: TaskResultBlock
