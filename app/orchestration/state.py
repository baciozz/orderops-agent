"""Durable transitions and transaction-scoped fencing."""

from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.models import JobAttempt, Task
from app.errors import AppError
from app.observability import get_logger, log_event

logger = get_logger(__name__)

ALLOWED = {
    "RECEIVED": {"QUEUED", "CANCELLED"},
    "QUEUED": {"RUNNING", "CANCELLED", "FAILED", "SUPERSEDED"},
    "RUNNING": {
        "QUEUED",
        "NEEDS_CLARIFICATION",
        "AWAITING_APPROVAL",
        "FAILED",
        "CANCELLED",
        "SUPERSEDED",
        "RECONCILING",
    },
    "NEEDS_CLARIFICATION": {"QUEUED", "CANCELLED", "SUPERSEDED"},
    "AWAITING_APPROVAL": {"QUEUED", "COMMITTING", "CANCELLED", "SUPERSEDED"},
    "COMMITTING": {"SUCCEEDED", "RECONCILING", "FAILED"},
    "RECONCILING": {"SUCCEEDED", "FAILED"},
    "SUCCEEDED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
    "SUPERSEDED": set(),
}
guard: ContextVar[tuple[str, int, Callable, int] | None] = ContextVar("durable_guard", default=None)


def transition(task, target):
    if target not in ALLOWED[task.status]:
        raise AppError(
            f"非法状态转换 {task.status} → {target}", code="ILLEGAL_TRANSITION", http_status=409
        )
    task.status = target


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def check_fence(session, task_id, token, now):
    task = session.get(Task, task_id)
    attempt = session.scalar(
        select(JobAttempt)
        .where(JobAttempt.task_id == task_id)
        .order_by(JobAttempt.attempt_no.desc())
    )
    if (
        task is None
        or task.status != "RUNNING"
        or attempt is None
        or attempt.fencing_token != token
        or attempt.lease_until is None
        or aware(attempt.lease_until) <= now
    ):
        log_event(
            logger,
            "lease.expired_or_fenced",
            task_id=task_id,
            attempt_id=attempt.id if attempt else None,
            order_id=task.order_id if task else None,
            outcome="rejected",
            error_code="STALE_ATTEMPT",
        )
        raise AppError("旧 attempt 结果已丢弃", code="STALE_ATTEMPT", http_status=409)
    return task, attempt


def enforce(session):
    bound = guard.get()
    if bound:
        task_id, token, clock, _lease_seconds = bound
        check_fence(session, task_id, token, clock())


def utcnow():
    return datetime.now(UTC)
