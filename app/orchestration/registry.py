"""In-memory sub-agent registry.

Loaded at startup from code configuration. Duplicate agent_id + major version,
a missing schema, or kind=external fails registration. There is no runtime
self-registration API.
"""

from schemas.catalog import lookup_schema
from schemas.enums import AgentKind
from schemas.subagent import SubAgentSpec

from app.errors import RegistryError, ToolNotAllowed

# Tools a sub-agent must never receive, even if a spec lists them.
FORBIDDEN_TOOLS = frozenset(
    {
        "approve_plan",
        "commit",
        "plans.approve",
        "plans.commit",
        "order.commit",
    }
)


def major_version(version: str) -> int:
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise RegistryError(
            f"版本号必须是主.次.修订，收到 {version}",
            code="BAD_VERSION",
        )
    return int(parts[0])


class Registry:
    def __init__(self) -> None:
        self._by_major: dict[tuple[str, int], SubAgentSpec] = {}

    def register(self, spec: SubAgentSpec) -> None:
        if spec.kind == AgentKind.EXTERNAL:
            raise RegistryError(
                "外部 Agent 属于 P1，当前阶段拒绝注册",
                code="EXTERNAL_NOT_ENABLED",
            )
        if spec.kind == AgentKind.MODEL and not spec.budget.uses_model:
            raise RegistryError(
                f"{spec.agent_id} 的 kind=model 但 budget.uses_model 为假",
                code="SCHEMA_MISSING",
            )
        for ref in (spec.input_schema, spec.output_schema):
            if lookup_schema(ref.model_id, ref.model_version) is None:
                raise RegistryError(
                    f"子 Agent {spec.agent_id} 引用了未登记的 schema "
                    f"{ref.model_id}@{ref.model_version}，启动失败",
                    code="SCHEMA_MISSING",
                    details={"agent_id": spec.agent_id, "model_id": ref.model_id},
                )
        key = (spec.agent_id, major_version(spec.version))
        if key in self._by_major:
            raise RegistryError(
                f"子 Agent {spec.agent_id} 主版本 {key[1]} 已注册，启动失败",
                code="REGISTRY_DUPLICATE",
                details={"agent_id": spec.agent_id, "major": key[1]},
            )
        self._by_major[key] = spec

    def specs(self) -> tuple[SubAgentSpec, ...]:
        return tuple(self._by_major.values())

    def find_by_capability(self, capability: str) -> SubAgentSpec | None:
        matches = [spec for spec in self._by_major.values() if capability in spec.capabilities]
        if len(matches) > 1:
            ids = ", ".join(sorted(spec.agent_id for spec in matches))
            raise RegistryError(
                f"能力 {capability} 同时由多个子 Agent 提供（{ids}），不能静默选择",
                code="AMBIGUOUS_CAPABILITY",
                details={"capability": capability},
            )
        if not matches:
            return None
        return matches[0]


def load_registry(specs: list[SubAgentSpec]) -> Registry:
    registry = Registry()
    for spec in specs:
        registry.register(spec)
    return registry


def check_tool_whitelist(spec: SubAgentSpec, tool_name: str) -> None:
    """Reject tools outside the spec, and always reject approve/commit."""
    if tool_name in FORBIDDEN_TOOLS or tool_name not in spec.tools:
        raise ToolNotAllowed(
            f"工具 {tool_name} 不在 {spec.agent_id} 的只读白名单内",
            details={"agent_id": spec.agent_id, "tool": tool_name},
        )


def invoke_tool(spec: SubAgentSpec, tool_name: str, *_args: object, **_kwargs: object) -> None:
    check_tool_whitelist(spec, tool_name)
    raise NotImplementedError("TODO(stage-1C): 在查询层按任务范围执行只读工具，并写审计")
