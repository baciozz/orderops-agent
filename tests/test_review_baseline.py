"""Baseline instrumentation uses real service/ledger with synthetic MockTransport.

These tests verify accounting, not model understanding or performance superiority.
"""

from copy import deepcopy

import httpx
import pytest
from app.review import service
from app.review.contracts import CreateReview, ExtractRequest
from benchmarks.review_baseline import run_once, summarize
from tests.test_review_workspace import (
    all_business_rows,
    configured,
    input_body,
    model,
    proposal,
    response,
)

META = {
    "code_sha": "1" * 40,
    "dataset_sha256": "2" * 64,
    "evidence_kind": "mock_transport",
    "sample": "synthetic-development-only",
}


def execute(sessions, settings, handler, *, key="one"):
    review_id = service.create(sessions, CreateReview.model_validate(input_body(request_key=key)))
    request = ExtractRequest(revision=1, request_key="extract", acknowledge_transmission=True)
    client = model(sessions, settings, handler)
    return run_once(sessions, settings, review_id, request, client=client, metadata=META)


def test_same_client_call_observed_before_and_after_rules(session_factory, database_url):
    settings = configured(database_url)
    seen = []
    before = all_business_rows(session_factory)

    def handle(request):
        seen.append(request)
        # A well-formed but unsafe model quantity must remain visible as error.
        payload = proposal()
        payload["changes"][0].update(quantity=5, after_qty=5)
        return response(payload)

    record = execute(session_factory, settings, handle)
    assert len(seen) == record["call_count"] == 1
    assert record["raw_disposition"] == "propose"
    assert record["calls"][0]["payload"]["changes"][0]["after_qty"] == 5
    assert record["full_check"]["status"] == "blocked"
    assert record["cost_complete"] and len(record["ledger"]) == 1
    assert record["quality"] is None
    result = summarize([record])
    assert result["raw_propose"] == result["rule_blocked"] == result["client_call_attempts"] == 1
    assert result["accuracy"] is None
    assert all_business_rows(session_factory) == before
    assert b"synthetic-development-only" not in seen[0].content
    assert b"11111111111111111111" not in seen[0].content


@pytest.mark.parametrize("fault", ["timeout", "bad_json", "schema", "http_500", "cancel"])
def test_failures_and_discarded_results_stay_in_denominator(session_factory, database_url, fault):
    settings = configured(database_url)
    before = all_business_rows(session_factory)
    calls = []

    def handle(request):
        calls.append(request)
        if fault == "timeout":
            raise httpx.ReadTimeout("must not be copied into report", request=request)
        if fault == "bad_json":
            return httpx.Response(200, content=b"invalid provider body")
        if fault == "http_500":
            return httpx.Response(500, json={"error": {"message": "untrusted"}})
        if fault == "schema":
            return response({"order_id": "SYN-2401"})
        with session_factory() as session:
            from app.db.models import ReviewWorkspace

            row = session.query(ReviewWorkspace).one()
            rid, revision = row.id, row.revision
        service.cancel(session_factory, rid, revision)
        return response()

    record = execute(session_factory, settings, handle)
    result = summarize([record])
    assert result["attempts"] == result["client_call_attempts"] == len(calls) == 1
    assert result["rule_ready"] == 0
    assert record["cost_complete"] and record["ledger"][0]["status"] == "SETTLED"
    assert record["timing_s"]["rules_only"] is None
    assert all_business_rows(session_factory) == before
    assert result["accuracy"] is None
    if fault == "cancel":
        assert record["result_discarded"] and record["full_status"] == "CANCELLED"
    else:
        assert record["error"] and record["full_status"] == "ERROR"


def test_no_hidden_repeat_or_human_corrected_baseline(session_factory, database_url):
    from app.review.contracts import Correction

    settings = configured(database_url)
    record = execute(session_factory, settings, lambda request: response())
    request = ExtractRequest(revision=1, request_key="extract", acknowledge_transmission=True)
    with pytest.raises(ValueError, match="fresh synthetic"):
        run_once(
            session_factory,
            settings,
            record["review_id"],
            request,
            client=model(session_factory, settings, lambda request: pytest.fail("unexpected call")),
            metadata=META,
        )
    rid = service.create(
        session_factory, CreateReview.model_validate(input_body(request_key="human"))
    )
    service.correct(
        session_factory, rid, Correction(revision=1, proposal=proposal(), reason="human test")
    )
    with pytest.raises(ValueError, match="fresh synthetic"):
        run_once(
            session_factory,
            settings,
            rid,
            request,
            client=model(session_factory, settings, lambda request: pytest.fail("unexpected call")),
            metadata=META,
        )


def test_summary_refuses_duplicate_or_mixed_evidence(session_factory, database_url):
    record = execute(session_factory, configured(database_url), lambda request: response())
    with pytest.raises(ValueError, match="double count"):
        summarize([record, record])
    other = deepcopy(record)
    other["review_id"] = "another-id"
    other["metadata"]["evidence_kind"] = "real_provider"
    with pytest.raises(ValueError, match="separately"):
        summarize([record, other])


def test_budget_refusal_is_not_reported_as_verified_transmission(session_factory, database_url):
    record = execute(
        session_factory,
        configured(database_url, live_budget_usd="0"),
        lambda request: pytest.fail("budget refusal must happen before transport"),
    )
    result = summarize([record])
    assert result["attempts"] == result["errors"] == result["client_call_attempts"] == 1
    assert record["ledger"] == [] and not record["cost_complete"]
    assert result["transport_requests"] is None
    assert result["accuracy"] is None
