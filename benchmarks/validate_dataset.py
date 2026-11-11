"""Offline labels/partition validator, outside the application and provider boundary."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from app.review.contracts import Proposal, Snapshot
from app.review.rules import check_proposal


def load(path):
    return json.loads(path.read_text())


def validate(root):
    frozen = load(root / "FREEZE.json")
    for name, digest in frozen["files"].items():
        path = root / name
        assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
    cases, risks, stories = [
        load(root / name) for name in ("cases.json", "risks.json", "stories.json")
    ]
    assert len(stories) == 120 and len(cases) == 360 and len(risks) == 60
    assert Counter(row["split"] for row in stories) == {"dev": 60, "validation": 20, "heldout": 40}
    assert Counter(row["split"] for row in cases) == {"dev": 180, "validation": 60, "heldout": 120}
    assert Counter(row["split"] for row in risks) == {"dev": 20, "validation": 10, "heldout": 30}
    grouped = defaultdict(list)
    for row in cases:
        grouped[row["story_id"]].append(row)
        assert row["synthetic"]
        assert not set(row) & {"expected", "oracle", "rendering_text"}
    assert all(
        len(rows) == 3 and len({row["split"] for row in rows}) == 1 for rows in grouped.values()
    )
    forms = Counter(row["form"] for row in cases if row["split"] == "heldout")
    assert len(forms) == 7 and min(forms.values()) >= 10
    outcomes = Counter()
    for sid, rows in grouped.items():
        row = rows[0]
        snapshot = Snapshot.model_validate(load(root / row["baseline_asset"]))
        # The oracle generator arithmetic is checked independently before consulting app rules.
        truth = load(root / "private_oracle" / (sid + ".json"))
        oracle = truth["oracle"]
        order = snapshot.orders[0]
        quantities = {line.sku_id: line.ordered_qty for line in order.lines}
        assert all(0 <= (line.shipped_qty or 0) <= line.ordered_qty for line in order.lines)
        assert all(0 <= (line.cut_qty or 0) <= line.ordered_qty for line in order.lines)
        if oracle["expected_check"] == "ready":
            for change in oracle["changes"]:
                if change["mode"] == "set_total":
                    quantities[change["sku_id"]] = change["quantity"]
                elif change["mode"] == "increment":
                    quantities[change["sku_id"]] += change["quantity"]
                elif change["mode"] == "replace_remaining":
                    quantities[change["sku_id"]] -= change["quantity"]
                    quantities[change["to_sku"]] += change["quantity"]
            assert quantities == oracle["expected_checklist_quantities"]
        assert oracle["expected_commit_state"] == "NOT_EXECUTED"
        assert oracle["expected_post_commit_state"] is None
        proposal = Proposal.model_validate(
            dict(
                order_id=order.order_id,
                disposition=oracle["disposition"],
                changes=[{**change, "citations": []} for change in oracle["changes"]],
                questions=oracle["required_clarifications"],
                summary="Offline oracle consistency only",
            )
        )
        checked = check_proposal(
            snapshot, order.order_id, truth["rendering_text"], proposal, human=True
        )
        assert checked["status"] == oracle["expected_check"], (sid, checked)
        outcomes[checked["status"]] += 1
    pending = load(root / "HUMAN_REVIEW_PENDING.json")
    assert len(pending) == 50 and all(row["status"] == "PENDING" for row in pending)
    return dict(
        structure="PASS",
        story_counts=dict(Counter(row["split"] for row in stories)),
        cases=len(cases),
        risks=len(risks),
        heldout_forms=dict(forms),
        arithmetic_and_rules_consistency=dict(outcomes),
        human_review="PENDING; not qualified",
        model_calls=0,
        fee_usd=0,
        real_model_metrics=None,
        freeze_sha256=hashlib.sha256((root / "FREEZE.json").read_bytes()).hexdigest(),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.dataset), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
