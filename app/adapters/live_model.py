"""Guarded synchronous model transport. Providers never send or manage billing."""

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from schemas.common import UsageReport

from app.adapters.budget import BudgetLedger
from app.adapters.llm import StructuredCallRequest, StructuredCallResult, resolve_mode
from app.adapters.providers import provider_for_settings
from app.adapters.providers.schema import ensure_supported_schema, validate_payload
from app.config import Settings
from app.errors import AppError, LiveStartRefused, SchemaInvalid

startup_logger = logging.getLogger(__name__)


class LiveLLMClient:
    mode = "LIVE"

    def __init__(
        self,
        settings: Settings,
        ledger: BudgetLedger,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        resolution = resolve_mode(settings)
        if resolution.mode != "LIVE":
            raise LiveStartRefused(resolution.live_block_reason or "LIVE_DISABLED")
        self._provider = provider_for_settings(settings)
        self._provider.validate(settings)
        self._settings = settings
        self._ledger = ledger
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))
        startup_logger.info(
            "live model provider=%s host=%s",
            self._provider.name,
            urlsplit(settings.llm_base_url).hostname,
        )

    def structured_call(self, request: StructuredCallRequest) -> StructuredCallResult:
        if request.max_output_tokens > self._settings.model_max_tokens:
            raise AppError("单次输出 token 超过配置上限", code="TOKEN_LIMIT", http_status=422)
        if request.max_output_tokens < 0 or request.input_tokens < 0:
            raise AppError("token 数不能为负", code="TOKEN_LIMIT", http_status=422)
        schema = request.response_schema or {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
        if self._provider.validate_payload:
            ensure_supported_schema(schema)
        wire = self._provider.build_request(request, schema)
        # One token per UTF-8 byte is a conservative byte-token upper bound,
        # including CJK (unlike characters/4 or bytes/2). Add 256 for framing.
        prompt_bytes = len((wire.prompt_text + json.dumps(schema)).encode("utf-8"))
        estimate = max(request.input_tokens, prompt_bytes + 256)
        key = self._settings.llm_api_key_value()
        reservation_id = self._ledger.reserve(
            provider=self._provider.name,
            model=self._settings.llm_model,
            input_tokens=estimate,
            max_output_tokens=request.max_output_tokens,
            task_id=request.task_id,
            agent_id=request.agent_id,
            max_calls_per_task=self._settings.model_max_calls_per_task,
            at=self._clock(),
        )
        # Unknown billing (including transport failures) keeps the pessimistic estimate.
        input_tokens, output_tokens = estimate, request.max_output_tokens
        try:
            response = self._send(wire.url, wire.body, key)
            parsed = self._provider.parse_response(response)
            if parsed.usage is None:
                if response.is_success:
                    raise SchemaInvalid("模型响应缺少有效用量，按授权上限记账")
                # HTTP rejection without billed usage still consumes a call slot.
                input_tokens, output_tokens = 0, 0
            else:
                input_tokens, output_tokens = parsed.usage
            if not response.is_success:
                self._raise_http_error(response, key)
            if not parsed.complete:
                raise AppError("模型输出未完成", code="MODEL_INCOMPLETE", http_status=422)
            payload = parsed.payload
            # Upstream text is untrusted, even if it somehow echoes a credential.
            if payload is None or (key and key in json.dumps(payload, ensure_ascii=False)):
                raise SchemaInvalid("模型输出不是有效的结构化对象")
            if self._provider.validate_payload:
                validate_payload(payload, schema)
        finally:
            # Exactly one settlement on every path; crashes leave the reservation intact.
            row = self._ledger.settle(
                reservation_id, input_tokens=input_tokens, output_tokens=output_tokens
            )
        return StructuredCallResult(
            payload=payload,
            usage=UsageReport(
                mode="LIVE",
                provider=self._provider.name,
                model=self._settings.llm_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=format(row.cost_usd, "f"),
                price_version=row.price_version,
            ),
        )

    def _send(self, url: str, body: dict[str, object], key: str | None) -> httpx.Response:
        # No automatic retry; caller must re-submit a new task.
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._settings.model_timeout_s,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                return client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}"} if key else {},
                    json=body,
                )
        except httpx.TimeoutException:
            raise AppError(
                "模型请求超时，用量按授权上限记账",
                code="MODEL_TIMEOUT",
                http_status=504,
                retryable=False,
            ) from None
        except httpx.TransportError:
            raise AppError(
                "模型连接失败，用量按授权上限记账",
                code="MODEL_TRANSPORT",
                http_status=502,
                retryable=False,
            ) from None

    @staticmethod
    def _raise_http_error(response: httpx.Response, key: str | None) -> None:
        details: dict[str, object] = {"status_code": response.status_code}
        try:
            error = response.json().get("error", {})
            for field in ("type", "code"):
                value = error.get(field)
                # Do not reflect arbitrary upstream messages, headers or bodies.
                if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,80}", value):
                    details[f"error_{field}"] = "REDACTED" if key and key in value else value
        except (ValueError, AttributeError):
            pass
        status = response.status_code
        code = (
            "MODEL_AUTH"
            if status in (401, 403)
            else "MODEL_RATE_LIMIT"
            if status == 429
            else "MODEL_UPSTREAM"
            if status >= 500
            else "MODEL_HTTP"
        )
        raise AppError(
            "模型提供方拒绝请求",
            code=code,
            http_status=502,
            retryable=False,
            details=details,
        )
