"""SubAgent contract and the unified AgentProposal envelope (revision 2.2–2.3)."""

from typing import Literal

from pydantic import Field, model_validator

from schemas import SCHEMA_VERSION
from schemas.common import ApiModel, Issue, SourceRef, UsageReport
from schemas.enums import AgentKind


class SchemaRef(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    model_id: str
    model_version: str


class AgentBudget(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    uses_model: bool
    max_tokens_per_call: int | None = None
    max_calls_per_task: int | None = None

    @model_validator(mode="after")
    def model_agents_declare_limits(self) -> "AgentBudget":
        if self.uses_model and (
            self.max_tokens_per_call is None or self.max_calls_per_task is None
        ):
            raise ValueError("使用模型的子 Agent 必须声明每次 max_tokens 和每任务调用次数")
        if self.max_tokens_per_call is not None and self.max_tokens_per_call < 1:
            raise ValueError("max_tokens_per_call 必须为正")
        if self.max_calls_per_task is not None and self.max_calls_per_task < 1:
            raise ValueError("max_calls_per_task 必须为正")
        return self


class AgentScope(ApiModel):
    """Query-layer scope. Enforcement itself arrives with tool execution (stage 1C)."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    binds: list[Literal["order_id", "source_id", "sku_id"]]
    read_only: Literal[True] = True


class SubAgentSpec(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    agent_id: str
    version: str
    kind: AgentKind
    capabilities: list[str] = Field(min_length=1)
    handles: list[str] = Field(default_factory=list)
    input_schema: SchemaRef
    output_schema: SchemaRef
    tools: list[str] = Field(default_factory=list)
    scope: AgentScope
    timeout_s: int = 30
    max_steps: int = 8
    max_retries: int = 2
    budget: AgentBudget


class AgentProposal(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    agent_id: str
    agent_version: str
    task_id: str
    intent_revision: int
    fencing_token: int
    input_hash: str
    payload: dict[str, object]
    source_refs: list[SourceRef] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    usage: UsageReport
