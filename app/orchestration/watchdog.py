"""Restart recovery inspects committed operations before considering takeover."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.db.models import Event, InputPayload, JobAttempt, Operation, OutboxDelivery, Plan, Task
from app.db.session import begin_immediate
from app.observability import enrich, get_logger, log_event, observed
from app.orchestration.state import aware, transition, utcnow
from app.orchestration.worker import DEFAULT_LIMITS
from app.services.kernel import retry_pending_verification

logger = get_logger(__name__)


@observed(
    logger,
    "watchdog.scan",
    start_event="watchdog.scan",
    exclude=("mode", "task_id", "attempt_id", "order_id"),
    fields=lambda **_: {
        "scanned_tasks": 0,
        "recovered_tasks": 0,
        "outbox_created": 0,
        "pending_retries": 0,
    },
)
def scan(factory, *, clock=utcnow, limits=DEFAULT_LIMITS, settings=None):
    changed = []
    scanned = outbox_created = 0
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
            outbox_created += 1
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
            scanned += 1
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
                log_event(
                    logger,
                    "lease.expired_or_fenced",
                    task_id=task.id,
                    attempt_id=attempt.id if attempt else None,
                    order_id=task.order_id,
                    outcome="expired",
                    reason="deadline" if deadline else "stalled" if stalled else "lease_expired",
                )
                receipt = (
                    session.get(InputPayload, task.last_event_id) if task.last_event_id else None
                )
                missing_input = receipt is None or receipt.task_id != task.id
                failed = (
                    missing_input
                    or deadline
                    or (
                        attempt
                        and (
                            attempt.attempt_no >= limits.attempts
                            or attempt.step_count >= limits.steps
                        )
                    )
                )
                transition(task, "FAILED" if failed else "QUEUED")
                task.error_code = (
                    "INPUT_UNAVAILABLE"
                    if missing_input
                    else "DEADLINE"
                    if deadline
                    else "ATTEMPT_LIMIT"
                    if failed
                    else None
                )
                if missing_input:
                    task.result_ref = "任务缺少可恢复输入，请核对后重新提交"
                if attempt:
                    attempt.lease_until = None
                    attempt.lease_owner = None
                changed.append(task.id)
    retries = retry_pending_verification(factory, clock=clock, settings=settings)
    enrich(
        scanned_tasks=scanned,
        recovered_tasks=len(changed),
        outbox_created=outbox_created,
        pending_retries=retries,
    )
    return changed
