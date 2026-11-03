"""LLM client surface. Fixture is the default. Live is a guarded stub."""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import SecretStr
from schemas.common import ApiModel, UsageReport
from schemas.enums import RuntimeMode

from app.config import Settings


class StructuredCallRequest(ApiModel):
    fixture_key: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    input_tokens: int = 0
    max_output_tokens: int = 1000
    response_schema_id: str
    prompt_ref: str


class StructuredCallResult(ApiModel):
    fixture_key: str | None = None
    payload: dict[str, object]
    usage: UsageReport


@dataclass(frozen=True)
class ModeResolution:
    mode: RuntimeMode
    live_requested: bool
    live_block_reason: str | None


def resolve_mode(settings: Settings) -> ModeResolution:
    """Refuse to start live when the flag, prices, or key are missing.

    The process keeps running in FIXTURE. Missing prices are not filled in.
    """
    if not settings.live_enabled:
        return ModeResolution(
            mode=RuntimeMode.FIXTURE,
            live_requested=False,
            live_block_reason=None,
        )
    if settings.price_in_per_mtok is None or settings.price_out_per_mtok is None:
        return ModeResolution(
            mode=RuntimeMode.FIXTURE,
            live_requested=True,
            live_block_reason="PRICES_NOT_CONFIGURED",
        )
    if settings.llm_api_key_value() is None:
        return ModeResolution(
            mode=RuntimeMode.FIXTURE,
            live_requested=True,
            live_block_reason="KEY_NOT_CONFIGURED",
        )
    return ModeResolution(mode=RuntimeMode.LIVE, live_requested=True, live_block_reason=None)


class LLMClient(Protocol):
    mode: Literal["FIXTURE", "LIVE"]

    def structured_call(self, request: StructuredCallRequest) -> StructuredCallResult:
        """Return a structured payload. Implementations must not log secrets."""


def mask_secret(value: SecretStr | None) -> str:
    return "" if value is None else "set"
