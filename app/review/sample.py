"""Synthetic input example, never a registered parser answer."""

SAMPLE_SNAPSHOT = {
    "synthetic": True,
    "captured_at": "2026-11-09T00:00:00Z",
    "catalog": [
        {
            "sku_id": "00123-藏蓝-M",
            "style": "00123",
            "color": "藏蓝",
            "size": "M",
            "fulfillment_mode": "STOCK",
        },
        {
            "sku_id": "00123-白-M",
            "style": "00123",
            "color": "白",
            "size": "M",
            "fulfillment_mode": "STOCK",
        },
    ],
    "orders": [
        {
            "order_id": "SYN-2401",
            "revision": 1,
            "lines": [{"sku_id": "00123-藏蓝-M", "ordered_qty": 50, "shipped_qty": 10}],
        }
    ],
    "inventory": [
        {"sku_id": "00123-藏蓝-M", "available_qty": 25},
        {"sku_id": "00123-白-M", "available_qty": 30},
    ],
    "documents": [],
}
SAMPLE_INSTRUCTION = "这张 SYN-2401 里，00123 款藏蓝 M 总数改为 36 件，已经发出的别动。"
