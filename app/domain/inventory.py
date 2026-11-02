"""Stock definitions used by the seed and, later, by the commit transaction.

available = on_hand - reserved. Movements are append-only and must rebuild
the balance. TODO(stage-1A): call these checks inside the commit transaction
for invariants 2 and the B18 rebuild.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.errors import InventoryInvariantError


@dataclass(frozen=True)
class MovementView:
    sku_id: str
    delta_on_hand: int
    delta_reserved: int
    before_revision: int
    after_revision: int


@dataclass(frozen=True)
class BalanceView:
    on_hand: int
    reserved: int
    revision: int

    @property
    def available(self) -> int:
        return available_qty(self.on_hand, self.reserved)


def available_qty(on_hand: int, reserved: int) -> int:
    if on_hand < 0 or reserved < 0 or reserved > on_hand:
        raise InventoryInvariantError(
            "on_hand / reserved 不满足不变量 2（非负且 reserved ≤ on_hand）"
        )
    return on_hand - reserved


def rebuild_balances(movements: Sequence[MovementView]) -> dict[str, BalanceView]:
    """Replay append-only movements. Revisions must increase by 1 with no gaps."""
    grouped: dict[str, list[MovementView]] = {}
    for movement in movements:
        grouped.setdefault(movement.sku_id, []).append(movement)
    rebuilt: dict[str, BalanceView] = {}
    for sku_id, rows in grouped.items():
        on_hand = 0
        reserved = 0
        revision = 0
        for row in sorted(rows, key=lambda item: item.after_revision):
            if row.before_revision != revision:
                raise InventoryInvariantError(
                    f"{sku_id} 流水 revision 不连续：期望 {revision}，实际 {row.before_revision}"
                )
            on_hand += row.delta_on_hand
            reserved += row.delta_reserved
            if row.after_revision != revision + 1:
                raise InventoryInvariantError(f"{sku_id} 的 after_revision 必须逐次加一")
            available_qty(on_hand, reserved)
            revision = row.after_revision
        rebuilt[sku_id] = BalanceView(on_hand=on_hand, reserved=reserved, revision=revision)
    return rebuilt
