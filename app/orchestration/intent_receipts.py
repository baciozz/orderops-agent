"""Server-owned receipt chronology for safe late binding in the SQLite demo.

Ordinals and watermarks are allocated in inbox's serialized write transaction.
They are not model fields, wall-clock timestamps or task-local input sequences.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Event, InputPayload, SalesOrder
from app.errors import AppError


def receipt_metadata(session: Session, order: SalesOrder | None) -> dict[str, object]:
    ordinals = [body.get("receipt_ordinal") for body in session.scalars(select(InputPayload.body))]
    ordinal = max((n for n in ordinals if type(n) is int and n > 0), default=0) + 1
    metadata: dict[str, object] = {"receipt_ordinal": ordinal}
    if order is None:
        # Bounded demo tradeoff: O(orders) intake/storage, no business state mutation.
        metadata["intent_watermark"] = dict(
            session.execute(select(SalesOrder.id, SalesOrder.intent_revision)).all()
        )
    else:
        metadata["received_intent"] = {
            "order_id": order.id,
            "intent_revision": order.intent_revision,
        }
    return metadata


def check_acquisition_order(session: Session, receipt: InputPayload, order: SalesOrder) -> None:
    """Only supersede an intent proven to precede this receipt, before any mutation."""
    ordinal = receipt.body.get("receipt_ordinal")
    watermark = receipt.body.get("intent_watermark")
    if type(ordinal) is not int or ordinal <= 0 or not isinstance(watermark, dict):
        raise _unknown()
    receipts = session.scalars(select(InputPayload)).all()
    if sum(row.body.get("receipt_ordinal") == ordinal for row in receipts) != 1:
        raise _unknown()
    previous = watermark.get(order.id)
    if type(previous) is not int or previous < 0 or order.intent_revision < previous:
        raise _unknown()
    if order.intent_revision == previous:
        return  # The current intent already existed in this receipt's transaction.
    origins = []
    for candidate in receipts:
        for key in ("received_intent", "late_bound_intent"):
            acquired = candidate.body.get(key)
            if (
                isinstance(acquired, dict)
                and acquired.get("order_id") == order.id
                and type(acquired.get("intent_revision")) is int
                and acquired.get("intent_revision") == order.intent_revision
            ):
                origin = session.get(Event, candidate.event_id)
                if origin is None or origin.order_id != (
                    order.id if key == "received_intent" else None
                ):
                    raise _unknown()
                origins.append(candidate)
    if len(origins) != 1:
        raise _unknown()
    earlier = origins[0].body.get("receipt_ordinal")
    if (
        type(earlier) is not int
        or earlier <= 0
        or earlier == ordinal
        or sum(row.body.get("receipt_ordinal") == earlier for row in receipts) != 1
    ):
        raise _unknown()
    if earlier > ordinal:
        raise AppError(
            "该订单已有较新的输入，请核对当前订单并选择订单重新提交",
            code="STALE_INPUT_ORDER",
            http_status=409,
        )


def _unknown() -> AppError:
    return AppError(
        "无法可靠确定订单意图的先后，请核对当前订单并选择订单重新提交",
        code="INTENT_ORDER_UNPROVEN",
        http_status=409,
    )
