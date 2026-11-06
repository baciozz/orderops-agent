"""Durable inbox, polling, cancellation and human answer events."""

from fastapi import APIRouter, Request
from schemas.api import (
    AnswerSubmitRequest,
    InputSubmitRequest,
    InputSubmitResponse,
    TaskCancelRequest,
)
from sqlalchemy import select

from app.adapters.llm import resolve_mode
from app.db.models import JobAttempt, Plan, Task
from app.db.session import begin_immediate
from app.errors import AppError
from app.orchestration.inbox import accept_input, latest_input
from app.orchestration.state import transition

router = APIRouter()


def receipt(body, request, task_id=None, *, fixture_only=False):
    event_id, task_id, status = accept_input(
        request.app.state.session_factory,
        **body.model_dump(exclude={"schema_version"}),
        task_id=task_id,
        runtime_mode="FIXTURE" if fixture_only else resolve_mode(request.app.state.settings).mode,
    )
    with request.app.state.session_factory() as session:
        chain_id = session.get(Task, task_id).chain_id
    return InputSubmitResponse(
        request_id=request.state.request_id,
        correlation_id=request.state.correlation_id,
        event_id=event_id,
        task_id=task_id,
        chain_id=chain_id,
        status=status,
    )


@router.post("/inputs", status_code=202, response_model=InputSubmitResponse)
def submit(body: InputSubmitRequest, request: Request):
    return receipt(body, request)


@router.post("/tasks/{task_id}/answers", status_code=202, response_model=InputSubmitResponse)
def answer(task_id: str, body: AnswerSubmitRequest, request: Request):
    return receipt(body, request, task_id)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request):
    with request.app.state.session_factory() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise AppError("任务不存在", code="NOT_FOUND", http_status=404)
        plans = list(session.scalars(select(Plan).where(Plan.task_id == task_id)))
        payload = latest_input(session, task)
        questions = list(payload.body.get("questions", [])) if payload else []
        from app.services.sheet_order import is_sheet_task

        if not is_sheet_task(session, task):
            for plan in plans:
                questions += plan.normalized_changes.get("outcome", {}).get("questions", [])
        return {
            "questions": questions,
            "extraction": payload.body.get("extraction") if payload else None,
            "task_id": task.id,
            "status": task.status,
            "runtime_mode": task.runtime_mode,
            "intent_revision": task.intent_revision,
            "result_ref": task.result_ref,
            "error_code": task.error_code,
            "plans": [
                {"plan_id": p.id, "source_refs": p.source_refs, "changes": p.normalized_changes}
                for p in plans
            ],
        }


def cancel_task(factory, task_id, expected_intent_revision):
    with factory() as session, begin_immediate(session):
        task = session.get(Task, task_id)
        if task is None:
            raise AppError("任务不存在", code="NOT_FOUND", http_status=404)
        if task.status in {"SUCCEEDED", "RECONCILING", "COMMITTING"}:
            return "ALREADY_COMMITTED"
        if task.intent_revision != expected_intent_revision:
            raise AppError("意图已更新", code="STALE_INTENT", http_status=409)
        if task.status != "CANCELLED":
            transition(task, "CANCELLED")
        task.cancel_requested = True
        for attempt in session.scalars(select(JobAttempt).where(JobAttempt.task_id == task_id)):
            attempt.lease_until = None
            attempt.lease_owner = None
        return task.status


@router.post("/tasks/{task_id}/cancel")
def cancel(task_id: str, body: TaskCancelRequest, request: Request):
    return {
        "task_id": task_id,
        "status": cancel_task(
            request.app.state.session_factory, task_id, body.expected_intent_revision
        ),
    }
