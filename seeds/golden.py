"""Numbers for the golden demo.

v1.0 gives SO-1001 v1 as black M 100, shipped 60, on-hand 140 / reserved 40,
and white M 120 / 0 / 120. v1.1 adds the MTO line, four downstream documents,
and assumes the blue L reservation of 10 comes from SO-1002.

The black M receipt is 200 so that receipt +200, reserve +100, ship −60
rebuilds on-hand 140 and reserved 40. v1.0 did not name the receipt quantity.
SO-1002's customer is 华南制衣; v1.1 did not name one.
C-NAVY-L consumes 2 metres of FAB-NAVY, which the X2 check requires.
"""

from datetime import UTC, datetime

SEED_AT = datetime(2026, 1, 15, tzinfo=UTC)
WAREHOUSE_ID = "W1"

# sku_id -> (on_hand, reserved, revision)
EXPECTED_BALANCES: dict[str, tuple[int, int, int]] = {
    "A-BLACK-M": (140, 40, 3),
    "A-WHITE-M": (120, 0, 1),
    "B-BLUE-L": (30, 10, 2),
}
