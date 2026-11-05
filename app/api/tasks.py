"""Read-only task views, local exports, and the T-STUB information demo."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from schemas.enums import RuntimeMode, TaskStatus, WaitingOn
from schemas.task_view import ApprovalBlock, TaskCard, TaskResultBlock, TaskView
from sqlalchemy import inspect, select

from app.db.models import Approval, Event, InputSource, Plan, Task, TaskSource
from app.errors import AppError
from app.orchestration.state import utcnow
from app.services.export import export_change_list
from app.services.task_view import build_task_view
from app.web import templates

router = APIRouter()
STUB_TASK_ID = "T-STUB"


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
            status=TaskStatus.RECEIVED,
            intent_revision=0,
            waiting_on=WaitingOn.NONE,
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
    plan_id = approval_id = None
    renewable = False
    uploaded_sources = []
    if task_id != STUB_TASK_ID:
        with request.app.state.session_factory() as session:
            uploaded_sources = [
                {"id": source.id, "name": source.original_name, "size": source.size}
                for source in session.scalars(
                    select(InputSource)
                    .join(TaskSource, TaskSource.source_id == InputSource.id)
                    .where(TaskSource.task_id == task_id)
                    .order_by(InputSource.id)
                )
            ]
    if view.approval.plan_hash:
        with request.app.state.session_factory() as session:
            plan = session.scalar(
                select(Plan)
                .join(Event, Event.payload_ref == Plan.id)
                .where(Plan.task_id == task_id, Plan.plan_hash == view.approval.plan_hash)
                .order_by(Event.received_at.desc(), Event.id.desc())
            )
            if plan:
                plan_id = plan.id
                # EXPIRED is projected only after the ordinary task and
                # dependency checks. A plan expiry itself cannot be renewed.
                renewable = (
                    plan.status in {"VALIDATED", "APPROVED"}
                    and view.approval.disabled_reason == "EXPIRED"
                )
                approval = session.scalar(
                    select(Approval)
                    .where(Approval.plan_id == plan.id)
                    .order_by(Approval.expires_at.desc(), Approval.id.desc())
                )
                if approval and approval.status == "ACTIVE" and view.approval.approvable:
                    approval_id = approval.id
    return templates.TemplateResponse(
        request,
        "task_view.html",
        {
            "view": view.model_dump(mode="json"),
            "plan_id": plan_id,
            "approval_id": approval_id,
            "renewable": renewable,
            "uploaded_sources": uploaded_sources,
            "can_answer": view.card.status.value in {"NEEDS_CLARIFICATION", "AWAITING_APPROVAL"},
            "can_cancel": task_id != STUB_TASK_ID
            and view.card.status.value
            in {"RECEIVED", "QUEUED", "RUNNING", "NEEDS_CLARIFICATION", "AWAITING_APPROVAL"},
        },
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
