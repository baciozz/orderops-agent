"""Offline protocol/concurrency checks, not model quality or speed measurements."""

import json
from threading import Barrier

import pytest
from app.adapters.budget import BudgetLedger
from app.adapters.live_model import LiveLLMClient
from app.review import service
from app.review.contracts import Snapshot
from app.review.rules import check_proposal
from benchmarks.compare_readers import run_arm, schedule
from tests.test_review_workspace import configured, input_body, model, proposal, response


def prepared(settings):
    return service._model_request({"input": input_body()}, settings, "not-a-case-label")


def check(payload):
    body = input_body()
    return check_proposal(
        Snapshot.model_validate(body["snapshot"]),
        body["selected_order_id"],
        body["instruction"],
        payload,
    )


def test_schedule_balanced_and_frozen_before_labels():
    rows = schedule(["dev-one", "dev-two"], 3, 17)
    assert rows == schedule(["dev-one", "dev-two"], 3, 17)
    for sample in ["dev-one", "dev-two"]:
        arms = [r["arm"] for r in rows if r["sample_id"] == sample]
        assert all(arms.count(a) == 3 for a in {"single", "double_same_prompt", "double_roles"})


@pytest.mark.parametrize("arm", ["single", "double_same_prompt", "double_roles"])
def test_readers_share_input_schema_ledger_and_do_not_execute(session_factory, database_url, arm):
    settings = configured(database_url)
    seen = []
    barrier = Barrier(2) if arm != "single" else None

    def handler(request):
        seen.append(json.loads(request.content))
        if barrier:
            barrier.wait(timeout=5)
        return response()

    count = 1 if arm == "single" else 2
    clients = [model(session_factory, settings, handler) for _ in range(count)]
    record = run_arm(
        prepared(settings),
        arm=arm,
        clients=clients,
        settings=settings,
        sessions=session_factory,
        check=check,
        evidence_kind="mock_transport",
    )
    assert len(seen) == len(record["ledger"]) == record["client_call_attempts"] == count
    assert record["business_execution"] == "NOT_EXECUTED" and record["accuracy"] is None
    assert record["merged_check"]["status"] == "ready" and record["requires_human_confirmation"]
    assert all(r["messages"][1]["content"] == seen[0]["messages"][1]["content"] for r in seen)
    systems = {r["messages"][0]["content"] for r in seen}
    assert len(systems) == (2 if arm == "double_roles" else 1)


def test_disagreement_or_correlated_error_cannot_be_called_accuracy(session_factory, database_url):
    settings = configured(database_url)
    changed = proposal()
    changed["changes"][0].update(quantity=37, after_qty=37)
    clients = [
        model(session_factory, settings, lambda req: response()),
        model(session_factory, settings, lambda req: response(changed)),
    ]
    r = run_arm(
        prepared(settings),
        arm="double_same_prompt",
        clients=clients,
        settings=settings,
        sessions=session_factory,
        check=check,
        evidence_kind="mock_transport",
    )
    assert r["merged_proposal"] is None and r["needs_manual_resolution"]
    unsafe = proposal()
    unsafe["changes"][0].update(quantity=5, after_qty=5)
    clients = [model(session_factory, settings, lambda req: response(unsafe)) for _ in range(2)]
    r = run_arm(
        prepared(settings),
        arm="double_roles",
        clients=clients,
        settings=settings,
        sessions=session_factory,
        check=check,
        evidence_kind="mock_transport",
    )
    assert r["business_intent_agreement"] and r["merged_check"]["status"] == "blocked"
    assert r["needs_manual_resolution"] and r["advantage"] is None


def test_concurrent_budget_refusal_preserved_without_retry(session_factory, database_url):
    import httpx

    settings = configured(database_url)
    ledger = BudgetLedger(
        session_factory,
        cap_usd=settings.live_budget_usd,
        price_in_per_mtok=settings.price_in_per_mtok,
        price_out_per_mtok=settings.price_out_per_mtok,
        max_total_calls=1,
    )
    sent = []

    def handle(request):
        sent.append(request)
        return response()

    clients = [
        LiveLLMClient(settings, ledger, transport=httpx.MockTransport(handle)) for _ in range(2)
    ]
    r = run_arm(
        prepared(settings),
        arm="double_roles",
        clients=clients,
        settings=settings,
        sessions=session_factory,
        check=check,
        evidence_kind="mock_transport",
    )
    assert len(sent) == len(r["ledger"]) == 1
    assert sum(bool(c["error"]) for c in r["calls"]) == 1
    assert r["needs_manual_resolution"] and not r["ledger_complete"]


def test_reference_metadata_cannot_be_a_model_field(session_factory, database_url):
    settings = configured(database_url)
    request = prepared(settings)
    body = json.loads(request.user_prompt)
    body["expected"] = {"secret": "test-only"}
    request = request.model_copy(update={"user_prompt": json.dumps(body)})
    with pytest.raises(ValueError, match="public input fields"):
        run_arm(
            request,
            arm="single",
            clients=[
                model(session_factory, settings, lambda req: pytest.fail("must not transmit"))
            ],
            settings=settings,
            sessions=session_factory,
            check=check,
            evidence_kind="mock_transport",
        )


def test_reported_wall_time_includes_clarification_merge(
    session_factory, database_url, monkeypatch
):
    # Fake monotonic time avoids speed assumptions about the CI host. A real
    # delayed-merge experiment separately reproduced the missing 150ms scope.
    clock = [0.0]
    monkeypatch.setattr("benchmarks.compare_readers.perf_counter", lambda: clock[0])
    settings = configured(database_url)
    payload = {
        "order_id": "SYN-2401",
        "disposition": "clarify",
        "changes": [],
        "questions": ["请确认目标数量"],
        "summary": "开发计时夹具",
    }
    calls = []

    def merge_check(proposal):
        calls.append(proposal)
        if len(calls) == 3:
            clock[0] += 10.0
        return {"status": "clarify", "rows": []}

    record = run_arm(
        prepared(settings),
        arm="double_same_prompt",
        clients=[model(session_factory, settings, lambda req: response(payload)) for _ in range(2)],
        settings=settings,
        sessions=session_factory,
        check=merge_check,
        evidence_kind="mock_transport",
    )
    assert len(calls) == 3
    assert record["wall_s"] == 10.0 and record["summed_client_s"] == 0.0
