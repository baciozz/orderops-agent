"""The three P0 sub-agents. They propose. They do not approve or commit."""

from dataclasses import dataclass, field

from pydantic import ValidationError
from schemas.business import ExtractionResult
from schemas.catalog import lookup_schema
from schemas.common import UsageReport
from schemas.downstream import DocCheckOutput
from schemas.inventory import InventoryCheckOutput
from schemas.subagent import AgentProposal, SubAgentSpec
from sqlalchemy.orm import Session

from app.adapters.fixture_model import FixtureLLMClient
from app.adapters.llm import StructuredCallRequest
from app.api.sources import task_spans
from app.db.models import SalesOrder, Task
from app.domain.hashing import canonical_hash
from app.errors import AppError, CallLimitExceeded, SchemaInvalid
from app.orchestration.fixtures import compare_sheet_quantities
from app.orchestration.proposal import accept_proposal
from app.orchestration.registry import invoke_tool


@dataclass
class StepContext:
    session: Session
    task: Task
    spec: SubAgentSpec
    text: str
    input_mode: str
    source_id: str
    llm: FixtureLLMClient
    intent: int
    fence: int
    model_calls: int = 0
    source_ids: list[str] = field(default_factory=list)

    def call_tool(self, name: str, **kwargs: object) -> dict[str, object]:
        return invoke_tool(self.spec, name, **kwargs)


@dataclass
class AgentRun:
    payload: dict[str, object]
    extraction: ExtractionResult | None = None
    tokens: int = 0


def run_parser(ctx: StepContext) -> AgentRun:
    ctx.call_tool("source.read", source_id=ctx.source_id)
    call_limit = ctx.spec.budget.max_calls_per_task
    if call_limit is not None and ctx.model_calls >= call_limit:
        raise CallLimitExceeded(
            f"{ctx.spec.agent_id} 已达到每任务 {call_limit} 次模型调用上限",
            details={"agent_id": ctx.spec.agent_id},
        )
    ctx.model_calls += 1
    limit = ctx.spec.budget.max_tokens_per_call or 1000
    result = ctx.llm.structured_call(
        StructuredCallRequest(
            fixture_key=ctx.text,
            task_id=ctx.task.id,
            agent_id=ctx.spec.agent_id,
            max_output_tokens=limit,
            response_schema_id=ctx.spec.output_schema.model_id,
            prompt_ref="fixture://parser",
        )
    )
    extracted = bind_extraction(
        result.payload,
        task_id=ctx.task.id,
        intent=ctx.intent,
        mode=ctx.input_mode,
    )
    event = extracted.business_event
    if event is not None and event.order_id:
        order = ctx.session.get(SalesOrder, event.order_id)
        if order is None:
            raise AppError(f"订单 {event.order_id} 不存在", code="NOT_FOUND", http_status=404)
        ctx.intent = order.intent_revision
        ctx.task.order_id = order.id
        ctx.task.chain_id = f"CH-{order.id}"
        ctx.task.intent_revision = order.intent_revision
        extracted = bind_extraction(
            result.payload,
            task_id=ctx.task.id,
            intent=ctx.intent,
            mode=ctx.input_mode,
        )
    if ctx.source_ids:
        spans = [
            span
            for source_id in dict.fromkeys(ctx.source_ids)
            for span in task_spans(ctx.session, ctx.task.id, source_id)
        ]
        extracted = compare_sheet_quantities(extracted, spans)
    _accept(ctx, extracted.model_dump(mode="json"), result.usage)
    tokens = result.usage.input_tokens + result.usage.output_tokens
    return AgentRun(payload=extracted.model_dump(mode="json"), extraction=extracted, tokens=tokens)


def run_inventory(ctx: StepContext) -> AgentRun:
    ctx.call_tool("order.get", order_id=ctx.task.order_id)
    payload = ctx.call_tool("inventory.check")
    checked = _validate(InventoryCheckOutput, payload, ctx.spec)
    _accept(ctx, checked, UsageReport(mode="FIXTURE", cost_usd="0"))
    return AgentRun(payload=checked)


def run_doc_check(ctx: StepContext) -> AgentRun:
    ctx.call_tool("order.get", order_id=ctx.task.order_id)
    payload = ctx.call_tool("downstream.get")
    checked = _validate(DocCheckOutput, payload, ctx.spec)
    _accept(ctx, checked, UsageReport(mode="FIXTURE", cost_usd="0"))
    return AgentRun(payload=checked)


RUNNERS = {
    "parser": run_parser,
    "inventory-check": run_inventory,
    "doc-check": run_doc_check,
}


def bind_extraction(
    payload: dict[str, object],
    *,
    task_id: str,
    intent: int,
    mode: str,
) -> ExtractionResult:
    data = dict(payload)
    supplied_task = data.get("task_id")
    if supplied_task not in (None, task_id):
        raise SchemaInvalid("解析结果的 task_id 与当前任务不一致")
    data["task_id"] = task_id
    supplied_intent = data.get("intent_revision")
    if supplied_intent not in (None, intent):
        raise SchemaInvalid("解析结果的 intent_revision 与当前任务不一致")
    data["intent_revision"] = intent
    data.setdefault("input_mode", mode)
    data.setdefault("schema_version", "1.0")
    try:
        return ExtractionResult.model_validate(data)
    except ValidationError as exc:
        raise SchemaInvalid(f"解析输出不合 schema：{exc.error_count()} 个错误") from exc


def _validate(
    model: type[InventoryCheckOutput] | type[DocCheckOutput],
    payload: dict[str, object],
    spec: SubAgentSpec,
) -> dict[str, object]:
    catalog = lookup_schema(spec.output_schema.model_id, spec.output_schema.model_version)
    if catalog is None:
        raise SchemaInvalid(f"未登记的输出 schema {spec.output_schema.model_id}")
    try:
        parsed = model.model_validate(payload)
    except ValidationError as exc:
        raise SchemaInvalid(f"{spec.agent_id} 输出不合 schema：{exc.error_count()} 个错误") from exc
    return parsed.model_dump(mode="json")


def _accept(ctx: StepContext, payload: dict[str, object], usage: UsageReport) -> None:
    proposal = AgentProposal(
        agent_id=ctx.spec.agent_id,
        agent_version=ctx.spec.version,
        task_id=ctx.task.id,
        intent_revision=ctx.intent,
        fencing_token=ctx.fence,
        input_hash=canonical_hash({"task_id": ctx.task.id, "text": ctx.text}),
        payload=payload,
        usage=usage,
    )
    accept_proposal(
        proposal,
        expected_fencing_token=ctx.fence,
        expected_intent_revision=ctx.intent,
    )
