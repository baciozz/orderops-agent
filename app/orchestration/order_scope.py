"""Server-owned input receipts constrain parser order selection."""

from schemas.business import BusinessEvent
from schemas.enums import BusinessEventType
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Event, InputPayload, Task


def conflicting_order(session: Session, task: Task, event: BusinessEvent | None) -> str | None:
    if event is None:
        return None
    selected = session.scalar(
        select(Event.order_id)
        .join(InputPayload, InputPayload.event_id == Event.id)
        .where(InputPayload.task_id == task.id, Event.kind == "input.submit")
    )
    if event.event_type == BusinessEventType.NEW_ORDER:
        return selected
    if event.event_type in (BusinessEventType.CHANGE_ORDER, BusinessEventType.CANCEL_REMAINING):
        # An answer may inherit an inferred order. It cannot move that order's
        # already-acquired intent to another order either.
        origin = session.get(Event, task.last_event_id) if task.last_event_id else None
        bound = selected or (origin.order_id if origin else None)
        if bound and event.order_id != bound:
            return bound
    return None
