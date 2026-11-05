"""Local synthetic-data demo. Reuse the inbox, order reader and fixture worker."""

from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from schemas.api import InputSubmitRequest
from sqlalchemy import inspect, select
from starlette.concurrency import run_in_threadpool

from app.api.inputs import receipt
from app.api.orders import get_order
from app.db.models import Event, Task
from app.errors import AppError
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from app.orchestration.outbox import publish_pending
from app.orchestration.watchdog import scan
from app.orchestration.worker import DEFAULT_LIMITS, effective_active, run_once
from app.web import templates

router = APIRouter()


def _has_table(request: Request, name: str) -> bool:
    engine = request.app.state.engine
    database = engine.url.database
    present = database in (None, "", ":memory:") or Path(database).is_file()
    return present and inspect(engine).has_table(name)


def _submit_page(request: Request, values: dict[str, str], error=None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "demo_submit.html",
        {"values": values, "error": error},
        status_code=error.http_status if error else 200,
    )


@router.get("/ui", response_class=HTMLResponse)
@router.get("/ui/demo", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "demo_home.html", {"example": TEXT_BLACK_TO_WHITE})


@router.get("/ui/demo/submit", response_class=HTMLResponse)
def submit_page(request: Request):
    return _submit_page(
        request,
        {
            "selected_order_id": "SO-1001",
            "text": TEXT_BLACK_TO_WHITE,
            "client_event_key": f"demo-ui-{uuid4()}",
            "thread_id": "demo-ui",
        },
    )


@router.post("/ui/demo/submit", response_class=HTMLResponse)
async def submit(request: Request):
    # Native HTML forms use URL encoding; no multipart dependency or JS is needed.
    if request.headers.get("content-type", "").split(";", 1)[0] != (
        "application/x-www-form-urlencoded"
    ):
        raise AppError("请使用网页表单提交", code="VALIDATION_ERROR", http_status=400)
    fields = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    values = {
        name: fields.get(name, [""])[0]
        for name in ("selected_order_id", "text", "client_event_key", "thread_id")
    }
    values["client_event_key"] = values["client_event_key"].strip() or f"demo-ui-{uuid4()}"
    values["thread_id"] = values["thread_id"].strip() or "demo-ui"
    values["selected_order_id"] = values["selected_order_id"].strip()
    try:
        if not values["selected_order_id"]:
            raise AppError("请选择订单号", code="VALIDATION_ERROR")
        if not _has_table(request, "task"):
            raise AppError("请先运行 make init-db && make seed", code="DEMO_NOT_READY")
        body = InputSubmitRequest.model_validate(values)
        result = await run_in_threadpool(receipt, body, request)
    except AppError as exc:
        return _submit_page(request, values, error=exc)
    return RedirectResponse(f"/ui/tasks/{result.task_id}", status_code=303)


@router.get("/ui/demo/tasks", response_class=HTMLResponse)
def recent_tasks(request: Request):
    tasks = []
    if _has_table(request, "task"):
        with request.app.state.session_factory() as session:
            tasks = list(
                session.scalars(
                    select(Task)
                    .outerjoin(Event, Event.id == Task.last_event_id)
                    .order_by(Event.received_at.desc(), Task.id.desc())
                    .limit(50)
                )
            )
    return templates.TemplateResponse(request, "demo_tasks.html", {"tasks": tasks})


@router.get("/ui/demo/orders/{order_id}", response_class=HTMLResponse)
def order_page(order_id: str, request: Request):
    if not _has_table(request, "sales_order"):
        raise AppError("请先运行 make init-db && make seed", code="DEMO_NOT_READY")
    order = get_order(order_id, request)
    return templates.TemplateResponse(
        request, "demo_order.html", {"order": order.model_dump(mode="json")}
    )


@router.post("/ui/demo/worker-once")
def worker_once(request: Request):
    if not _has_table(request, "task"):
        raise AppError("请先运行 make init-db && make seed", code="DEMO_NOT_READY")
    factory = request.app.state.session_factory
    settings = request.app.state.settings
    limits = replace(DEFAULT_LIMITS, active=effective_active(settings.worker_max_active))
    recovered = scan(factory)
    # Never consult global live configuration from this local demo endpoint.
    outcome = run_once(
        factory, limits=limits, settings=settings.model_copy(update={"live_enabled": False})
    )
    delivered = publish_pending(factory)
    return {
        "mode": "FIXTURE",
        "recovered_task_ids": recovered,
        "result": asdict(outcome) if outcome else None,
        "published": delivered,
    }
