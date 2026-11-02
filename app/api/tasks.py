"""Task view stub. T-STUB shows the information structure and is not a business result."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from schemas.enums import RuntimeMode
from schemas.task_view import ApprovalBlock, TaskCard, TaskResultBlock, TaskView

from app.errors import AppError

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


def _require_stub(task_id: str) -> None:
    if task_id != STUB_TASK_ID:
        raise AppError(
            "任务不存在。Stage 0 只有信息结构桩 T-STUB，没有真实任务。",
            code="TASK_NOT_FOUND",
            http_status=404,
            details={"task_id": task_id, "stub_id": STUB_TASK_ID},
        )


@router.get("/tasks/{task_id}/view", response_model=TaskView)
def task_view(task_id: str, request: Request) -> TaskView:
    _require_stub(task_id)
    return build_stub_task_view(
        request_id=request.state.request_id,
        correlation_id=request.state.correlation_id,
        mode=request.app.state.mode_resolution.mode,
    )


@router.get("/ui/tasks/{task_id}", response_class=HTMLResponse)
def task_view_page(task_id: str, request: Request) -> HTMLResponse:
    _require_stub(task_id)
    view = build_stub_task_view(
        request_id=request.state.request_id,
        correlation_id=request.state.correlation_id,
        mode=request.app.state.mode_resolution.mode,
    )
    return _TEMPLATES.TemplateResponse(
        request,
        "task_view.html",
        {"view": view.model_dump(mode="json")},
    )
