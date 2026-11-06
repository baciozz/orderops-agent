"""Atomic input receipt; delivery identity is independent of text content."""

from uuid import uuid4

from sqlalchemy import func, select

from app.adapters.uploads import validate_text
from app.db.models import Event, InboxDelivery, InputPayload, SalesOrder, Task
from app.db.session import begin_immediate
from app.domain.hashing import canonical_hash
from app.errors import AppError
from app.observability import enrich, get_logger, observed, text_digest
from app.orchestration.intent_receipts import receipt_metadata
from app.orchestration.state import transition, utcnow
from app.services.kernel import invalidate_create_intent, invalidate_order

logger = get_logger(__name__)


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


@observed(logger, "inbox.received", fields=lambda task_id, **_: {"task_id": task_id})
def accept_input(
    factory,
    *,
    thread_id,
    client_event_key,
    text=None,
    source_ids=None,
    selected_order_id=None,
    task_id=None,
    expected_intent_revision=None,
    expected_input_event_id=None,
    clock=utcnow,
    fault=lambda _: None,
):
    validate_text(text, allow_empty=bool(source_ids))
    enrich(**text_digest(text or ""))
    request_hash = _request_hash(text, source_ids, task_id, selected_order_id)
    with factory() as session, begin_immediate(session):
        event = session.scalar(
            select(Event).where(
                Event.thread_id == thread_id, Event.source_event_key == client_event_key
            )
        )
        if event:
            receipt = session.get(InputPayload, event.id)
            stored_hash = None
            if receipt is not None and event.kind in {"input.submit", "clarify_answer"}:
                stored_hash = receipt.body.get("request_hash_v1") or _request_hash(
                    receipt.body.get("text"),
                    receipt.body.get("source_ids"),
                    receipt.task_id if event.kind == "clarify_answer" else None,
                    event.order_id if event.kind == "input.submit" else None,
                )
            if receipt is None or stored_hash != request_hash:
                raise AppError(
                    "输入事件键已用于其他请求，请为新输入使用新的事件键",
                    code="INPUT_REQUEST_CONFLICT",
                    http_status=409,
                )
            enrich(task_id=receipt.task_id, input_id=event.id, duplicate=True)
            return event.id, receipt.task_id, session.get(Task, receipt.task_id).status
        if selected_order_id and session.get(SalesOrder, selected_order_id) is None:
            raise AppError("订单不存在", code="NOT_FOUND", http_status=404)
        if task_id:
            task = session.get(Task, task_id)
            # Receipt replay above deliberately precedes this fence: a lost ACK
            # must not append again or fail merely because the task has advanced.
            if (
                task is not None
                and expected_intent_revision is not None
                and task.intent_revision != expected_intent_revision
            ):
                raise AppError("意图已更新", code="STALE_INTENT", http_status=409)
            if task is not None and expected_input_event_id is not None:
                current_input = latest_input(session, task)
                if current_input is None or current_input.event_id != expected_input_event_id:
                    raise AppError("输入已更新", code="STALE_INTENT", http_status=409)
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
        order = None
        if task.order_id:
            order = session.get(SalesOrder, task.order_id)
            invalidate_order(session, order.id, exclude_task_id=task.id)
            order.intent_revision += 1
            task.intent_revision = order.intent_revision
        else:
            invalidate_create_intent(session, task)
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
                body={
                    "text": text,
                    "source_ids": source_ids or [],
                    "input_sequence": sequence,
                    "request_hash_v1": request_hash,
                    **receipt_metadata(session, order),
                },
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
        enrich(task_id=task.id, input_id=event.id, duplicate=False, status=task.status)
        return event.id, task.id, task.status


def _request_hash(text, source_ids, task_id, selected_order_id):
    # Fences are retry preconditions, not a new input. An answer's optional
    # selected_order_id is ignored by the established API; scope is inherited.
    return canonical_hash(
        {
            "text": text or "",
            "source_ids": source_ids or [],
            "task_id": task_id,
            "selected_order_id": selected_order_id if task_id is None else None,
        }
    )
