"""FastAPI application factory.

The process binds to loopback when started via Make. Stage 0 does not open a
database connection and does not call a model during import or health checks.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from schemas import SCHEMA_VERSION
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.adapters.budget import reconcile_reservations
from app.adapters.llm import resolve_mode
from app.adapters.uploads import MAX_REQUEST_BYTES
from app.api.demo_ui import router as demo_ui_router
from app.api.dispatch import router as dispatch_router
from app.api.errors import json_error
from app.api.health import router as health_router
from app.api.inputs import router as inputs_router
from app.api.orders import router as orders_router
from app.api.sources import router as sources_router
from app.api.tasks import router as task_router
from app.config import Settings, get_settings
from app.db.schema import schema_ready
from app.db.session import make_engine, make_session_factory
from app.errors import AppError
from app.observability import configure_logging, exception_code, get_logger, log_context, log_event
from app.orchestration.defaults import build_default_registry

logger = get_logger(__name__)

_HTTP_ERROR_CODES = {
    400: "VALIDATION_ERROR",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    422: "VALIDATION_ERROR",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    registry = build_default_registry()
    resolution = resolve_mode(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # init-db/migrations remain an explicit operator action. A fresh health
        # server must not create a database just to reconcile an empty ledger.
        engine = app.state.engine
        database = engine.url.database
        present = database in (None, "", ":memory:") or Path(database).is_file()
        try:
            if present and schema_ready(engine):
                reconcile_reservations(
                    app.state.session_factory,
                    now=datetime.now(UTC),
                    ttl=timedelta(seconds=settings.effective_budget_reservation_ttl_s),
                )
            yield
        finally:
            engine.dispose()

    application = FastAPI(
        title="order-ops-superagent",
        version=__version__,
        description="Stage 1C sub-agent dispatch. Fixture mode by default. Not a production ERP.",
        lifespan=lifespan,
    )
    engine = make_engine(settings.database_url)
    application.state.engine = engine
    application.state.session_factory = make_session_factory(engine)
    application.state.settings = settings
    application.state.source_storage_dir = None
    application.state.registry = registry
    application.state.mode_resolution = resolution

    @application.middleware("http")
    async def attach_ids(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid4())
        correlation_id = request.headers.get("x-correlation-id") or request_id
        request.state.request_id = request_id
        request.state.correlation_id = correlation_id
        started = perf_counter()
        status_code = 500
        with log_context(request_id=request_id, correlation_id=correlation_id):
            try:
                # Bound actual streamed bytes, including requests without Content-Length.
                # Starlette's cached Request replays _body to the downstream route.
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                        status_code = 413
                        request.state.error_code = "UPLOAD_TOO_LARGE"
                        response = json_error(
                            AppError(
                                "请求体超过大小上限", code="UPLOAD_TOO_LARGE", http_status=413
                            ),
                            request,
                        )
                        response.headers["x-request-id"] = request_id
                        response.headers["x-correlation-id"] = correlation_id
                        return response
                    body.extend(chunk)
                request._body = bytes(body)
                response = await call_next(request)
                status_code = response.status_code
                response.headers["x-request-id"] = request_id
                response.headers["x-correlation-id"] = correlation_id
                return response
            except Exception as exc:
                request.state.error_code = exception_code(exc)
                raise
            finally:
                log_event(
                    logger,
                    "http.request",
                    app_mode=resolution.mode.value,
                    method=request.method,
                    path=request.url.path,
                    status_code=status_code,
                    outcome="success" if status_code < 400 else "failed",
                    error_code=getattr(request.state, "error_code", None),
                    duration_ms=round((perf_counter() - started) * 1000, 3),
                )

    @application.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        request.state.error_code = exc.code
        return json_error(exc, request)

    @application.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        request.state.error_code = _HTTP_ERROR_CODES.get(exc.status_code, "HTTP_ERROR")
        return json_error(
            AppError(
                str(exc.detail),
                code=request.state.error_code,
                http_status=exc.status_code,
            ),
            request,
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        request.state.error_code = "VALIDATION_ERROR"
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
    application.include_router(orders_router)
    application.include_router(dispatch_router)
    application.include_router(inputs_router)
    application.include_router(sources_router)
    application.include_router(demo_ui_router)
    application.mount(
        "/ui/static",
        StaticFiles(directory=str(Path(__file__).parent / "web" / "static")),
        name="ui-static",
    )
    return application


app = create_app()
