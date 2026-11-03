"""Guarded synchronous OpenAI Responses transport. No calls occur at construction."""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from schemas.common import UsageReport

from app.adapters.budget import BudgetLedger
from app.adapters.llm import StructuredCallRequest, StructuredCallResult, resolve_mode
from app.config import Settings
from app.errors import AppError, LiveStartRefused, SchemaInvalid


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
        if settings.llm_provider != "openai":
            raise LiveStartRefused("PROVIDER_UNSUPPORTED")
        if not settings.llm_model:
            raise LiveStartRefused("MODEL_NOT_CONFIGURED")
        # Keep the credential on the official origin, including when configured.
        if settings.llm_base_url.rstrip("/") != "https://api.openai.com/v1":
            raise LiveStartRefused("BASE_URL_UNSUPPORTED")
        self._settings = settings
        self._ledger = ledger
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

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
        # One token per UTF-8 byte is a conservative byte-token upper bound,
        # including CJK (unlike characters/4 or bytes/2). Add 256 for framing.
        prompt_bytes = len(
            (request.system_prompt + request.user_prompt + json.dumps(schema)).encode("utf-8")
        )
        estimate = max(request.input_tokens, prompt_bytes + 256)
        self._ledger.authorize(
            input_tokens=estimate,
            max_output_tokens=request.max_output_tokens,
            task_id=request.task_id,
            max_calls_per_task=self._settings.model_max_calls_per_task,
        )
        key = self._settings.llm_api_key_value()
        body = {
            "model": self._settings.llm_model,
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
        }
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._settings.model_timeout_s,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    self._settings.llm_base_url.rstrip("/") + "/responses",
                    headers={"Authorization": f"Bearer {key}"},
                    json=body,
                )
        except httpx.TimeoutException:
            self._record(request, estimate, request.max_output_tokens)
            raise AppError(
                "模型请求超时，用量按授权上限记账",
                code="MODEL_TIMEOUT",
                http_status=504,
                retryable=True,
            ) from None
        except httpx.TransportError:
            self._record(request, estimate, request.max_output_tokens)
            raise AppError(
                "模型连接失败，用量按授权上限记账",
                code="MODEL_TRANSPORT",
                http_status=502,
                retryable=True,
            ) from None
        if not response.is_success:
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
                retryable=status == 429 or status >= 500,
                details=details,
            )
        try:
            data = response.json()
            usage = data["usage"]
            input_tokens, output_tokens = usage["input_tokens"], usage["output_tokens"]
            if any(type(n) is not int or n < 0 for n in (input_tokens, output_tokens)):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            self._record(request, estimate, request.max_output_tokens)
            raise SchemaInvalid("模型响应缺少有效用量，按授权上限记账") from None
        row = self._record(request, input_tokens, output_tokens)
        if data.get("status") != "completed":
            raise AppError("模型输出未完成", code="MODEL_INCOMPLETE", http_status=422)
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
                raise ValueError
            # Upstream text is untrusted, even if it somehow echoes a credential.
            if key and key in json.dumps(payload, ensure_ascii=False):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise SchemaInvalid("模型输出不是有效的结构化对象") from None
        return StructuredCallResult(
            payload=payload,
            usage=UsageReport(
                mode="LIVE",
                provider="openai",
                model=self._settings.llm_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=format(row.cost_usd, "f"),
                price_version=row.price_version,
            ),
        )

    def _record(self, request: StructuredCallRequest, input_tokens: int, output_tokens: int):
        return self._ledger.record(
            provider="openai",
            model=self._settings.llm_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            task_id=request.task_id,
            agent_id=request.agent_id,
            at=self._clock(),
        )
