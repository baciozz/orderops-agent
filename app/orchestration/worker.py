"""Short claims and fenced fixture execution. Clock and crash boundaries are injectable."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from app.db.models import InboxDelivery, InputPayload, JobAttempt, Task
from app.db.session import begin_immediate
from app.errors import AppError
from app.orchestration.dispatch import dispatch_text
from app.orchestration.state import check_fence, guard, transition, utcnow


@dataclass(frozen=True)
class Limits:
    active: int = 2
    lease_seconds: int = 60
    progress_seconds: int = 120
    attempts: int = 3
    steps: int = 8


DEFAULT_LIMITS = Limits()


def claim(factory, owner, *, clock=utcnow, limits=DEFAULT_LIMITS):
    with factory() as session, begin_immediate(session):
        if session.scalar(
            select(func.count()).select_from(Task).where(Task.status == "RUNNING")
        ) >= min(2, limits.active):
            return None
        task = session.scalar(select(Task).where(Task.status == "QUEUED").order_by(Task.id))
        if task is None:
            return None
        previous = session.scalar(
            select(JobAttempt)
            .where(JobAttempt.task_id == task.id)
            .order_by(JobAttempt.attempt_no.desc())
        )
        number = previous.attempt_no + 1 if previous else 1
        if number > limits.attempts:
            transition(task, "FAILED")
            task.error_code = "ATTEMPT_LIMIT"
            return None
        transition(task, "RUNNING")
        attempt = JobAttempt(
            id=str(uuid4()),
            task_id=task.id,
            attempt_no=number,
            fencing_token=previous.fencing_token + 1 if previous else 1,
            lease_owner=owner,
            lease_until=clock() + timedelta(seconds=limits.lease_seconds),
            heartbeat_at=clock(),
            last_progress_at=clock(),
            step_count=0,
        )
        session.add(attempt)
        return task.id, attempt.fencing_token


def heartbeat(factory, task_id, token, *, progress=False, clock=utcnow, limits=DEFAULT_LIMITS):
    with factory() as session, begin_immediate(session):
        task, attempt = check_fence(session, task_id, token, clock())
        if attempt.step_count >= limits.steps:
            transition(task, "FAILED")
            task.error_code = "STEP_LIMIT"
            attempt.lease_until = None
            return False
        attempt.heartbeat_at = clock()
        attempt.lease_until = clock() + timedelta(seconds=limits.lease_seconds)
        if progress:
            attempt.last_progress_at = clock()
            attempt.step_count += 1
        return True


def run_once(
    factory,
    owner="worker",
    *,
    clock=utcnow,
    limits=DEFAULT_LIMITS,
    fault=lambda _: None,
    **dispatch_options,
):
    claimed = claim(factory, owner, clock=clock, limits=limits)
    if claimed is None:
        return None
    task_id, fence = claimed
    fault("after_claim")
    with factory() as session:
        task = session.get(Task, task_id)
        event_id = task.last_event_id
        payload = session.get(InputPayload, event_id).body
    caller_hook = dispatch_options.pop("before_step", None)

    def before_step(ctx):
        attempt = ctx.session.scalar(
            select(JobAttempt)
            .where(JobAttempt.task_id == task_id)
            .order_by(JobAttempt.attempt_no.desc())
        )
        if attempt.step_count >= limits.steps:
            raise AppError("已达到步骤上限", code="STEP_LIMIT", http_status=409)
        if caller_hook:
            caller_hook(ctx)

    marker = guard.set((task_id, fence, clock))
    try:
        outcome = dispatch_text(
            factory,
            text=payload["text"] or "",
            client_event_key=event_id,
            before_step=before_step,
            durable_task_id=task_id,
            now=clock(),
            **dispatch_options,
        )
    finally:
        guard.reset(marker)
    with factory() as session, begin_immediate(session):
        attempt = session.scalar(
            select(JobAttempt)
            .where(JobAttempt.task_id == task_id)
            .order_by(JobAttempt.attempt_no.desc())
        )
        if attempt.fencing_token == fence:
            attempt.lease_owner = None
            attempt.lease_until = None
            delivery = session.scalar(
                select(InboxDelivery).where(
                    InboxDelivery.event_id == event_id, InboxDelivery.consumer == "worker"
                )
            )
            delivery.delivery_state = "DELIVERED"
            delivery.attempts += 1
    with factory() as session:
        outcome.status = session.get(Task, task_id).status
    return outcome
