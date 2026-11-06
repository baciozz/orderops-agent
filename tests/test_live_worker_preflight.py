"""Offline config eligibility must precede durable claims and attempt counters."""

# ruff: noqa: F811

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from app import cli
from app.adapters.llm import validated_live_provider
from app.api.demo_ui import worker_once as demo_worker_once
from app.db.models import InboxDelivery, JobAttempt, ModelUsage, Task
from app.db.session import make_engine, make_session_factory
from app.errors import LiveStartRefused
from app.orchestration import dispatch, watchdog, worker, worker_cycle
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from app.orchestration.inbox import accept_input
from app.orchestration.worker_loop import LoopOptions, run_loop
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from tests.test_deployment_boundaries import input_request, mock_client, seeded  # noqa: F401
from tests.test_stage4_live import NOW, no_network, settings  # noqa: F401

INVALID = [
    pytest.param({"llm_model": None}, "MODEL_NOT_CONFIGURED", id="missing-model"),
    pytest.param({"llm_model": "  "}, "MODEL_NOT_CONFIGURED", id="blank-model"),
    pytest.param({"llm_provider": "unknown"}, "PROVIDER_UNSUPPORTED", id="unknown-provider"),
    pytest.param({"llm_provider": None}, "PROVIDER_UNSUPPORTED", id="missing-provider"),
    pytest.param({"llm_base_url": "https://mock.invalid/v1"}, "BASE_URL_UNSUPPORTED", id="bad-url"),
    pytest.param({"llm_base_url": None}, "BASE_URL_UNSUPPORTED", id="missing-url"),
    pytest.param({"price_in_per_mtok": None}, "PRICES_NOT_CONFIGURED", id="missing-price"),
    pytest.param({"llm_api_key": None}, "KEY_NOT_CONFIGURED", id="missing-key"),
    pytest.param(
        {"llm_provider": "openai_compatible", "llm_base_url": "http://mock.invalid/v1"},
        "BASE_URL_INSECURE",
        id="compatible-http",
    ),
    pytest.param(
        {
            "llm_provider": "openai_compatible",
            "llm_base_url": "https://mock.invalid/v1",
            "llm_model": None,
        },
        "MODEL_NOT_CONFIGURED",
        id="compatible-model",
    ),
]


@pytest.mark.parametrize("overrides,reason", INVALID)
def test_bad_configuration_never_consumes_claim_or_recovery_attempts(seeded, overrides, reason):
    with pytest.raises(LiveStartRefused) as refused:
        validated_live_provider(settings(**overrides))
    assert refused.value.reason == reason
    _, task_id, _ = accept_input(
        seeded,
        thread_id="preflight",
        client_event_key="one",
        text=TEXT_BLACK_TO_WHITE,
        runtime_mode="LIVE",
        clock=lambda: NOW,
    )
    calls = []
    refusal = None
    for index in range(5):
        moment = NOW + timedelta(seconds=index * 61)
        try:
            worker.run_once(
                seeded,
                settings=settings(**overrides),
                clock=lambda moment=moment: moment,
                client_factory=mock_client(calls),
            )
        except LiveStartRefused as exc:
            # Baseline raises only after spending the task's next attempt.
            refusal = exc.reason
        watchdog.scan(seeded, clock=lambda moment=moment: moment + timedelta(seconds=61))
    assert calls == []
    assert refusal in (None, reason)
    with seeded() as session:
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0
        attempts = session.scalar(select(func.count()).select_from(JobAttempt))
        task = session.get(Task, task_id)
        assert (task.status, task.error_code, attempts) == ("QUEUED", None, 0)


@pytest.mark.parametrize(
    "variant",
    ["explicit", "global", "live-cycle", "fixture-cycle", "loop", "cli-once", "cli-loop", "demo"],
)
def test_consumer_variants_preserve_live_queue_and_serve_fixture(
    seeded, database_url, monkeypatch, variant
):
    _, live_id, _ = accept_input(
        seeded,
        thread_id="live",
        client_event_key="live",
        text=TEXT_BLACK_TO_WHITE,
        runtime_mode="LIVE",
    )
    _, fixture_id, _ = accept_input(
        seeded, thread_id="fixture", client_event_key="fixture", text=TEXT_BLACK_TO_WHITE
    )
    configured = settings(llm_model=None, database_url=database_url)
    calls = []
    monkeypatch.setattr(dispatch, "LiveLLMClient", mock_client(calls))
    monkeypatch.setattr(worker, "get_settings", lambda: configured)
    monkeypatch.setattr(cli, "get_settings", lambda: configured)
    for _ in range(2):
        if variant == "explicit":
            worker.run_once(seeded, settings=configured)
        elif variant == "global":
            worker.run_once(seeded)
        elif variant in {"live-cycle", "fixture-cycle"}:
            worker_cycle.run_cycle(seeded, configured, fixture_only=variant == "fixture-cycle")
        elif variant == "loop":
            run_loop(seeded, configured, options=LoopOptions(0.1, 2), wait=lambda _: None)
        elif variant == "cli-once":
            assert cli.main(["worker-once"]) == 0
        elif variant == "cli-loop":
            assert cli.main(["worker-loop", "--max-iterations", "1"]) == 0
        else:
            request = input_request(seeded, configured)
            request.app.state.engine = seeded.kw["bind"]
            demo_worker_once(request)
    with seeded() as session:
        assert session.get(Task, live_id).status == "QUEUED"
        assert session.get(Task, live_id).runtime_mode == "LIVE"
        assert session.get(Task, fixture_id).status == "AWAITING_APPROVAL"
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 1
        assert session.scalar(select(JobAttempt.task_id)) == fixture_id
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0
    assert calls == []


def test_expired_prior_attempt_waits_for_valid_consumer_then_recovers(seeded, monkeypatch):
    _, task_id, _ = accept_input(
        seeded,
        thread_id="recovery",
        client_event_key="one",
        text=TEXT_BLACK_TO_WHITE,
        runtime_mode="LIVE",
        clock=lambda: NOW,
    )
    assert worker.claim(seeded, "prior", clock=lambda: NOW, allow_live=True) == (task_id, 1)
    calls = []
    monkeypatch.setattr(dispatch, "LiveLLMClient", mock_client(calls))
    later = NOW + timedelta(seconds=61)
    for _ in range(5):
        worker_cycle.run_cycle(
            seeded, settings(llm_model=None), clock=lambda: later, fixture_only=False
        )
    with seeded() as session:
        assert session.get(Task, task_id).status == "QUEUED"
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 1
        assert session.scalar(select(InboxDelivery.attempts)) == 0
    assert calls == []
    outcome = worker.run_once(seeded, settings=settings(), clock=lambda: later)
    assert outcome.task_id == task_id and outcome.status == "AWAITING_APPROVAL"
    assert len(calls) == 1
    with seeded() as session:
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 2
        assert session.scalar(select(func.max(JobAttempt.fencing_token))) == 2
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 1


def test_valid_and_invalid_consumers_compete_without_wasting_attempts(seeded, database_url):
    _, task_id, _ = accept_input(
        seeded,
        thread_id="race",
        client_event_key="one",
        text=TEXT_BLACK_TO_WHITE,
        runtime_mode="LIVE",
    )
    barrier = Barrier(2)
    calls = []

    def consume(configured):
        engine = make_engine(database_url)
        try:
            barrier.wait(timeout=5)
            return worker.run_once(
                make_session_factory(engine), settings=configured, client_factory=mock_client(calls)
            )
        finally:
            engine.dispose()

    with ThreadPoolExecutor(2) as pool:
        invalid = pool.submit(consume, settings(llm_model=None))
        valid = pool.submit(consume, settings())
        assert invalid.result(timeout=20) is None
        assert valid.result(timeout=20).task_id == task_id
    assert len(calls) == 1
    with seeded() as session:
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 1
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 1


def test_original_settings_mutation_after_claim_cannot_invalidate_preflight(seeded):
    configured = settings()
    _, task_id, _ = accept_input(
        seeded,
        thread_id="snapshot",
        client_event_key="one",
        text=TEXT_BLACK_TO_WHITE,
        runtime_mode="LIVE",
    )
    calls = []

    def mutate_source(_):
        configured.llm_model = None
        configured.llm_provider = "unknown"
        configured.live_enabled = False

    outcome = worker.run_once(
        seeded, settings=configured, fault=mutate_source, client_factory=mock_client(calls)
    )
    assert outcome.task_id == task_id and outcome.status == "AWAITING_APPROVAL"
    assert len(calls) == 1
    assert configured.live_enabled is False


def test_preflight_is_local_and_unexpected_validation_fault_precedes_claim(seeded, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No I/O or reservation during preflight")

    monkeypatch.setattr("httpx.Client", forbidden)
    monkeypatch.setattr("app.adapters.budget.BudgetLedger.reserve", forbidden)
    assert validated_live_provider(settings()).name == "openai"
    assert (
        validated_live_provider(
            settings(llm_provider="openai_compatible", llm_base_url="https://mock.invalid/v1")
        ).name
        == "openai_compatible"
    )
    _, task_id, _ = accept_input(
        seeded,
        thread_id="fault",
        client_event_key="one",
        text=TEXT_BLACK_TO_WHITE,
        runtime_mode="LIVE",
    )
    monkeypatch.setattr("app.adapters.providers.provider_for_settings", forbidden)
    with pytest.raises(AssertionError):
        worker.run_once(seeded, settings=settings())
    with seeded() as session:
        assert session.get(Task, task_id).status == "QUEUED"
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 0


def test_invalid_config_logs_only_static_reason_and_keeps_fixture_isolation(seeded, caplog):
    accept_input(seeded, thread_id="fixture", client_event_key="one", text=TEXT_BLACK_TO_WHITE)
    calls = []
    outcome = worker.run_once(
        seeded,
        settings=settings(llm_provider="PRIVATE_CONFIG_MARKER"),
        client_factory=mock_client(calls),
    )
    assert outcome.status == "AWAITING_APPROVAL" and calls == []
    records = [r for r in caplog.records if getattr(r, "event", None) == "worker.live_unavailable"]
    assert len(records) == 1 and records[0].reason == "PROVIDER_UNSUPPORTED"
    assert "PRIVATE_CONFIG_MARKER" not in caplog.text


def test_sql_replace_is_outside_update_trigger_boundary(engine):
    """Document the actual DB boundary, without endorsing a replacement writer."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO task (id, chain_id, status, intent_revision, cancel_requested) "
            "VALUES ('replace-probe', 'synthetic', 'QUEUED', 0, 0)"
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql("UPDATE task SET runtime_mode='LIVE' WHERE id='replace-probe'")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT OR REPLACE INTO task "
            "(id, chain_id, status, intent_revision, cancel_requested, runtime_mode) "
            "VALUES ('replace-probe', 'synthetic', 'QUEUED', 0, 0, 'LIVE')"
        )
        assert (
            connection.exec_driver_sql(
                "SELECT runtime_mode FROM task WHERE id='replace-probe'"
            ).scalar_one()
            == "LIVE"
        )
