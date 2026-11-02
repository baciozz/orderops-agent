"""Liveness and the current runtime mode. This does not open the database."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from schemas import SCHEMA_VERSION
from schemas.enums import RuntimeMode

from app.config import Settings

router = APIRouter()


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    status: Literal["ok"] = "ok"
    stage: Literal["0"] = "0"
    service: Literal["order-ops-superagent"] = "order-ops-superagent"
    mode: RuntimeMode
    live_requested: bool
    live_block_reason: str | None
    configured_live_budget_usd: str
    effective_live_budget_usd: str


def build_health(request: Request, settings: Settings) -> HealthResponse:
    resolution = request.app.state.mode_resolution
    return HealthResponse(
        request_id=request.state.request_id,
        correlation_id=request.state.correlation_id,
        mode=resolution.mode,
        live_requested=resolution.live_requested,
        live_block_reason=resolution.live_block_reason,
        configured_live_budget_usd=format(settings.live_budget_usd, "f"),
        effective_live_budget_usd=format(settings.effective_live_budget_usd, "f"),
    )


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    return build_health(request, request.app.state.settings)


@router.get("/")
def root() -> dict[str, str]:
    return {
        "service": "order-ops-superagent",
        "stage": "0",
        "health": "/health",
        "task_view_stub": "/tasks/T-STUB/view",
        "task_view_page": "/ui/tasks/T-STUB",
    }
