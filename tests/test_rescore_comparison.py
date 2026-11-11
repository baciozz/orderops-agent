"""Offline S3 rescore helpers: comparison rule, disagreement mirror, publication sanitizer."""

from benchmarks import rescore_comparison as rc


def _arms(single, same, roles):
    def reps(values):
        return {
            str(r): {
                "raw_accuracy": {"numerator": v, "denominator": 120},
                "effective_accuracy": {"numerator": v, "denominator": 120},
            }
            for r, v in zip((1, 2, 3), values, strict=True)
        }

    return {
        "single": {"by_repetition": reps(single)},
        "double_same_prompt": {"by_repetition": reps(same)},
        "double_roles": {"by_repetition": reps(roles)},
    }


def _verdicts(arms):
    return {(c["a"], c["b"]): c["verdict"] for c in rc.compare(arms, "raw_accuracy")}


def test_difference_needs_three_same_direction_repetitions_of_at_least_three():
    verdicts = _verdicts(_arms([102, 100, 102], [101, 98, 95], [97, 96, 98]))
    assert verdicts[("single", "double_roles")] == "有差异"
    # 1, 2, 7: same direction but two repetitions below 3 samples.
    assert verdicts[("single", "double_same_prompt")].startswith("无结论")
    # 4, 2, -3: direction flips.
    assert verdicts[("double_same_prompt", "double_roles")].startswith("无结论")


def test_disagreement_mirrors_runner_agreement():
    change = {
        "mode": "set_total",
        "sku_id": "S1",
        "to_sku": None,
        "quantity": 5,
        "unit": "件",
        "before_qty": 1,
        "after_qty": 5,
    }

    def call(qty, error=None):
        payload = {
            "disposition": "propose",
            "order_id": "O1",
            "changes": [dict(change, quantity=qty)],
            "questions": [],
        }
        return {
            "raw_proposal": payload,
            "schema_valid": True,
            "error": error,
            "check": {"status": "ready"},
        }

    assert rc._disagreement([call(5)]) is None
    assert rc._disagreement([call(5), call(5)]) is None
    assert rc._disagreement([call(5), call(6)]) == "intent_differs"
    assert rc._disagreement([call(5), call(5, "MODEL_TIMEOUT")]) == "call_failed_or_invalid"


def test_published_proposal_drops_free_text():
    payload = {
        "disposition": "clarify",
        "order_id": "O1",
        "changes": [{"mode": "set_total", "sku_id": "S1", "citations": [{"quote": "客户原话"}]}],
        "questions": ["请确认 sk-test-123"],
        "summary": "客户原话",
    }
    cleaned = rc._clean_proposal(payload)
    text = repr(cleaned)
    assert "客户原话" not in text and "sk-" not in text
    assert cleaned["question_count"] == 1
    check = rc._clean_check({"status": "clarify", "issues": ["原文 sk-x"], "rows": []})
    assert check == {"status": "clarify", "issue_count": 1, "rows": []}
