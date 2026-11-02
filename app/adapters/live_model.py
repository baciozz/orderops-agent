"""Live client guard. The HTTP transport is intentionally absent in Stage 0."""

from app.adapters.budget import BudgetLedger
from app.adapters.llm import StructuredCallRequest, StructuredCallResult, resolve_mode
from app.config import Settings
from app.errors import AppError, LiveStartRefused, LiveTransportNotReady


class LiveLLMClient:
    mode = "LIVE"

    def __init__(self, settings: Settings, ledger: BudgetLedger) -> None:
        resolution = resolve_mode(settings)
        if resolution.mode != "LIVE":
            raise LiveStartRefused(resolution.live_block_reason or "LIVE_DISABLED")
        self._settings = settings
        self._ledger = ledger

    def structured_call(self, request: StructuredCallRequest) -> StructuredCallResult:
        max_tokens = self._settings.model_max_tokens
        if request.max_output_tokens > max_tokens:
            raise AppError(
                f"单次 max_tokens {request.max_output_tokens} 超过配置上限 {max_tokens}",
                code="TOKEN_LIMIT",
                http_status=422,
            )
        self._ledger.authorize(
            input_tokens=request.input_tokens,
            max_output_tokens=request.max_output_tokens,
            task_id=request.task_id,
            max_calls_per_task=self._settings.model_max_calls_per_task,
        )
        # TODO(stage-4): call the configured official provider with OOS_LLM_API_KEY
        # read from the environment, then ledger.record() the returned usage.
        # Do not put the key in the prompt, the exception, or the log.
        raise LiveTransportNotReady()
