"""Read-only task views, local exports, and the T-STUB information demo."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from schemas.enums import RuntimeMode
from schemas.task_view import ApprovalBlock, TaskCard, TaskResultBlock, TaskView
from sqlalchemy import inspect

from app.db.models import Task
from app.errors import AppError
from app.orchestration.state import utcnow
from app.services.export import export_change_list
from app.services.task_view import build_task_view

router = APIRouter()
STUB_TASK_ID = "T-STUB"
_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parents[1] / "web" / "templates")
)


def build_stub_task_view(
    *,
    request_id: str,
    correlation_id: str,
    mode: RuntimeMode,
) -> TaskView:
    return TaskView(
        request_id=request_id,
        correlation_id=correlation_id,
        card=TaskCard(
            task_id=STUB_TASK_ID,
            order_id=None,
            status="RECEIVED",
            intent_revision=0,
            waiting_on="none",
            mode=mode,
        ),
        dispatch=[],
        line_diffs=[],
        downstream=[],
        sources=[],
        approval=ApprovalBlock(
            plan_hash=None,
            generated_at=None,
            expires_at=None,
            approvable=False,
            disabled_reason=None,
        ),
        result=TaskResultBlock(
            operation_id=None,
            revision=None,
            verification=None,
            download_url=None,
        ),
    )


def _view(task_id: str, request: Request) -> TaskView:
    engine = request.app.state.engine
    database = engine.url.database
    present = database == ":memory:" or (database and Path(database).is_file())
    if present and inspect(engine).has_table("task"):
        with request.app.state.session_factory() as session:
            if session.get(Task, task_id) is not None:
                return build_task_view(
                    session,
                    task_id,
                    request_id=request.state.request_id,
                    correlation_id=request.state.correlation_id,
                    mode=request.app.state.mode_resolution.mode,
                    now=utcnow(),
                )
    if task_id == STUB_TASK_ID:
        return build_stub_task_view(
            request_id=request.state.request_id,
            correlation_id=request.state.correlation_id,
            mode=request.app.state.mode_resolution.mode,
        )
    raise AppError("任务不存在", code="TASK_NOT_FOUND", http_status=404)


@router.get("/tasks/{task_id}/view", response_model=TaskView)
def task_view(task_id: str, request: Request) -> TaskView:
    return _view(task_id, request)


@router.get("/ui/tasks/{task_id}", response_class=HTMLResponse)
def task_view_page(task_id: str, request: Request) -> HTMLResponse:
    view = _view(task_id, request)
    return _TEMPLATES.TemplateResponse(
        request,
        "task_view.html",
        {"view": view.model_dump(mode="json")},
    )


@router.get("/tasks/{task_id}/download")
def download(task_id: str, request: Request, fmt: str = "csv") -> Response:
    view = _view(task_id, request)
    if not view.result.download_url:
        raise AppError("尚无可下载的变更清单", code="EXPORT_NOT_FOUND", http_status=404)
    rows = [{"mode": view.card.mode.value, **row.model_dump()} for row in view.line_diffs]
    try:
        content = export_change_list(rows, fmt=fmt)
    except ValueError as exc:
        raise AppError(str(exc), code="EXPORT_REJECTED", http_status=400) from exc
    return Response(
        content=content,
        media_type="text/csv"
        if fmt == "csv"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="changes.{fmt}"'},
    )
