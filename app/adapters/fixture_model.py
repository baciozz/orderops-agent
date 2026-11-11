"""Fixed fixture responses. A successful fixture call is not evidence of a live model."""

import time
from datetime import UTC, datetime
from typing import Literal

from schemas.common import UsageReport

from app.adapters.call_trace import CallTraceStore, draft_from_request
from app.adapters.llm import StructuredCallRequest, StructuredCallResult
from app.errors import FixtureNotFound
from app.observability import get_logger, observed

logger = get_logger(__name__)


class FixtureLLMClient:
    mode: Literal["FIXTURE", "LIVE"] = "FIXTURE"

    def __init__(
        self,
        fixtures: dict[str, dict[str, object]] | None = None,
        *,
        traces: CallTraceStore | None = None,
    ) -> None:
        self._fixtures = fixtures or {}
        self._traces = traces

    def _write_trace(
        self,
        request: StructuredCallRequest,
        *,
        started_at: datetime,
        started_mono: float,
        status: str,
        error_code: str | None,
        schema_valid: bool,
    ) -> None:
        if self._traces is None:
            return
        self._traces.record_fixture(
            draft_from_request(
                request,
                provider="fixture",
                model="fixture",
                started_at=started_at,
                elapsed_ms=int((time.perf_counter() - started_mono) * 1000),
                status=status,
                error_code=error_code,
                schema_valid=schema_valid,
            )
        )

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
        started_at = datetime.now(UTC)
        started_mono = time.perf_counter()
        try:
            if not request.fixture_key or request.fixture_key not in self._fixtures:
                raise FixtureNotFound(
                    f"没有 fixture {request.fixture_key or ''}，不会改为调用 live 模型",
                    details={"fixture_key": request.fixture_key},
                )
            result = StructuredCallResult(
                fixture_key=request.fixture_key,
                payload=self._fixtures[request.fixture_key],
                usage=UsageReport(mode="FIXTURE", cost_usd="0"),
            )
        except FixtureNotFound as exc:
            self._write_trace(
                request,
                started_at=started_at,
                started_mono=started_mono,
                status="error",
                error_code=exc.code,
                schema_valid=False,
            )
            raise
        self._write_trace(
            request,
            started_at=started_at,
            started_mono=started_mono,
            status="ok",
            error_code=None,
            schema_valid=True,
        )
        return result
