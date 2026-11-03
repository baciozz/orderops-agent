"""Atomic input receipt; delivery identity is independent of text content."""

from uuid import uuid4

from sqlalchemy import func, select

from app.db.models import Event, InboxDelivery, InputPayload, SalesOrder, Task
from app.db.session import begin_immediate
from app.errors import AppError
from app.orchestration.state import transition, utcnow
from app.services.kernel import invalidate_order


def latest_input(session, task):
    """Plan/commit events may replace last_event_id without carrying input text."""
    current = session.get(InputPayload, task.last_event_id) if task.last_event_id else None
    if current is not None:
        return current
    return session.scalar(
        select(InputPayload)
        .join(Event, Event.id == InputPayload.event_id)
        .where(InputPayload.task_id == task.id)
        .order_by(
            InputPayload.body["input_sequence"].as_integer().desc(),
            Event.received_at.desc(),
            Event.id.desc(),
        )
        .limit(1)
    )


def accept_input(
    factory,
    *,
    thread_id,
    client_event_key,
    text=None,
    source_ids=None,
    selected_order_id=None,
    task_id=None,
    clock=utcnow,
    fault=lambda _: None,
):
    with factory() as session, begin_immediate(session):
        event = session.scalar(
            select(Event).where(
                Event.thread_id == thread_id, Event.source_event_key == client_event_key
            )
        )
        if event:
            receipt = session.get(InputPayload, event.id)
            return event.id, receipt.task_id, session.get(Task, receipt.task_id).status
        if not text and not source_ids:
            raise AppError("需要 text 或 source_ids", code="INVALID_INPUT", http_status=400)
        if selected_order_id and session.get(SalesOrder, selected_order_id) is None:
            raise AppError("订单不存在", code="NOT_FOUND", http_status=404)
        if task_id:
            task = session.get(Task, task_id)
            if task is None or task.status not in {"NEEDS_CLARIFICATION", "AWAITING_APPROVAL"}:
                raise AppError("任务不能恢复", code="ILLEGAL_TRANSITION", http_status=409)
        else:
            task = Task(
                id=str(uuid4()),
                chain_id=thread_id,
                order_id=selected_order_id,
                status="RECEIVED",
                intent_revision=0,
            )
            session.add(task)
            session.flush()
        if task.order_id:
            order = session.get(SalesOrder, task.order_id)
            invalidate_order(session, order.id, exclude_task_id=task.id)
            order.intent_revision += 1
            task.intent_revision = order.intent_revision
        event = Event(
            id=str(uuid4()),
            source_event_key=client_event_key,
            thread_id=thread_id,
            chain_id=task.chain_id,
            order_id=task.order_id,
            kind="clarify_answer" if task_id else "input.submit",
            received_at=clock(),
        )
        session.add(event)
        session.flush()
        fault("after_event")
        task.last_event_id = event.id
        transition(task, "QUEUED")
        task.error_code = None
        sequence = (
            session.scalar(
                select(func.count())
                .select_from(InputPayload)
                .where(InputPayload.task_id == task.id)
            )
            + 1
        )
        session.add(
            InputPayload(
                event_id=event.id,
                task_id=task.id,
                body={"text": text, "source_ids": source_ids or [], "input_sequence": sequence},
            )
        )
        session.add(
            InboxDelivery(
                id=str(uuid4()),
                event_id=event.id,
                consumer="worker",
                delivery_state="PENDING",
                attempts=0,
                available_at=clock(),
            )
        )
        return event.id, task.id, task.status
