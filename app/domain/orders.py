"""Order quantity definitions.

TODO(stage-1A): derive OPEN / PARTIALLY_SHIPPED / FULFILLED / CANCELLED_UNSHIPPED
from shipment facts inside the service layer. Do not let a caller pass a target
status through.
"""

from schemas.enums import FulfillmentMode


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
    ordered_by_sku: dict[str, int],
    shipped_by_sku: dict[str, int],
    cancelled_unshipped: bool,
) -> str:
    raise NotImplementedError(
        "TODO(stage-1A): 由发货事实推导订单状态，已发部分取消剩余时不得谎称整单从未存在"
    )
