"""A scoped observation, never a business approval or a global completeness claim."""

from typing import Literal

from pydantic import Field

from schemas.common import ApiModel

FindingCode = Literal[
    "EXPECTED_LINK_MISSING", "LINK_IDENTITY_MISMATCH", "DUPLICATE_LINK", "UNEXPECTED_LINK"
]


class LinkAuditFinding(ApiModel):
    code: FindingCode
    doc_type: str
    doc_id: str
    link_ids: list[str] = Field(default_factory=list)


class LinkAudit(ApiModel):
    schema_version: Literal["1.0"] = "1.0"
    operation_id: str
    scope: Literal["retained_committed_operation"] = "retained_committed_operation"
    status: Literal["consistent_for_operation", "findings", "uncertain"]
    reason_code: str | None = None
    order_id: str | None = None
    order_revision: int | None = None
    expected_link_count: int | None = None
    matched_link_count: int = 0
    findings: list[LinkAuditFinding] = Field(default_factory=list)
    observation: Literal["single_sqlite_read_snapshot"] = "single_sqlite_read_snapshot"
    global_completeness: Literal["not_assessed"] = "not_assessed"
    current_ownership: Literal["not_assessed"] = "not_assessed"
    authorizes_business_action: Literal[False] = False
