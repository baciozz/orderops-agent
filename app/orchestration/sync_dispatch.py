"""Leased synchronous execution with request-bound, crash-safe retry identity.

There is intentionally no raw-text inbox. A crash before a plan is committed
fails closed; a retry after plan commit reconstructs the original task's result.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Event, InputPayload, JobAttempt, Plan, SyncDispatchReceipt, Task
from app.db.session import begin_immediate
from app.errors import AppError
from app.observability import enrich, task_log_fields
from app.orchestration.state import aware, sync_guard, transition

if TYPE_CHECKING:
    from app.orchestration.dispatch import DispatchOutcome

LEASE_SECONDS = 60


def execute(
    factory: sessionmaker[Session],
    *,
    key: str,
    request_hash: str,
    clock: Callable[[], datetime],
    run: Callable[[str], "DispatchOutcome"],
    runtime_mode: str = "FIXTURE",
    before_create: Callable[[], object] | None = None,
) -> "DispatchOutcome":
    with factory() as session, begin_immediate(session):
        receipt = session.get(SyncDispatchReceipt, key)
        if receipt is not None:
            if receipt.request_hash != request_hash:
                raise AppError(
                    "调度事件键已用于不同请求", code="DISPATCH_REQUEST_CONFLICT", http_status=409
                )
            return replay(session, receipt, clock())
        # Old releases did not bind dispatch keys to their request. Do not guess
        # from a plan (or borrow a direct /plans event with the same public key).
        if session.scalar(
            select(Event.id).where(Event.thread_id == "demo", Event.source_event_key == key)
        ):
            raise AppError(
                "历史事件键无法验证请求身份，请使用新键",
                code="DISPATCH_REQUEST_CONFLICT",
                http_status=409,
            )
        # Replays/conflicts above do not need a usable model configuration.
        # Only a new request is checked, before any durable identity or lease.
        # This callback must perform local validation only: no model I/O/billing.
        if before_create is not None:
            before_create()
        task = Task(
            id=str(uuid4()),
            chain_id="CH-UNASSIGNED",
            status="RUNNING",
            intent_revision=0,
            runtime_mode=runtime_mode,
        )
        session.add(task)
        session.flush()
        session.add(
            SyncDispatchReceipt(client_event_key=key, task_id=task.id, request_hash=request_hash)
        )
        session.add(
            JobAttempt(
                id=str(uuid4()),
                task_id=task.id,
                attempt_no=1,
                lease_owner="dispatcher",
                lease_until=clock() + timedelta(seconds=LEASE_SECONDS),
                fencing_token=1,
                heartbeat_at=clock(),
                last_progress_at=clock(),
                step_count=0,
            )
        )
        task_id = task.id
    marker = sync_guard.set((task_id, 1, clock, LEASE_SECONDS))
    try:
        return run(task_id)
    finally:
        sync_guard.reset(marker)


def replay(session: Session, receipt: SyncDispatchReceipt, now: datetime) -> "DispatchOutcome":
    from app.orchestration.dispatch import DispatchOutcome, _step_views, _view

    task = session.get(Task, receipt.task_id)
    assert task is not None  # FK owned by the receipt transaction.
    enrich(**task_log_fields(session, task.id))
    plan = session.scalar(
        select(Plan)
        .join(Event, Event.payload_ref == Plan.id)
        .where(
            Plan.task_id == task.id,
            Event.thread_id == "demo",
            Event.source_event_key == f"sync:{task.id}",
        )
    )
    current_input = session.get(InputPayload, task.last_event_id) if task.last_event_id else None
    # The user can answer a synchronous clarification through the durable API.
    # Replaying the original synchronous request must never fence that worker.
    if task.status == "RUNNING" and plan is None and current_input is None:
        attempt = session.scalar(
            select(JobAttempt)
            .where(JobAttempt.task_id == task.id)
            .order_by(JobAttempt.attempt_no.desc())
        )
        if attempt and attempt.lease_until and aware(attempt.lease_until) > now:
            raise AppError(
                "该调度仍在执行，请稍后使用相同请求重试",
                code="DISPATCH_IN_PROGRESS",
                http_status=409,
                retryable=True,
            )
        transition(task, "FAILED")
        task.error_code = "INPUT_UNAVAILABLE"
        task.result_ref = "同步调度已中断，未保存可恢复输入；请核对后使用新事件键提交"
        if attempt:
            attempt.lease_owner = None
            attempt.lease_until = None
    if plan is None:
        return _view(
            session,
            task,
            status=task.status,
            error_code=task.error_code,
            issues=[task.result_ref] if task.result_ref else [],
        )
    stored = (plan.normalized_changes or {}).get("outcome") or {}
    questions = list(stored.get("questions") or [])
    issues = questions or ([str(stored["message"])] if stored.get("message") else [])
    return DispatchOutcome(
        task_id=task.id,
        chain_id=task.chain_id,
        status=plan.status if task.status == "AWAITING_APPROVAL" else task.status,
        plan_id=plan.id,
        order_id=task.order_id,
        intent_revision=plan.intent_revision,
        plan_hash=plan.plan_hash,
        requires_human_approval=task.status == "AWAITING_APPROVAL" and plan.status == "VALIDATED",
        issues=issues,
        error_code=task.error_code,
        steps=_step_views(session, task.id),
    )
