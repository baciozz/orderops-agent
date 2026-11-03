"""Quantity definitions the later stages will call. Not a business workflow."""

import pytest
from app.domain.inventory import MovementView, available_qty, rebuild_balances
from app.domain.orders import derive_order_status, unexecuted_qty
from app.errors import InventoryInvariantError


def test_available_and_unexecuted_definitions() -> None:
    assert available_qty(140, 40) == 100
    assert unexecuted_qty(fulfillment_mode="STOCK", ordered_qty=100, shipped_qty=60) == 40
    assert unexecuted_qty(fulfillment_mode="MTO", ordered_qty=300, cut_qty=120) == 180
    with pytest.raises(InventoryInvariantError):
        available_qty(10, 11)


def test_rebuild_rejects_a_gap() -> None:
    with pytest.raises(InventoryInvariantError):
        rebuild_balances(
            [
                MovementView(
                    sku_id="A-BLACK-M",
                    delta_on_hand=10,
                    delta_reserved=0,
                    before_revision=1,
                    after_revision=2,
                )
            ]
        )


def test_status_derivation_is_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="stage-1A"):
        derive_order_status(ordered_by_sku={}, shipped_by_sku={}, cancelled_unshipped=False)
