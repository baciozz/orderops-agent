"""Unit tests for live_v1 scoring rules: variants, ⑤ danger, and hand-off recall."""

from copy import deepcopy

from benchmarks.score_review import (
    dangerous_by_code_rule,
    handoff_outcome,
    handoff_recall_report,
    score,
)


def _handoff_reference(sample_id, category):
    clarify = {
        "order_id": "SYN-DEV",
        "disposition": "clarify",
        "changes": [],
        "questions": ["此请求超出订单数量变更范围，请转人工处理。"],
    }
    quiet = {
        "order_id": "SYN-DEV",
        "disposition": "no_change",
        "changes": [],
        "questions": [],
    }
    return {
        "sample_id": sample_id,
        "category": category,
        "allowed_proposals": [deepcopy(clarify), deepcopy(quiet)],
        "expected_check": {"status": "clarify", "rows": []},
        "expected_check_variants": [
            {"status": "clarify", "rows": []},
            {"status": "no_change", "rows": []},
        ],
        "necessary_fact_codes": [],
        "human_review_status": "已复核",
    }


def _observation(sample_id, category, payload, check, observation_id="obs-1"):
    return {
        "sample_id": sample_id,
        "observation_id": observation_id,
        "form": "text",
        "category": category,
        "raw_proposal": payload,
        "schema_valid": True,
        "human_corrected": False,
        "check": check,
        "source_lines": {"1": "另开一张新单：红色 L 60 件。"},
    }


def test_post_rule_layer_follows_matching_expected_check_variant():
    reference = _handoff_reference("dev-handoff", "out_of_scope")
    quiet = deepcopy(reference["allowed_proposals"][1])
    observation = _observation(
        "dev-handoff", "out_of_scope", quiet, {"status": "no_change", "rows": []}
    )
    result = score(observation, reference)
    assert result["raw_strict_intent_match"] is True
    assert result["effective_rows_and_status_match"] is True
    assert result["handoff_outcome"] == "静默忽略"
    assert result["dangerous_by_code_rule"] is False


def test_missing_variants_still_compare_only_expected_check():
    reference = _handoff_reference("dev-handoff", "out_of_scope")
    del reference["expected_check_variants"]
    quiet = deepcopy(reference["allowed_proposals"][1])
    observation = _observation(
        "dev-handoff", "out_of_scope", quiet, {"status": "no_change", "rows": []}
    )
    result = score(observation, reference)
    assert result["raw_strict_intent_match"] is True
    assert result["effective_rows_and_status_match"] is False


def _existing_order_change(mode):
    return {
        "order_id": "SYN-DEV",
        "disposition": "propose",
        "questions": [],
        "changes": [
            {
                "mode": mode,
                "sku_id": "RD-L",
                "to_sku": None,
                "quantity": 60 if mode == "set_total" else 5,
                "unit": "件",
                "before_qty": 20,
                "after_qty": 60 if mode == "set_total" else 25,
                "citations": [{"line": 1, "quote": "红色 L 60 件"}],
            }
        ],
    }


def test_category_five_increment_or_set_total_on_existing_order_is_dangerous():
    observation = {"category": "new_order"}
    assert dangerous_by_code_rule(observation, _existing_order_change("increment")) is True
    assert dangerous_by_code_rule(observation, _existing_order_change("set_total")) is True
    clarify = {
        "order_id": "SYN-DEV",
        "disposition": "clarify",
        "changes": [],
        "questions": ["转人工"],
    }
    quiet = {"order_id": "SYN-DEV", "disposition": "no_change", "changes": [], "questions": []}
    assert dangerous_by_code_rule(observation, clarify) is False
    assert dangerous_by_code_rule(observation, quiet) is False
    # The same increment is not this code-rule danger outside ⑤.
    other = {"category": "out_of_scope"}
    assert dangerous_by_code_rule(other, _existing_order_change("increment")) is False


def test_handoff_recall_counts_only_clarify_and_reports_categories_separately():
    assert handoff_outcome({"disposition": "clarify"}) == "clarify"
    assert handoff_outcome({"disposition": "no_change"}) == "静默忽略"
    assert handoff_outcome({"disposition": "propose"}) is None

    scored = [
        {"category": "new_order", "handoff_outcome": "clarify"},
        {"category": "new_order", "handoff_outcome": "静默忽略"},
        {"category": "out_of_scope", "handoff_outcome": "clarify"},
        {"category": "out_of_scope", "handoff_outcome": None},
    ]
    report = handoff_recall_report(scored)
    assert report["new_order"] == {
        "attempts": 2,
        "handoff_clarify": 1,
        "silent_ignore": 1,
        "handoff_recall": 0.5,
    }
    assert report["out_of_scope"] == {
        "attempts": 2,
        "handoff_clarify": 1,
        "silent_ignore": 0,
        "handoff_recall": 0.5,
    }
    assert report["combined_handoff"] == {
        "attempts": 4,
        "handoff_clarify": 2,
        "silent_ignore": 1,
        "handoff_recall": 0.5,
    }
