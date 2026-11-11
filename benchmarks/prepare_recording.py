"""Generate synthetic development assets for a future real-model recording.

No model, credentials, benchmark holdout, business writes or recording is used.
"""

import argparse
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from app.review.contracts import Snapshot
from app.review.sample import SAMPLE_SNAPSHOT


def prepare(output):
    snapshot = deepcopy(SAMPLE_SNAPSHOT)
    order_id = "SYN-REC-" + uuid4().hex[:12]
    snapshot["captured_at"] = datetime.now(UTC).isoformat()
    snapshot["orders"][0]["order_id"] = order_id
    snapshot["orders"][0]["lines"][0].update(ordered_qty=73, shipped_qty=9)
    Snapshot.model_validate(snapshot)
    instruction = f"这张{order_id}，麻烦把00123款藏蓝M总订购量调整成51件；已发走的9件保留。"
    output.mkdir(mode=0o700, exist_ok=False)
    (output / "baseline.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    (output / "customer.txt").write_text(instruction, encoding="utf-8")
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir()}
    record = {
        "status": "NOT_RECORDED",
        "kind": "development demo preparation only, not a heldout case",
        "files": files,
        "selected_order_id": order_id,
        "synthetic": True,
        "real_calls_made": 0,
        "actual_model": None,
        "specific_model_snapshot": None,
        "code_sha": None,
        "prompt_sha256": None,
        "schema_sha256": None,
        "actual_usage_and_cost": None,
        "start_end_and_duration": None,
        "raw_model_outcome": None,
        "manual_correction": None,
        "browser_confirmation_and_actual_export_readback": None,
        "business_state_before_after": None,
        "qualified_recording": False,
    }
    (output / "RECORDING_RECORD.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    for path in output.iterdir():
        path.chmod(0o600)
    return {"status": "NOT_RECORDED", "files": files, "real_calls_made": 0}


def main():
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output)))


if __name__ == "__main__":
    main()
