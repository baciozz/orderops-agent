"""At-least-once delivery; local consumers transact receipt and effect together."""

from datetime import timedelta
from time import perf_counter
from typing import Any

from sqlalchemy import select

from app.db.models import ConsumerReceipt, Event, Operation, OutboxDelivery
from app.db.session import begin_immediate
from app.observability import exception_code, get_logger, log_context, log_event, task_log_fields
from app.orchestration.state import aware, utcnow

logger = get_logger(__name__)


def _event_fields(session: Any, event_id: str) -> dict[str, Any]:
    try:
        event = session.get(Event, event_id)
        operation = (
            session.get(Operation, event.payload_ref) if event and event.payload_ref else None
        )
        if operation and operation.result_json.get("task_id"):
            return task_log_fields(session, operation.result_json["task_id"])
    except Exception:
        pass
    return {}


@log_context(exclude=("mode",))
def consume(factory, event_id, consumer, effect=lambda session, event_id: None, *, clock=utcnow):
    started = perf_counter()
    with factory() as session, begin_immediate(session):
        if session.get(ConsumerReceipt, (event_id, consumer)):
            log_event(
                logger,
                "outbox.duplicate_skipped",
                event_id=event_id,
                consumer=consumer,
                outcome="duplicate",
                duration_ms=round((perf_counter() - started) * 1000, 3),
                **_event_fields(session, event_id),
            )
            return False
        effect(session, event_id)
        session.add(ConsumerReceipt(event_id=event_id, consumer=consumer, received_at=clock()))
        return True


@log_context(exclude=("mode",))
def publish_pending(factory, send=None, *, clock=utcnow, fault=lambda _: None):
    with factory() as session:
        ids = list(
            session.scalars(
                select(OutboxDelivery.id).where(
                    OutboxDelivery.delivery_state != "DELIVERED",
                    OutboxDelivery.available_at <= clock(),
                )
            )
        )
    delivered = 0
    for delivery_id in ids:
        started = perf_counter()
        with factory() as session, begin_immediate(session):
            row = session.get(OutboxDelivery, delivery_id)
            if row.delivery_state == "DELIVERED" or aware(row.available_at) > clock():
                continue
            fields = _event_fields(session, row.event_id)
            row.attempts += 1
            row.available_at = clock() + timedelta(seconds=min(60, 2 ** min(row.attempts, 6)))
            event_id, consumer = row.event_id, row.consumer
        try:
            if send:
                send(event_id, consumer)
            else:
                consume(factory, event_id, consumer, clock=clock)
        except Exception as exc:
            log_event(
                logger,
                "outbox.failed",
                **fields,
                event_id=event_id,
                delivery_id=delivery_id,
                consumer=consumer,
                outcome="failed",
                error_code=exception_code(exc),
                duration_ms=round((perf_counter() - started) * 1000, 3),
            )
            continue
        try:
            fault("after_send")
            with factory() as session, begin_immediate(session):
                session.get(OutboxDelivery, delivery_id).delivery_state = "DELIVERED"
            delivered += 1
        except Exception as exc:
            log_event(
                logger,
                "outbox.failed",
                **fields,
                event_id=event_id,
                delivery_id=delivery_id,
                consumer=consumer,
                outcome="failed",
                error_code=exception_code(exc),
                duration_ms=round((perf_counter() - started) * 1000, 3),
            )
            raise
        log_event(
            logger,
            "outbox.delivered",
            **fields,
            event_id=event_id,
            delivery_id=delivery_id,
            consumer=consumer,
            outcome="success",
            duration_ms=round((perf_counter() - started) * 1000, 3),
        )
    return delivered
