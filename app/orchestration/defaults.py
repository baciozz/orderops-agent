"""The three P0 sub-agent specs. Stage 1C executes them from the dispatcher."""

from schemas.enums import AgentKind
from schemas.subagent import AgentBudget, AgentScope, SchemaRef, SubAgentSpec

from app.orchestration.registry import Registry, load_registry

_SCOPE = AgentScope(binds=["order_id", "source_id", "sku_id"])


def _ref(model_id: str) -> SchemaRef:
    return SchemaRef(model_id=model_id, model_version="1.0")


def parser_spec() -> SubAgentSpec:
    return SubAgentSpec(
        agent_id="parser",
        version="0.1.0",
        kind=AgentKind.MODEL,
        capabilities=["parse.text", "parse.chat", "parse.md", "parse.xlsx"],
        handles=["unstructured"],
        input_schema=_ref("parser.input"),
        output_schema=_ref("parser.output"),
        tools=["source.read"],
        scope=_SCOPE,
        budget=AgentBudget(uses_model=True, max_tokens_per_call=1000, max_calls_per_task=3),
    )


def inventory_spec() -> SubAgentSpec:
    return SubAgentSpec(
        agent_id="inventory-check",
        version="0.1.0",
        kind=AgentKind.DETERMINISTIC,
        capabilities=["check.inventory"],
        handles=["new_order", "change_order", "cancel_remaining"],
        input_schema=_ref("inventory.input"),
        output_schema=_ref("inventory.output"),
        tools=["order.get", "inventory.check"],
        scope=_SCOPE,
        budget=AgentBudget(uses_model=False),
    )


def doc_check_spec() -> SubAgentSpec:
    return SubAgentSpec(
        agent_id="doc-check",
        version="0.1.0",
        kind=AgentKind.DETERMINISTIC,
        capabilities=["check.downstream"],
        handles=["change_order", "cancel_remaining"],
        input_schema=_ref("doccheck.input"),
        output_schema=_ref("doccheck.output"),
        tools=["order.get", "downstream.get"],
        scope=_SCOPE,
        budget=AgentBudget(uses_model=False),
    )


def default_specs() -> list[SubAgentSpec]:
    return [parser_spec(), inventory_spec(), doc_check_spec()]


def build_default_registry() -> Registry:
    return load_registry(default_specs())
