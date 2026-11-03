"""Order quantity definitions. Status is derived from shipment facts, never passed in."""

from schemas.enums import FulfillmentMode, OrderStatus


def unexecuted_qty(
    *,
    fulfillment_mode: str,
    ordered_qty: int,
    shipped_qty: int = 0,
    cut_qty: int = 0,
) -> int:
    """STOCK remaining is ordered minus shipped. MTO remaining is ordered minus cut.

    A negative result means the line already violates invariant 1 or 6.
    The function reports the number; the service decides the error.
    """
    if fulfillment_mode == FulfillmentMode.STOCK:
        return ordered_qty - shipped_qty
    if fulfillment_mode == FulfillmentMode.MTO:
        return ordered_qty - cut_qty
    raise ValueError(f"未知履约方式 {fulfillment_mode}")


def derive_order_status(
    *,
    lines: list[tuple[str, int, str]],
    shipped_by_sku: dict[str, int],
) -> str:
    """lines are (sku_id, ordered_qty, fulfillment_mode).

    MTO quantity is not treated as shipped in P0. Cancelling the unshipped
    remainder leaves the shipment rows in place, so a fully shipped stock
    order is FULFILLED rather than "never existed".
    """
    any_shipped = any(qty > 0 for qty in shipped_by_sku.values())
    unexecuted = False
    for _sku, ordered, mode in lines:
        if mode == FulfillmentMode.MTO and ordered > 0:
            unexecuted = True
        elif mode == FulfillmentMode.STOCK and ordered > shipped_by_sku.get(_sku, 0):
            unexecuted = True
    if not lines and not any_shipped:
        return OrderStatus.CANCELLED_UNSHIPPED
    if any_shipped and unexecuted:
        return OrderStatus.PARTIALLY_SHIPPED
    if any_shipped:
        return OrderStatus.FULFILLED
    if unexecuted:
        return OrderStatus.OPEN
    return OrderStatus.CANCELLED_UNSHIPPED
