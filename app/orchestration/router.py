"""Route by required capability, never by a hard-coded agent name.

Missing a required capability returns CAPABILITY_MISSING. The caller marks the
task failed. The router does not skip that step.
"""

from typing import Literal

from pydantic import Field
from schemas.common import ApiModel
from schemas.enums import BusinessEventType, InputMode

from app.orchestration.registry import Registry

PARSE_BY_MODE = {
    InputMode.TEXT: "parse.text",
    InputMode.CHAT: "parse.chat",
    InputMode.MD: "parse.md",
    InputMode.XLSX: "parse.xlsx",
}


class PlannedStep(ApiModel):
    capability: str
    agent_id: str | None
    agent_version: str | None
    skipped: bool = False
    skip_reason: str | None = None


class RouteSuccess(ApiModel):
    ok: Literal[True] = True
    event_type: str
    disposition: Literal["dispatch", "read_service", "unspecified"]
    steps: list[PlannedStep] = Field(default_factory=list)
    note: str | None = None


class RouteFailure(ApiModel):
    ok: Literal[False] = False
    code: Literal["CAPABILITY_MISSING", "UNSUPPORTED_EVENT"]
    event_type: str
    capability: str | None = None
    message: str


class Router:
    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def plan(
        self,
        event_type: str,
        *,
        input_mode: str | None = None,
        has_downstream: bool | None = None,
    ) -> RouteSuccess | RouteFailure:
        if event_type == "unstructured":
            return self._plan_unstructured(input_mode)
        if event_type == BusinessEventType.QUERY:
            return RouteSuccess(event_type=event_type, disposition="read_service", steps=[])
        if event_type == BusinessEventType.CLARIFY_ANSWER:
            return RouteSuccess(
                event_type=event_type,
                disposition="unspecified",
                steps=[],
                note="TODO(stage-1C): v1.1 路由表没有单列 clarify_answer 的能力，不能猜测下一跳",
            )
        if event_type == BusinessEventType.NEW_ORDER:
            return self._dispatch(event_type, ["check.inventory"], skipped=[])
        if event_type in (BusinessEventType.CHANGE_ORDER, BusinessEventType.CANCEL_REMAINING):
            if has_downstream is None:
                return RouteFailure(
                    code="UNSUPPORTED_EVENT",
                    event_type=event_type,
                    message="改单或取消路由必须显式说明是否存在下游单据",
                )
            skipped: list[tuple[str, str]] = []
            required = ["check.inventory"]
            if has_downstream:
                required.append("check.downstream")
            else:
                skipped.append(("check.downstream", "无下游单据"))
            return self._dispatch(event_type, required, skipped=skipped)
        return RouteFailure(
            code="UNSUPPORTED_EVENT",
            event_type=event_type,
            message=f"未知事件类型 {event_type}，任务失败，未派发",
        )

    def _plan_unstructured(self, input_mode: str | None) -> RouteSuccess | RouteFailure:
        if input_mode is None:
            return RouteFailure(
                code="UNSUPPORTED_EVENT",
                event_type="unstructured",
                message="非结构化输入必须带 input_mode",
            )
        try:
            mode = InputMode(input_mode)
        except ValueError:
            return RouteFailure(
                code="UNSUPPORTED_EVENT",
                event_type="unstructured",
                message=f"不支持的输入类型 {input_mode}",
            )
        return self._dispatch("unstructured", [PARSE_BY_MODE[mode]], skipped=[])

    def _dispatch(
        self,
        event_type: str,
        required: list[str],
        *,
        skipped: list[tuple[str, str]],
    ) -> RouteSuccess | RouteFailure:
        steps: list[PlannedStep] = []
        for capability in required:
            spec = self._registry.find_by_capability(capability)
            if spec is None:
                return RouteFailure(
                    code="CAPABILITY_MISSING",
                    event_type=event_type,
                    capability=capability,
                    message=(
                        f"没有子 Agent 提供能力 {capability}，"
                        "任务失败（CAPABILITY_MISSING），不能静默跳过"
                    ),
                )
            steps.append(
                PlannedStep(
                    capability=capability,
                    agent_id=spec.agent_id,
                    agent_version=spec.version,
                    skipped=False,
                )
            )
        for capability, reason in skipped:
            spec = self._registry.find_by_capability(capability)
            steps.append(
                PlannedStep(
                    capability=capability,
                    agent_id=spec.agent_id if spec else None,
                    agent_version=spec.version if spec else None,
                    skipped=True,
                    skip_reason=reason,
                )
            )
        return RouteSuccess(event_type=event_type, disposition="dispatch", steps=steps)
