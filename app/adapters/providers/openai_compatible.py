"""Conservative OpenAI-compatible Chat Completions JSON intent provider."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import httpx

from app.adapters.providers.base import ParsedResponse, ProviderRequest, reported_usage
from app.adapters.providers.urls import validate_compatible_url
from app.config import Settings
from app.errors import LiveStartRefused

if TYPE_CHECKING:
    from app.adapters.llm import StructuredCallRequest


class OpenAICompatibleChatProvider:
    name = "openai_compatible"
    validate_payload = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def validate(self, settings: Settings) -> None:
        loopback = validate_compatible_url(settings.llm_base_url, settings.llm_api_key_value())
        if not settings.llm_model:
            raise LiveStartRefused("MODEL_NOT_CONFIGURED")
        if not loopback and settings.llm_api_key_value() is None:
            raise LiveStartRefused("KEY_NOT_CONFIGURED")

    def build_request(
        self, request: StructuredCallRequest, schema: dict[str, object]
    ) -> ProviderRequest:
        base_url = self._settings.llm_base_url
        model = self._settings.llm_model
        if not base_url:
            raise LiveStartRefused("BASE_URL_NOT_CONFIGURED")
        if not model:
            raise LiveStartRefused("MODEL_NOT_CONFIGURED")
        system = request.system_prompt
        response_format: dict[str, object] = {"type": "json_object"}
        if self._settings.llm_json_schema_supported:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": re.sub(r"[^a-zA-Z0-9_-]", "_", request.response_schema_id)[:64],
                    "schema": schema,
                    "strict": True,
                },
            }
        else:
            system += (
                "\nReturn only a JSON object that strictly follows this JSON Schema. "
                "Do not add fields or Markdown fences. Schema: " + json.dumps(schema)
            )
        body: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": request.user_prompt},
            ],
            "max_tokens": request.max_output_tokens,
            "temperature": 0,
            "stream": False,
            "response_format": response_format,
        }

        return ProviderRequest(
            url=base_url.rstrip("/") + "/chat/completions",
            body=body,
            prompt_text=system + request.user_prompt,
        )

    def parse_response(self, response: httpx.Response) -> ParsedResponse:
        try:
            data = response.json()
            if not isinstance(data, dict):
                return ParsedResponse()
        except ValueError:
            return ParsedResponse()
        usage = reported_usage(data, "prompt_tokens", "completion_tokens")
        try:
            choice = data["choices"][0]
            if choice.get("finish_reason") != "stop":
                return ParsedResponse(usage=usage, complete=False)
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError
            payload = json.loads(content)
            if not isinstance(payload, dict):
                payload = None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            payload = None
        return ParsedResponse(usage=usage, payload=payload)
