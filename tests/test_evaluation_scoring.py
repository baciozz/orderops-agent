"""Development mechanics only, not real labels or human-review evidence."""

from copy import deepcopy

import pytest
from benchmarks.score_review import score, sha, summarize


def example():
    proposal = {
        "order_id": "SYN-DEV",
        "disposition": "propose",
        "questions": [],
        "changes": [
            {
                "mode": "set_total",
                "sku_id": "BLUE",
                "to_sku": None,
                "quantity": 39,
                "unit": "件",
                "before_qty": 84,
                "after_qty": 39,
                "citations": [{"line": 1, "quote": "蓝色改39件"}],
            }
        ],
    }
    check = {"status": "ready", "rows": [{"sku_id": "BLUE", "old_qty": 84, "new_qty": 39}]}
    observation = {
        "sample_id": "dev",
        "observation_id": "dev-1",
        "form": "text",
        "category": "quantity",
        "raw_proposal": proposal,
        "schema_valid": True,
        "human_corrected": False,
        "check": check,
        "source_lines": {"1": "客户要求蓝色改39件"},
    }
    reference = {
        "sample_id": "dev",
        "allowed_proposals": [deepcopy(proposal)],
        "expected_check": deepcopy(check),
        "human_review_status": "PENDING",
    }
    return observation, reference


def test_rule_success_is_not_raw_intent_or_human_source_success():
    observation, reference = example()
    initial = score(observation, reference)
    assert initial["raw_strict_intent_match"] and initial["effective_rows_and_status_match"]
    assert initial["completion_candidate"] is None
    assert initial["dangerous_execution_observed"] is None
    observation["raw_proposal"]["changes"].append(
        {
            "mode": "set_total",
            "sku_id": "WHITE",
            "to_sku": None,
            "quantity": 45,
            "unit": "件",
            "before_qty": 45,
            "after_qty": 45,
            "citations": [],
        }
    )
    result = score(observation, reference)
    assert result["effective_rows_and_status_match"] and not result["raw_strict_intent_match"]
    assert result["redundant_zero_effect_actions"] == 1
    assert result["completion_candidate"] is False
    assert summarize([result])["formal_qualified"] is False


def test_safety_no_execution_is_separate_from_necessary_clarification():
    observation, reference = example()
    ref = {
        "order_id": "SYN-DEV",
        "disposition": "clarify",
        "changes": [],
        "questions": ["确认单位"],
    }
    reference.update(
        allowed_proposals=[ref],
        expected_check={"status": "clarify", "rows": []},
        necessary_fact_codes=["unit_conversion"],
    )
    observation.update(
        raw_proposal={**ref, "disposition": "no_change", "questions": []},
        check={"status": "no_change", "rows": []},
        execution_observation={
            "before_business_sha256": "a" * 64,
            "after_business_sha256": "a" * 64,
            "digest_scope": "all_business_tables_v1",
            "unauthorized_tool_use": False,
        },
    )
    result = score(observation, reference)
    assert result["dangerous_execution_observed"] is False
    assert result["necessary_clarification_covered"] is False
    assert result["completion_candidate"] is False


def test_semantic_review_bound_to_exact_payload_and_not_question_wording():
    observation, reference = example()
    payload = {
        "order_id": "SYN-DEV",
        "disposition": "clarify",
        "changes": [],
        "questions": ["每包多少件？"],
    }
    reference.update(
        allowed_proposals=[payload],
        expected_check={"status": "clarify", "rows": []},
        necessary_fact_codes=["unit_conversion"],
    )
    observation.update(raw_proposal=deepcopy(payload), check={"status": "clarify", "rows": []})
    observation["raw_proposal"]["questions"] = ["请提供一包的件数"]
    assert score(observation, reference)["necessary_clarification_covered"] is None
    # Mock attestation exercises binding, not a claim of an actual human reviewer.
    assessment = {
        "reviewer_kind": "human",
        "reviewer_code": "synthetic-unit-fixture-only",
        "owner_verified": True,
        "proposal_sha256": sha(observation["raw_proposal"]),
        "covered_fact_codes": ["unit_conversion"],
    }
    assert score(observation, reference, assessment)["necessary_clarification_covered"] is True
    assessment["proposal_sha256"] = "0" * 64
    assert score(observation, reference, assessment)["necessary_clarification_covered"] is None


@pytest.mark.parametrize("failure", ["timeout", "schema", "human", "bad_citation"])
def test_failures_stay_in_attempt_denominator(failure):
    observation, reference = example()
    if failure in {"timeout", "schema"}:
        observation.update(
            raw_proposal=None, schema_valid=False, error="MODEL_" + failure.upper(), check=None
        )
    elif failure == "human":
        observation["human_corrected"] = True
    else:
        observation["raw_proposal"]["changes"][0]["citations"] = [
            {"line": 1, "quote": "不存在的文字"}
        ]
    result = score(observation, reference)
    assert result["completion_candidate"] is False
    report = summarize([result])
    assert report["total"]["attempts"] == 1 and report["formal_qualified"] is False
    assert report["by_category"]["quantity"]["attempts"] == 1
    with pytest.raises(ValueError, match="duplicate"):
        summarize([result, result])


def test_business_digest_detects_same_count_update_and_new_tables(session_factory):
    from benchmarks.business_state import business_state

    with session_factory() as session:
        session.connection().exec_driver_sql(
            "CREATE TABLE evaluation_probe (id INTEGER, note TEXT)"
        )
        session.connection().exec_driver_sql(
            "INSERT INTO evaluation_probe VALUES (1, 'synthetic-before')"
        )
        session.commit()
    before = business_state(session_factory)
    with session_factory() as session:
        session.connection().exec_driver_sql("UPDATE evaluation_probe SET note='synthetic-after'")
        session.commit()
    after = business_state(session_factory)
    assert before["sha256"] != after["sha256"]
    assert next(r for r in before["tables"] if r["table"] == "evaluation_probe")["row_count"] == 1
    assert next(r for r in after["tables"] if r["table"] == "evaluation_probe")["row_count"] == 1
    assert "synthetic-after" not in str(after)
