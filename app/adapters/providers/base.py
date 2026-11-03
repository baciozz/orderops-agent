"""Wire-format providers; HTTP, credentials, and accounting belong to the client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx

from app.config import Settings

if TYPE_CHECKING:
    from app.adapters.llm import StructuredCallRequest


@dataclass(frozen=True)
class ProviderRequest:
    url: str
    body: dict[str, object]
    # The actual system/user text, including any provider-added instructions.
    prompt_text: str


@dataclass(frozen=True)
class ParsedResponse:
    usage: tuple[int, int] | None = None
    complete: bool = True
    payload: dict[str, object] | None = None


def reported_usage(data: dict, input_field: str, output_field: str) -> tuple[int, int] | None:
    try:
        usage = data["usage"]
        tokens = usage[input_field], usage[output_field]
        if any(type(n) is not int or n < 0 for n in tokens):
            return None
        return tokens
    except (KeyError, TypeError):
        return None


class ModelProvider(Protocol):
    name: str
    validate_payload: bool

    def validate(self, settings: Settings) -> None:
        """Refuse unsupported configuration without HTTP or exposing credentials."""

    def build_request(
        self, request: StructuredCallRequest, schema: dict[str, object]
    ) -> ProviderRequest:
        """Return URL, JSON body and actual prompt text, without credentials."""

    def parse_response(self, response: httpx.Response) -> ParsedResponse:
        """Return usage even when the untrusted output is malformed."""
