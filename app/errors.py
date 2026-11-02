"""Stable application errors. Messages are Chinese. Codes are stable tokens."""

from typing import Any


class AppError(Exception):
    """Base error mapped to the API error envelope."""

    code = "ERROR"
    http_status = 400
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        if retryable is not None:
            self.retryable = retryable
        self.details = details or {}


class RegistryError(AppError):
    code = "REGISTRY_ERROR"
    http_status = 500


class ToolNotAllowed(AppError):
    code = "TOOL_NOT_ALLOWED"
    http_status = 403


class BudgetExceeded(AppError):
    code = "BUDGET_EXCEEDED"
    http_status = 429
    retryable = False

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message or "已达到 live 模型预算上限，拒绝本次调用",
            code="BUDGET_EXCEEDED",
            http_status=429,
            retryable=False,
            details=details,
        )


class CallLimitExceeded(AppError):
    code = "CALL_LIMIT"
    http_status = 429

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="CALL_LIMIT", http_status=429, details=details)


class PricesNotConfigured(AppError):
    code = "PRICES_NOT_CONFIGURED"
    http_status = 503

    def __init__(self) -> None:
        super().__init__(
            "未配置 OOS_PRICE_IN_PER_MTOK / OOS_PRICE_OUT_PER_MTOK，拒绝 live，不猜测单价",
            code="PRICES_NOT_CONFIGURED",
            http_status=503,
        )


class LiveStartRefused(AppError):
    code = "LIVE_START_REFUSED"
    http_status = 503

    def __init__(self, reason: str) -> None:
        known = {"PRICES_NOT_CONFIGURED", "KEY_NOT_CONFIGURED", "LIVE_DISABLED"}
        super().__init__(
            f"live 模式拒绝启动（{reason}），保持 FIXTURE",
            code=reason if reason in known else "LIVE_START_REFUSED",
            http_status=503,
            details={"reason": reason},
        )
        self.reason = reason


class LiveTransportNotReady(AppError):
    code = "LIVE_TRANSPORT_NOT_READY"
    http_status = 503

    def __init__(self) -> None:
        super().__init__(
            "TODO(stage-4): live 传输未实现，本次不会访问网络",
            code="LIVE_TRANSPORT_NOT_READY",
            http_status=503,
        )


class FixtureNotFound(AppError):
    code = "FIXTURE_NOT_FOUND"
    http_status = 422


class SeedIntegrityError(AppError):
    code = "SEED_INTEGRITY"
    http_status = 500

    def __init__(self, violations: list[str]) -> None:
        super().__init__(
            "合成 seed 与黄金数字不一致",
            code="SEED_INTEGRITY",
            http_status=500,
            details={"violations": violations},
        )
        self.violations = violations


class InventoryInvariantError(AppError):
    code = "INVENTORY_INVARIANT"
    http_status = 422


class ProposalRejected(AppError):
    code = "PROPOSAL_REJECTED"
    http_status = 409


class SchemaInvalid(AppError):
    code = "SCHEMA_INVALID"
    http_status = 422

    def __init__(self, message: str) -> None:
        super().__init__(message, code="SCHEMA_INVALID", http_status=422)
