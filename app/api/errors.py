"""Map AppError onto the stable error envelope."""

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from schemas import SCHEMA_VERSION

from app.errors import AppError


def error_payload(
    exc: AppError,
    *,
    request_id: str,
    correlation_id: str,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "correlation_id": correlation_id,
        "schema_version": SCHEMA_VERSION,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "details": exc.details,
        },
    }


def json_error(exc: AppError, request: Request) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    correlation_id = getattr(request.state, "correlation_id", request_id)
    return JSONResponse(
        status_code=exc.http_status,
        content=error_payload(exc, request_id=request_id, correlation_id=correlation_id),
    )
