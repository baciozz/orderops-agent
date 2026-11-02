"""Restart recovery inspects committed operations before considering takeover."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.db.models import Event, JobAttempt, Operation, OutboxDelivery, Plan, Task
from app.db.session import begin_immediate
from app.orchestration.state import aware, transition, utcnow
from app.orchestration.worker import DEFAULT_LIMITS


def scan(factory, *, clock=utcnow, limits=DEFAULT_LIMITS):
    changed = []
    with factory() as session, begin_immediate(session):
        # Include terminal tasks: an acknowledgement can precede notification delivery.
        # Existing pending rows retain their retry schedule and consumer identity.
        for event in session.scalars(
            select(Event)
            .join(Operation, Operation.id == Event.payload_ref)
            .where(
                Event.kind == "order.committed",
                Operation.status == "COMMITTED",
                ~select(OutboxDelivery.id)
                .where(OutboxDelivery.event_id == Event.id, OutboxDelivery.consumer == "ui")
                .exists(),
            )
        ):
            session.add(
                OutboxDelivery(
                    id=str(uuid4()),
                    event_id=event.id,
                    consumer="ui",
                    delivery_state="PENDING",
                    attempts=0,
                    available_at=clock(),
                )
            )
        for task in session.scalars(
            select(Task).where(Task.status.in_(["RUNNING", "COMMITTING", "RECONCILING"]))
        ):
            operation = session.scalar(
                select(Operation)
                .join(Plan, Plan.id == Operation.plan_id)
                .where(Plan.task_id == task.id)
            )
            if operation:
                if task.status != "RECONCILING":
                    transition(task, "RECONCILING")
                transition(task, "SUCCEEDED")
                task.result_ref = operation.id
                changed.append(task.id)
                continue
            attempt = session.scalar(
                select(JobAttempt)
                .where(JobAttempt.task_id == task.id)
                .order_by(JobAttempt.attempt_no.desc())
            )
            if task.status != "RUNNING":
                continue
            deadline = task.deadline_at and aware(task.deadline_at) <= clock()
            stalled = (
                attempt
                and attempt.last_progress_at
                and aware(attempt.last_progress_at) + timedelta(seconds=limits.progress_seconds)
                <= clock()
            )
            expired = (
                not attempt or not attempt.lease_until or aware(attempt.lease_until) <= clock()
            )
            if deadline or stalled or expired:
                failed = deadline or (
                    attempt
                    and (
                        attempt.attempt_no >= limits.attempts or attempt.step_count >= limits.steps
                    )
                )
                transition(task, "FAILED" if failed else "QUEUED")
                task.error_code = "DEADLINE" if deadline else "ATTEMPT_LIMIT" if failed else None
                if attempt:
                    attempt.lease_until = None
                    attempt.lease_owner = None
                changed.append(task.id)
    return changed
