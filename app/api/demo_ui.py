"""Local synthetic-data demo. Reuse the inbox, order reader and fixture worker."""

from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from schemas.api import InputSubmitRequest
from schemas.enums import BusinessEventType
from sqlalchemy import inspect, select
from starlette.concurrency import run_in_threadpool

from app.api.inputs import receipt
from app.api.orders import get_order
from app.db.models import Event, SalesOrder, Task
from app.errors import AppError
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE, TEXT_NEW_ORDER, golden_fixtures
from app.orchestration.outbox import publish_pending
from app.orchestration.watchdog import scan
from app.orchestration.worker import DEFAULT_LIMITS, effective_active, run_once
from app.web import templates

router = APIRouter()


def _change_example_orders() -> dict[str, str]:
    """Read trusted shipped declarations, not user text or model output."""
    targets = {}
    for text, payload in golden_fixtures().items():
        event = payload.get("business_event")
        if isinstance(event, dict) and event.get("event_type") == BusinessEventType.CHANGE_ORDER:
            target = event.get("order_id")
            if isinstance(target, str):
                targets[text] = target
    return targets


_CHANGE_EXAMPLE_ORDERS = _change_example_orders()


def _has_table(request: Request, name: str) -> bool:
    engine = request.app.state.engine
    database = engine.url.database
    present = database in (None, "", ":memory:") or Path(database).is_file()
    return present and inspect(engine).has_table(name)


def _submit_page(request: Request, values: dict[str, str], error=None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "demo_submit.html",
        {
            "values": values,
            "error": error,
            "example_orders": sorted(set(_CHANGE_EXAMPLE_ORDERS.values())),
        },
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
        target = _CHANGE_EXAMPLE_ORDERS.get(body.text) if body.text is not None else None
        if target is not None and body.selected_order_id != target:
            # Every known mismatch stops before receipt, including reused keys.
            # Preserve the ordinary missing-order response with a read-only lookup.
            with request.app.state.session_factory() as session:
                if session.get(SalesOrder, body.selected_order_id) is None:
                    raise AppError("订单不存在", code="NOT_FOUND", http_status=404)
            raise AppError(
                "此精确示例的固定订单与所选订单不一致，未提交。请核对订单和示例。",
                code="DEMO_EXAMPLE_ORDER_MISMATCH",
            )
        result = await run_in_threadpool(receipt, body, request, fixture_only=True)
    except AppError as exc:
        return _submit_page(request, values, error=exc)
    return RedirectResponse(f"/ui/tasks/{result.task_id}", status_code=303)


def _new_stock_page(request: Request, key: str, error=None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "demo_new_stock.html",
        {
            "example": TEXT_NEW_ORDER,
            "client_event_key": key,
            "error": error,
            "enabled": not request.app.state.settings.live_enabled,
        },
        status_code=error.http_status if error else 200,
    )


@router.get("/ui/demo/new-stock", response_class=HTMLResponse)
def new_stock_page(request: Request):
    return _new_stock_page(request, str(uuid4()))


@router.post("/ui/demo/new-stock", response_class=HTMLResponse)
async def new_stock_submit(request: Request):
    """Exact first-input fixture only; shared answers retain their normal contract."""
    key = ""
    try:
        if request.app.state.settings.live_enabled:
            raise AppError("请关闭 LIVE 后使用此 FIXTURE 入口", code="FIXTURE_REQUIRED")
        if request.headers.get("content-type", "").split(";", 1)[0] != (
            "application/x-www-form-urlencoded"
        ):
            raise AppError("请使用网页表单提交", code="VALIDATION_ERROR")
        try:
            fields = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError as exc:
            raise AppError("表单编码无效", code="VALIDATION_ERROR") from exc
        key = fields.get("client_event_key", [""])[0]
        # Reject unknown fields (including mode/order/source overrides) and duplicate
        # parameters before the durable inbox. Never silently pick the first value.
        if set(fields) != {"text", "client_event_key"} or any(
            len(values) != 1 for values in fields.values()
        ):
            raise AppError("仅允许新单精确示例，不接受订单号或模式参数", code="VALIDATION_ERROR")
        if fields["text"][0] != TEXT_NEW_ORDER:
            raise AppError(
                "仅支持页面上的精确新单示例，不能修改文案或数量", code="VALIDATION_ERROR"
            )
        try:
            if str(UUID(key)) != key:
                raise ValueError("noncanonical key")
        except ValueError as exc:
            raise AppError("提交标识无效，请重新打开示例页面", code="VALIDATION_ERROR") from exc
        if not _has_table(request, "task"):
            raise AppError("请先运行 make init-db && make seed", code="DEMO_NOT_READY")
        body = InputSubmitRequest(
            thread_id="demo-new-stock",
            client_event_key=key,
            text=TEXT_NEW_ORDER,
            selected_order_id=None,
        )
        result = await run_in_threadpool(receipt, body, request, fixture_only=True)
    except AppError as exc:
        return _new_stock_page(request, key, error=exc)
    return RedirectResponse(f"/ui/tasks/{result.task_id}", status_code=303)


_SHEET_COMMAND = "新建现货订单（表格）"


def _new_stock_sheet_page(request: Request, key: str, error=None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "demo_new_stock_sheet.html",
        {"command": _SHEET_COMMAND, "client_event_key": key, "error": error},
        status_code=error.http_status if error else 200,
    )


@router.get("/ui/demo/new-stock-sheet", response_class=HTMLResponse)
def new_stock_sheet_page(request: Request):
    return _new_stock_sheet_page(request, str(uuid4()))


@router.post("/ui/demo/new-stock-sheet", response_class=HTMLResponse)
async def new_stock_sheet_submit(request: Request):
    """Activate the bounded application command through the ordinary inbox only."""
    key = ""
    try:
        if request.headers.get("content-type", "").split(";", 1)[0] != (
            "application/x-www-form-urlencoded"
        ):
            raise AppError("请使用网页表单提交", code="VALIDATION_ERROR")
        try:
            fields = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError as exc:
            raise AppError("表单编码无效", code="VALIDATION_ERROR") from exc
        key = fields.get("client_event_key", [""])[0]
        if set(fields) != {"text", "client_event_key"} or any(
            len(values) != 1 for values in fields.values()
        ):
            raise AppError("不接受额外字段、订单号、模式或来源参数", code="VALIDATION_ERROR")
        if fields["text"][0] != _SHEET_COMMAND:
            raise AppError("请使用页面上的固定表格入口指令", code="VALIDATION_ERROR")
        try:
            if str(UUID(key)) != key:
                raise ValueError("noncanonical key")
        except ValueError as exc:
            raise AppError("提交标识无效，请重新打开页面", code="VALIDATION_ERROR") from exc
        if not _has_table(request, "task"):
            raise AppError("请先运行 make init-db && make seed", code="DEMO_NOT_READY")
        body = InputSubmitRequest(
            thread_id="demo-new-stock-sheet",
            client_event_key=key,
            text=_SHEET_COMMAND,
            selected_order_id=None,
            source_ids=[],
        )
        result = await run_in_threadpool(receipt, body, request, fixture_only=True)
    except AppError as exc:
        return _new_stock_sheet_page(request, key, error=exc)
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
    recovered = scan(factory, settings=settings)
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
