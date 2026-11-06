"""LLM client surface. Fixture is the default; live requires explicit configuration."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import Field, SecretStr
from schemas.common import ApiModel, UsageReport
from schemas.enums import RuntimeMode

from app.config import Settings
from app.errors import LiveStartRefused

if TYPE_CHECKING:
    from app.adapters.providers.base import ModelProvider


class StructuredCallRequest(ApiModel):
    fixture_key: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    input_tokens: int = 0
    max_output_tokens: int = 1000
    response_schema_id: str
    prompt_ref: str
    system_prompt: str = ""
    user_prompt: str = ""
    response_schema: dict[str, object] = Field(default_factory=dict)


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
    """Refuse live when the flag, prices, required key, or compatible config are missing.

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
    if settings.llm_provider == "openai_compatible":
        from app.adapters.providers.openai_compatible import OpenAICompatibleChatProvider

        try:
            OpenAICompatibleChatProvider(settings).validate(settings)
        except LiveStartRefused as exc:
            return ModeResolution(
                mode=RuntimeMode.FIXTURE,
                live_requested=True,
                live_block_reason=exc.reason,
            )
    # Unknown providers are refused by provider selection, rather than misreported
    # as a missing OpenAI key. Preserve the existing unconfigured-provider fallback.
    elif settings.llm_provider in (None, "openai") and settings.llm_api_key_value() is None:
        return ModeResolution(
            mode=RuntimeMode.FIXTURE,
            live_requested=True,
            live_block_reason="KEY_NOT_CONFIGURED",
        )
    return ModeResolution(mode=RuntimeMode.LIVE, live_requested=True, live_block_reason=None)


def validated_live_provider(settings: Settings) -> "ModelProvider":
    """Validate every local transport prerequisite without I/O, billing or logging.

    resolve_mode is the existing intake/fallback policy, not provider readiness.
    Consumers must use this full check before spending a durable task attempt.
    """
    from app.adapters.providers import provider_for_settings

    resolution = resolve_mode(settings)
    if resolution.mode != "LIVE":
        raise LiveStartRefused(resolution.live_block_reason or "LIVE_DISABLED")
    provider = provider_for_settings(settings)
    provider.validate(settings)
    return provider


class LLMClient(Protocol):
    mode: Literal["FIXTURE", "LIVE"]

    def structured_call(self, request: StructuredCallRequest) -> StructuredCallResult:
        """Return a structured payload. Implementations must not log secrets."""


def mask_secret(value: SecretStr | None) -> str:
    return "" if value is None else "set"
