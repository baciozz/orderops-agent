"""Bounded, advisory surface similarity. Never decides identity or discards input."""

import json
import re
import unicodedata
from difflib import SequenceMatcher


def _signature(data: dict) -> str | None:
    raw = data["input"]
    if not raw.get("selected_order_id") or not raw.get("snapshot"):
        return None
    assets = data.get("assets", [])
    if any(not item.get("confirmed") or item.get("role") == "unknown" for item in assets):
        return None
    sources = [
        {
            field: asset.get(field)
            for field in ("sha256", "role", "mapping", "corrected_text", "note")
        }
        for asset in assets
    ]
    return json.dumps(
        {
            "order": raw["selected_order_id"],
            "snapshot": raw["snapshot"],
            "sources": sorted(sources, key=lambda item: json.dumps(item, sort_keys=True)),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def resembles(current: dict, previous: dict) -> bool:
    signature = _signature(current)
    if signature is None or signature != _signature(previous):
        return False
    left = unicodedata.normalize("NFKC", current["input"]["instruction"])
    right = unicodedata.normalize("NFKC", previous["input"]["instruction"])
    # Preserve quantity/sign tokens. A different quantity is a different request,
    # even if nearly every other character is identical.
    if re.findall(r"[+-]?\d+(?:\.\d+)?", left) != re.findall(r"[+-]?\d+(?:\.\d+)?", right):
        return False
    punctuation = "，。！？、,!?;；:："
    left, right = (
        "".join(char for char in text if not char.isspace() and char not in punctuation)
        for text in (left, right)
    )
    if not left or not right:
        return False
    if left == right:
        return True
    # Long documents still get exact normalized matching, without quadratic
    # fuzzy work. This is intentionally not a semantic duplicate detector.
    return max(len(left), len(right)) <= 1000 and SequenceMatcher(None, left, right).ratio() >= 0.92
