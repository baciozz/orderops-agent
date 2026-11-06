"""Read-only task views, local exports, and the T-STUB information demo."""

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from schemas.enums import RuntimeMode, TaskStatus, WaitingOn
from schemas.task_view import ApprovalBlock, TaskCard, TaskResultBlock, TaskView
from sqlalchemy import inspect, select

from app.api.errors import json_error
from app.db.models import (
    Approval,
    Event,
    InputPayload,
    InputSource,
    Operation,
    Plan,
    PurchaseProposal,
    Task,
    TaskSource,
)
from app.errors import AppError
from app.orchestration.inbox import latest_input
from app.orchestration.state import utcnow
from app.services.export import export_change_list
from app.services.task_audit import export_task_audit
from app.services.task_view import build_task_view
from app.web import templates

router = APIRouter()
STUB_TASK_ID = "T-STUB"
_MODEL_FAILURE_MESSAGES = {
    "MODEL_TIMEOUT": "模型请求超时，未生成可供批准的计划。",
    "MODEL_TRANSPORT": "连接模型提供方失败，未生成可供批准的计划。",
    "MODEL_UPSTREAM": "模型提供方服务出错，未生成可供批准的计划。",
    "SCHEMA_INVALID": "模型返回格式不符合要求，已达到本次允许的格式检查次数。",
}


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


def _task_table_present(request: Request) -> bool:
    engine = request.app.state.engine
    database = engine.url.database
    present = database == ":memory:" or (database and Path(database).is_file())
    return bool(present and inspect(engine).has_table("task"))


def _view(task_id: str, request: Request) -> TaskView:
    if _task_table_present(request):
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
    # Capture the input fence before projecting the view: a concurrent answer
    # may make this page stale, but must never pair an old view with a new fence.
    input_event_id = None
    if task_id != STUB_TASK_ID and _task_table_present(request):
        with request.app.state.session_factory() as session:
            task = session.get(Task, task_id)
            payload = latest_input(session, task) if task else None
            input_event_id = payload.event_id if payload else None
    view = _view(task_id, request)
    plan_id = approval_id = None
    renewable = False
    duplicate_warning = None
    uploaded_sources = []
    failure_guidance = None
    sheet_workflow = None
    if task_id != STUB_TASK_ID:
        with request.app.state.session_factory() as session:
            task = session.get(Task, task_id)
            sheet_workflow = _sheet_presentation(session, task)
            if task and task.status == "FAILED" and view.card.status == TaskStatus.FAILED:
                # result_ref may contain private input or an exception. Never render it.
                # Check all task plans too: a failure may have overwritten the reference.
                operation = session.scalar(
                    select(Operation.id)
                    .outerjoin(Plan, Operation.plan_id == Plan.id)
                    .where((Plan.task_id == task_id) | (Operation.id == task.result_ref))
                    .limit(1)
                )
                failure_guidance = {
                    "fixture_not_found": task.error_code == "FIXTURE_NOT_FOUND",
                    "intent_order": task.error_code
                    in {"STALE_INPUT_ORDER", "INTENT_ORDER_UNPROVEN"},
                    "model_error_code": task.error_code
                    if task.runtime_mode == "LIVE" and task.error_code in _MODEL_FAILURE_MESSAGES
                    else None,
                    "model_message": _MODEL_FAILURE_MESSAGES.get(task.error_code or "", ""),
                    "has_operation": operation is not None,
                }
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
                duplicate_of = (plan.normalized_changes or {}).get("suspected_duplicate_of")
                if plan.plan_kind == "create" and isinstance(duplicate_of, str) and duplicate_of:
                    duplicate_warning = {
                        "order_id": duplicate_of,
                        "url": f"/ui/demo/orders/{quote(duplicate_of, safe='')}",
                    }
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
            "duplicate_warning": duplicate_warning,
            "failure_guidance": failure_guidance,
            "sheet_workflow": sheet_workflow,
            "uploaded_sources": uploaded_sources,
            "input_event_id": input_event_id,
            "audit_url": f"/tasks/{quote(task_id, safe='')}/audit"
            if task_id != STUB_TASK_ID
            else None,
            "can_answer": input_event_id is not None
            and view.card.status.value in {"NEEDS_CLARIFICATION", "AWAITING_APPROVAL"},
            "can_cancel": task_id != STUB_TASK_ID
            and view.card.status.value
            in {"RECEIVED", "QUEUED", "RUNNING", "NEEDS_CLARIFICATION", "AWAITING_APPROVAL"},
        },
    )


def _sheet_presentation(session, task: Task | None):
    """Read linked history only; no task-only proposal is promoted to current."""
    if task is None:
        return None
    origins = list(
        session.execute(
            select(InputPayload, Event)
            .join(Event, Event.id == InputPayload.event_id)
            .where(InputPayload.task_id == task.id, Event.kind == "input.submit")
            .limit(2)
        )
    )
    if len(origins) != 1:
        return None
    receipt, event = origins[0]
    if (
        event.order_id is not None
        or receipt.body.get("text") != "新建现货订单（表格）"
        or receipt.body.get("source_ids") != []
        or type(receipt.body.get("input_sequence")) is not int
        or receipt.body["input_sequence"] != 1
    ):
        return None
    latest = latest_input(session, task)
    history = []
    for plan in session.scalars(select(Plan).where(Plan.task_id == task.id).limit(10)):
        record = (plan.normalized_changes or {}).get("sheet_order_v1")
        if not isinstance(record, dict):
            continue
        shortage = record.get("shortage")
        if not isinstance(shortage, dict) or shortage.get("plan_id") != plan.id:
            continue
        input_id = shortage.get("input_event_id")
        if not isinstance(input_id, str):
            continue
        owner = session.get(InputPayload, input_id)
        origin = session.get(Event, input_id)
        if (
            owner is None
            or owner.task_id != task.id
            or origin is None
            or origin.kind != "clarify_answer"
            or owner.body.get("sheet_order_v1") != record
            or type(shortage.get("intent_revision")) is not int
            or shortage["intent_revision"] != plan.intent_revision
        ):
            continue
        facts = shortage.get("facts")
        if not isinstance(facts, list) or len(facts) > 15000:
            continue
        current = (
            latest is not None
            and latest.event_id == input_id
            and task.intent_revision == shortage["intent_revision"]
        )
        seen = set()
        for fact in facts:
            if not isinstance(fact, dict) or not isinstance(fact.get("proposal_id"), str):
                continue
            proposal_id = fact["proposal_id"]
            if proposal_id in seen:
                continue
            seen.add(proposal_id)
            proposal = session.get(PurchaseProposal, proposal_id)
            if (
                proposal is None
                or proposal.task_id != task.id
                or proposal.sku_id != fact.get("sku_id")
                or type(fact.get("shortage_qty")) is not int
                or proposal.shortage_qty != fact["shortage_qty"]
            ):
                continue
            history.append(
                {
                    "plan_id": plan.id,
                    "input_event_id": input_id,
                    "proposal_id": proposal.id,
                    "sku_id": proposal.sku_id,
                    "qty": proposal.shortage_qty,
                    "status": proposal.status,
                    "current": current,
                }
            )
    correction_view = None
    record = latest.body.get("sheet_order_v1") if latest else None
    correction = record.get("correction") if isinstance(record, dict) else None
    if isinstance(correction, dict) and latest is not None:
        if (
            correction.get("event_id") == latest.event_id
            and type(correction.get("input_sequence")) is int
            and correction["input_sequence"] == latest.body.get("input_sequence")
            and correction.get("text") == latest.body.get("text")
            and type(correction.get("old_qty")) is int
            and type(correction.get("new_qty")) is int
            and isinstance(correction.get("source_id"), str)
            and session.get(TaskSource, (task.id, correction["source_id"])) is not None
            and type(correction.get("row_number")) is int
        ):
            correction_view = correction
    return {
        "history": history,
        "correction": correction_view,
        "exhausted": task.error_code == "ATTEMPT_LIMIT",
        "answer_placeholder": "蓝 L 改成 20"
        if history or (isinstance(record, dict) and record.get("rows"))
        else "按附件新建订单",
    }


@router.get("/tasks/{task_id}/audit")
def task_audit(task_id: str, request: Request) -> Response:
    try:
        response = Response(
            content=export_task_audit(request.app.state.engine, task_id),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="task-audit-v1.json"'},
        )
    except AppError as exc:
        request.state.error_code = exc.code
        response = json_error(exc, request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


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
