"""FastAPI application factory.

The process binds to loopback when started via Make. Stage 0 does not open a
database connection and does not call a model during import or health checks.
"""

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from schemas import SCHEMA_VERSION

from app import __version__
from app.adapters.llm import resolve_mode
from app.api.errors import json_error
from app.api.health import router as health_router
from app.api.tasks import router as task_router
from app.config import Settings, get_settings
from app.errors import AppError
from app.orchestration.defaults import build_default_registry


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    registry = build_default_registry()
    resolution = resolve_mode(settings)
    application = FastAPI(
        title="order-ops-superagent",
        version=__version__,
        description="Stage 0 skeleton. Fixture mode by default. Not a production ERP.",
    )
    application.state.settings = settings
    application.state.registry = registry
    application.state.mode_resolution = resolution

    @application.middleware("http")
    async def attach_ids(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id") or str(uuid4())
        correlation_id = request.headers.get("x-correlation-id") or request_id
        request.state.request_id = request_id
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        response.headers["x-correlation-id"] = correlation_id
        return response

    @application.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return json_error(exc, request)

    @application.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        correlation_id = getattr(request.state, "correlation_id", request_id)
        return JSONResponse(
            status_code=400,
            content={
                "request_id": request_id,
                "correlation_id": correlation_id,
                "schema_version": SCHEMA_VERSION,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求格式不正确",
                    "retryable": False,
                    "details": {"errors": jsonable_encoder(exc.errors())},
                },
            },
        )

    application.include_router(health_router)
    application.include_router(task_router)
    return application


app = create_app()
