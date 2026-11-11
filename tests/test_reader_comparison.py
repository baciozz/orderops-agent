"""Offline protocol/concurrency checks, not model quality or speed measurements."""

import json
from decimal import Decimal
from threading import Barrier

import httpx
import pytest
from app.adapters.budget import BudgetLedger
from app.adapters.live_model import LiveLLMClient
from app.config import Settings
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


def _s3_settings(database_url, **overrides):
    values = dict(
        _env_file=None,
        database_url=database_url,
        live_enabled=True,
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:9999/v1",
        llm_api_key="test-key-not-a-secret",
        llm_model="gpt-6-luna",
        model_max_tokens=3000,
        live_budget_usd="25.00",
        price_in_per_mtok=None,
        price_out_per_mtok=None,
        model_prices={
            "gpt-6-luna": {"in_per_mtok": "0.10", "out_per_mtok": "0.50"},
            "gpt-6.1-sol": {"in_per_mtok": "2.00", "out_per_mtok": "10.00"},
        },
    )
    values.update(overrides)
    return Settings(**values)


def _compat_response(payload=None, *, prompt_tokens=100, completion_tokens=120):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            proposal() if payload is None else payload, ensure_ascii=False
                        )
                    },
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        },
    )


def _seed_ledger(path, settings, spend_usd):
    from app.adapters.budget import BudgetLedger
    from app.db.schema import init_db
    from app.db.session import make_engine, make_session_factory

    engine = make_engine(f"sqlite:///{path}")
    init_db(engine)
    sessions = make_session_factory(engine)
    book = BudgetLedger.from_settings(sessions, settings, cap_usd=Decimal("30.00"))
    prices = settings.prices_for(settings.llm_model)
    target = Decimal(spend_usd)
    output_tokens = int((target * Decimal("1000000") / prices[1]).to_integral_value())
    reservation = book.reserve(
        provider="openai",
        model=settings.llm_model,
        input_tokens=0,
        max_output_tokens=output_tokens,
        agent_id="seed-prior",
    )
    book.settle(reservation, input_tokens=0, output_tokens=output_tokens)
    engine.dispose()
    return book.spent()


def test_unpriced_model_is_refused_without_http_or_ledger(session_factory, database_url, tmp_path):
    from app.errors import PricesNotConfigured
    from benchmarks.run_live_eval import evaluate_comparison

    sent = []
    settings = _s3_settings(
        database_url,
        model_prices={"gpt-6-luna": {"in_per_mtok": "0.10", "out_per_mtok": "0.50"}},
    )
    ledger = tmp_path / "ledger.db"

    def forbidden(request):
        sent.append(request)
        raise AssertionError("unpriced model must not send HTTP")

    with pytest.raises(PricesNotConfigured):
        evaluate_comparison(
            settings=settings,
            ledger_db=ledger,
            arm="single",
            model="gpt-6.1-sol",
            transport=httpx.MockTransport(forbidden),
            out=tmp_path / "summary.json",
            records_path=tmp_path / "obs.jsonl",
        )
    assert sent == []
    assert not ledger.exists()


def test_cumulative_cap_across_arms_stops_before_http(session_factory, database_url, tmp_path):
    from benchmarks.run_live_eval import evaluate_comparison

    settings = _s3_settings(database_url)
    ledger = tmp_path / "ledger.db"
    _seed_ledger(ledger, settings, "10.00")
    sent = []

    def forbidden(request):
        sent.append(request)
        raise AssertionError("cap already reached; no more HTTP")

    summary = evaluate_comparison(
        settings=settings,
        ledger_db=ledger,
        arm="double_same_prompt",
        model="gpt-6-luna",
        transport=httpx.MockTransport(forbidden),
        out=tmp_path / "summary.json",
        records_path=tmp_path / "obs.jsonl",
    )
    assert summary["stop_reason"] == "BUDGET_EXCEEDED"
    assert summary["cost"]["client_call_attempts"] == 0
    assert sent == []


def test_first_ten_main_extrapolation_over_cap_stops(session_factory, database_url, tmp_path):
    from benchmarks.run_live_eval import evaluate_comparison

    settings = _s3_settings(database_url)
    ledger = tmp_path / "ledger.db"
    sent = []

    def handle(request):
        sent.append(request)
        # ~$0.01 / call at $0.50/MTok output so 1800-call projection exceeds $10.
        return _compat_response(completion_tokens=20_000)

    summary = evaluate_comparison(
        settings=settings,
        ledger_db=ledger,
        arm="single",
        model="gpt-6-luna",
        transport=httpx.MockTransport(handle),
        out=tmp_path / "summary.json",
        records_path=tmp_path / "obs.jsonl",
    )
    assert summary["stop_reason"] == "MAIN_COST_EXTRAPOLATION"
    assert len(sent) == 10
    assert summary["cost"]["client_call_attempts"] == 10
    projected = Decimal(summary["cost"]["projected_main_cumulative_usd"])
    assert projected > Decimal("10.00")
    assert summary["ledger_complete"] is True


def test_strong_smoke_extrapolation_over_cap_stops(session_factory, database_url, tmp_path):
    from benchmarks.run_live_eval import evaluate_comparison

    settings = _s3_settings(database_url, llm_model="gpt-6.1-sol")
    ledger = tmp_path / "ledger.db"
    _seed_ledger(ledger, settings, "21.50")
    sent = []

    def handle(request):
        sent.append(request)
        # $10/MTok output: 2000 tokens ≈ $0.02; 360 × $0.02 + $21.50 > $22.
        return _compat_response(completion_tokens=2000)

    summary = evaluate_comparison(
        settings=settings,
        ledger_db=ledger,
        arm="single",
        model="gpt-6.1-sol",
        smoke=True,
        transport=httpx.MockTransport(handle),
        out=tmp_path / "summary.json",
        records_path=tmp_path / "obs.jsonl",
    )
    assert summary["stop_reason"] == "STRONG_COST_EXTRAPOLATION"
    assert len(sent) == 5
    assert summary["cost"]["client_call_attempts"] == 5
    projected = Decimal(summary["cost"]["projected_strong_full_usd"])
    assert projected > Decimal("22.00")
    assert Decimal(summary["cost"]["pre_run_ledger_usd"]) >= Decimal("21.50")


def _s3_out(tmp_path):
    return dict(
        ledger_db=tmp_path / "ledger.db",
        out=tmp_path / "summary.json",
        records_path=tmp_path / "obs.jsonl",
        state_path=tmp_path / "state.json",
    )


def _auth_response():
    return httpx.Response(
        401, json={"error": {"type": "invalid_request_error", "code": "invalid_api_key"}}
    )


def _incomplete_response():
    body = _compat_response().json()
    body["choices"][0]["finish_reason"] = "length"
    return httpx.Response(200, json=body)


def _upstream_response():
    return httpx.Response(500, json={"error": {"type": "server_error", "code": "server_error"}})


def test_limit_stops_after_n_and_partial_summary(session_factory, database_url, tmp_path):
    from benchmarks.run_live_eval import evaluate_comparison

    settings = _s3_settings(database_url)
    sent = []

    def handle(request):
        sent.append(request)
        return _compat_response()

    summary = evaluate_comparison(
        settings=settings,
        arm="single",
        model="gpt-6-luna",
        smoke=False,
        limit=3,
        transport=httpx.MockTransport(handle),
        **_s3_out(tmp_path),
    )
    assert len(sent) == 3
    assert summary["stop_reason"] == "LIMIT"
    assert summary["attempted"] == 3
    assert summary["planned_attempts"] == 3
    assert summary["not_attempted"] == 0
    assert len(summary["rows"]) == 3
    assert summary["resumes"]["count"] == 0
    assert summary["run_id"]


def test_skip_completed_observations_and_resume_log(session_factory, database_url, tmp_path):
    from benchmarks.run_live_eval import (
        build_comparison_index,
        evaluate_comparison,
        verify_manifest,
    )

    settings = _s3_settings(database_url)
    manifest, _ = verify_manifest()
    rows, _, _ = build_comparison_index(manifest, arm="single")
    first_ids = [row["observation_id"] for row in rows[:4]]
    sent = []

    def handle(request):
        sent.append(request)
        return _compat_response()

    paths = _s3_out(tmp_path)
    first = evaluate_comparison(
        settings=settings,
        arm="single",
        model="gpt-6-luna",
        limit=2,
        transport=httpx.MockTransport(handle),
        **paths,
    )
    assert first["stop_reason"] == "LIMIT"
    assert first["run_id"]
    first_run = first["run_id"]
    assert len(sent) == 2

    second = evaluate_comparison(
        settings=settings,
        arm="single",
        model="gpt-6-luna",
        limit=4,
        transport=httpx.MockTransport(handle),
        **paths,
    )
    assert len(sent) == 4
    assert second["run_id"] == first_run
    assert second["resumes"]["count"] == 1
    assert second["resumes"]["starts"][0]["starting_observation_id"] == first_ids[2]
    assert second["stop_reason"] == "LIMIT"
    assert second["attempted"] == 4


def test_auth_failed_observation_is_retried(session_factory, database_url, tmp_path):
    from benchmarks.run_live_eval import (
        build_comparison_index,
        evaluate_comparison,
        verify_manifest,
    )

    settings = _s3_settings(database_url)
    rows, _, _ = build_comparison_index(verify_manifest()[0], arm="single")
    first_id = rows[0]["observation_id"]
    sent = []
    mode = {"auth": True}

    def handle(request):
        sent.append(request)
        if mode["auth"]:
            return _auth_response()
        return _compat_response()

    paths = _s3_out(tmp_path)
    first = evaluate_comparison(
        settings=settings,
        arm="single",
        model="gpt-6-luna",
        limit=3,
        transport=httpx.MockTransport(handle),
        **paths,
    )
    assert first["stop_reason"] == "MODEL_AUTH"
    assert len(sent) == 1
    # Attempted includes the auth failure; it is not a completed observation.
    assert first["attempted"] == 1

    mode["auth"] = False
    second = evaluate_comparison(
        settings=settings,
        arm="single",
        model="gpt-6-luna",
        limit=3,
        transport=httpx.MockTransport(handle),
        **paths,
    )
    assert len(sent) == 4
    assert second["run_id"] == first["run_id"]
    assert second["resumes"]["count"] == 1
    assert second["resumes"]["starts"][0]["starting_observation_id"] == first_id
    assert second["stop_reason"] == "LIMIT"
    assert second["attempted"] == 3


def test_only_retryable_codes_are_resent(session_factory, database_url, tmp_path):
    from benchmarks.run_live_eval import evaluate_comparison

    settings = _s3_settings(database_url)
    sent = []
    calls = {"n": 0}

    def handle(request):
        calls["n"] += 1
        sent.append(request)
        if calls["n"] == 1:
            return _incomplete_response()
        if calls["n"] == 2:
            return _upstream_response()
        return _compat_response()

    paths = _s3_out(tmp_path)
    first = evaluate_comparison(
        settings=settings,
        arm="single",
        model="gpt-6-luna",
        limit=3,
        transport=httpx.MockTransport(handle),
        **paths,
    )
    assert first["stop_reason"] == "LIMIT"
    assert first["attempted"] == 3
    assert len(sent) == 3
    # MODEL_INCOMPLETE and MODEL_UPSTREAM are completed failures, not stop codes.
    assert first["model_incomplete"]["single"] == 1

    before = len(sent)
    second = evaluate_comparison(
        settings=settings,
        arm="single",
        model="gpt-6-luna",
        limit=4,
        transport=httpx.MockTransport(handle),
        **paths,
    )
    assert len(sent) == before + 1
    assert second["resumes"]["count"] == 1
    assert second["attempted"] == 4


def test_code_sha_mismatch_refuses_resume(session_factory, database_url, tmp_path):
    import json

    from benchmarks.run_live_eval import evaluate_comparison

    settings = _s3_settings(database_url)
    paths = _s3_out(tmp_path)
    evaluate_comparison(
        settings=settings,
        arm="single",
        model="gpt-6-luna",
        limit=1,
        transport=httpx.MockTransport(lambda req: _compat_response()),
        **paths,
    )
    state = json.loads(paths["state_path"].read_text(encoding="utf-8"))
    state["code_sha"] = "0" * 40
    paths["state_path"].write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="RESUME_CODE_SHA_MISMATCH"):
        evaluate_comparison(
            settings=settings,
            arm="single",
            model="gpt-6-luna",
            limit=2,
            transport=httpx.MockTransport(lambda req: _compat_response()),
            **paths,
        )
