"""Schema catalog consulted when a sub-agent is registered.

A missing or version-mismatched model id fails registration. The process
does not start with an incomplete contract.
"""

from pydantic import BaseModel

from schemas.business import BusinessEvent, ExtractionResult, ParserInput
from schemas.downstream import DocCheckInput, DocCheckOutput
from schemas.inventory import InventoryCheckInput, InventoryCheckOutput
from schemas.subagent import AgentProposal
from schemas.task_view import TaskView

CATALOG_MODELS: dict[str, type[BaseModel]] = {
    "parser.input": ParserInput,
    "parser.output": ExtractionResult,
    "business.event": BusinessEvent,
    "inventory.input": InventoryCheckInput,
    "inventory.output": InventoryCheckOutput,
    "doccheck.input": DocCheckInput,
    "doccheck.output": DocCheckOutput,
    "agent.proposal": AgentProposal,
    "task.view": TaskView,
}


def schema_version_of(model: type[BaseModel]) -> str:
    field = model.model_fields.get("schema_version")
    if field is None or field.default is None:
        raise ValueError(f"{model.__name__} 没有 schema_version 默认值")
    return str(field.default)


def lookup_schema(model_id: str, model_version: str) -> type[BaseModel] | None:
    model = CATALOG_MODELS.get(model_id)
    if model is None:
        return None
    if schema_version_of(model) != model_version:
        return None
    return model
