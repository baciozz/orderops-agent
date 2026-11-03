"""Fixed fixture responses. A successful fixture call is not evidence of a live model."""

from typing import Literal

from schemas.common import UsageReport

from app.adapters.llm import StructuredCallRequest, StructuredCallResult
from app.errors import FixtureNotFound
from app.observability import get_logger, observed

logger = get_logger(__name__)


class FixtureLLMClient:
    mode: Literal["FIXTURE", "LIVE"] = "FIXTURE"

    def __init__(self, fixtures: dict[str, dict[str, object]] | None = None) -> None:
        self._fixtures = fixtures or {}

    @observed(
        logger,
        "model.call",
        fields=lambda self, request: {
            "mode": self.mode,
            "model": "fixture",
            "task_id": request.task_id,
            "prompt_kind": request.response_schema_id,
            "input_tokens": 0,
            "output_tokens": 0,
        },
    )
    def structured_call(self, request: StructuredCallRequest) -> StructuredCallResult:
        if not request.fixture_key or request.fixture_key not in self._fixtures:
            raise FixtureNotFound(
                f"没有 fixture {request.fixture_key or ''}，不会改为调用 live 模型",
                details={"fixture_key": request.fixture_key},
            )
        return StructuredCallResult(
            fixture_key=request.fixture_key,
            payload=self._fixtures[request.fixture_key],
            usage=UsageReport(mode="FIXTURE", cost_usd="0"),
        )
