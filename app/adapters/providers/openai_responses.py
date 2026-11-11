"""The existing OpenAI Responses contract, including strict format and store=false."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import httpx

from app.adapters.providers.base import ParsedResponse, ProviderRequest, reported_usage
from app.config import Settings
from app.errors import LiveStartRefused

if TYPE_CHECKING:
    from app.adapters.llm import StructuredCallRequest


class OpenAIResponsesProvider:
    name = "openai"
    # Responses retains its existing downstream ParserOutput validation behavior.
    validate_payload = False

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def validate(self, settings: Settings) -> None:
        if not settings.llm_model:
            raise LiveStartRefused("MODEL_NOT_CONFIGURED")
        if (settings.llm_base_url or "").rstrip("/") != "https://api.openai.com/v1":
            raise LiveStartRefused("BASE_URL_UNSUPPORTED")

    def build_request(
        self, request: StructuredCallRequest, schema: dict[str, object]
    ) -> ProviderRequest:
        base_url = self._settings.llm_base_url
        model = self._settings.llm_model
        if base_url is None:
            raise LiveStartRefused("BASE_URL_UNSUPPORTED")
        if not model:
            raise LiveStartRefused("MODEL_NOT_CONFIGURED")
        body: dict[str, object] = {
            "model": model,
            "input": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": re.sub(r"[^a-zA-Z0-9_-]", "_", request.response_schema_id)[:64],
                    "schema": schema,
                    "strict": True,
                }
            },
            "max_output_tokens": request.max_output_tokens,
            "reasoning": {"effort": self._settings.model_reasoning_effort},
            "store": False,
            "service_tier": "default",
        }

        return ProviderRequest(
            url=base_url.rstrip("/") + "/responses",
            body=body,
            prompt_text=request.system_prompt + request.user_prompt,
        )

    def parse_response(self, response: httpx.Response) -> ParsedResponse:
        try:
            data = response.json()
            if not isinstance(data, dict):
                return ParsedResponse()
        except ValueError:
            return ParsedResponse()
        usage = reported_usage(data, "input_tokens", "output_tokens")
        if data.get("status") != "completed":
            return ParsedResponse(usage=usage, complete=False)
        try:
            output_text = "".join(
                part["text"]
                for output in data["output"]
                if output.get("type") == "message"
                for part in output["content"]
                if part.get("type") == "output_text"
            )
            payload = json.loads(output_text)
            if not isinstance(payload, dict):
                payload = None
        except (ValueError, KeyError, TypeError, AttributeError):
            payload = None
        return ParsedResponse(usage=usage, payload=payload)
