"""Server-side plan hashes. Callers never supply the authoritative hash."""

import hashlib
import json
from typing import Any


def canonical_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"sha256:{digest}"
