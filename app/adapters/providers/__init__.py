"""Provider selection; constructing a provider never sends a request."""

from app.adapters.providers.base import ModelProvider
from app.adapters.providers.openai_compatible import OpenAICompatibleChatProvider
from app.adapters.providers.openai_responses import OpenAIResponsesProvider
from app.config import Settings
from app.errors import LiveStartRefused


def provider_for_settings(settings: Settings) -> ModelProvider:
    if settings.llm_provider == "openai":
        return OpenAIResponsesProvider(settings)
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleChatProvider(settings)
    raise LiveStartRefused("PROVIDER_UNSUPPORTED")
