"""At-least-once delivery; local consumers transact receipt and effect together."""

from datetime import timedelta

from sqlalchemy import select

from app.db.models import ConsumerReceipt, OutboxDelivery
from app.db.session import begin_immediate
from app.orchestration.state import aware, utcnow


def consume(factory, event_id, consumer, effect=lambda session, event_id: None, *, clock=utcnow):
    with factory() as session, begin_immediate(session):
        if session.get(ConsumerReceipt, (event_id, consumer)):
            return False
        effect(session, event_id)
        session.add(ConsumerReceipt(event_id=event_id, consumer=consumer, received_at=clock()))
        return True


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
        with factory() as session, begin_immediate(session):
            row = session.get(OutboxDelivery, delivery_id)
            if row.delivery_state == "DELIVERED" or aware(row.available_at) > clock():
                continue
            row.attempts += 1
            row.available_at = clock() + timedelta(seconds=min(60, 2 ** min(row.attempts, 6)))
            event_id, consumer = row.event_id, row.consumer
        try:
            if send:
                send(event_id, consumer)
            else:
                consume(factory, event_id, consumer, clock=clock)
        except Exception:
            continue
        fault("after_send")
        with factory() as session, begin_immediate(session):
            session.get(OutboxDelivery, delivery_id).delivery_state = "DELIVERED"
        delivered += 1
    return delivered
