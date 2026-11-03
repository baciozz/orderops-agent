"""Plan and new-order drafts. Hashing and precheck are stage 1A/1B."""

from typing import Literal

from pydantic import Field

from schemas import SCHEMA_VERSION
from schemas.business import LineChange, NewOrderLine
from schemas.common import ApiModel, SourceRef
from schemas.downstream import DocVerdict
from schemas.enums import PlanKind


class PlanDraft(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str
    plan_kind: PlanKind
    order_id: str | None = None
    base_revision: int = Field(ge=0)
    intent_revision: int = Field(ge=0)
    chain_intent_revision: int | None = None
    changes: list[LineChange] = Field(default_factory=list)
    new_lines: list[NewOrderLine] = Field(default_factory=list)
    expected_stock_revisions: dict[str, int] = Field(default_factory=dict)
    expected_doc_revisions: dict[str, int] = Field(default_factory=dict)
    downstream_changes: list[DocVerdict] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
