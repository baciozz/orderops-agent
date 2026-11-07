"""Synthetic synchronous config rejection and coherent configuration references."""

# ruff: noqa: F811

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event as Signal
from types import SimpleNamespace

import httpx
import pytest
from app.adapters.live_model import LiveLLMClient
from app.db.models import JobAttempt, ModelUsage, SyncDispatchReceipt, Task
from app.errors import AppError, LiveStartRefused
from app.main import create_app
from app.orchestration import dispatch, worker
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from app.orchestration.inbox import accept_input
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from tests import test_compat_provider as compatible
from tests.test_deployment_boundaries import mock_client, seeded  # noqa: F401
from tests.test_stage4_live import NOW, no_network, settings  # noqa: F401


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_model": None},
        {"llm_provider": "unknown"},
        {"llm_base_url": "https://mock.invalid/v1"},
    ],
)
def test_invalid_sync_config_leaves_no_identity_and_same_key_can_recover(seeded, overrides):
    calls = []
    options = dict(
        text=TEXT_BLACK_TO_WHITE,
        client_event_key="config-retry",
        now=NOW,
        client_factory=mock_client(calls),
    )
    with seeded() as session:
        before = session.scalar(select(func.count()).select_from(Task))
    with pytest.raises(LiveStartRefused):
        dispatch.dispatch_text(seeded, settings=settings(**overrides), **options)
    with seeded() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == before
        for model in (JobAttempt, SyncDispatchReceipt, ModelUsage):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    assert calls == []
    result = dispatch.dispatch_text(seeded, settings=settings(), **options)
    assert result.plan_id is not None
    assert dispatch.dispatch_text(seeded, settings=settings(), **options) == result
    assert len(calls) == 1


def test_concurrent_inplace_edit_cannot_mix_endpoint_and_key(seeded, monkeypatch):
    """Old deepcopy pauses after key A, letting a writer replace later URL fields."""
    key_a, key_b = "synthetic-key-A", "synthetic-key-B"
    url_a, url_b = "https://a.invalid/v1", "https://b.invalid/v1"
    original = compatible.configured(llm_api_key=key_a, llm_base_url=url_a)
    replacement = compatible.configured(llm_api_key=key_b, llm_base_url=url_b)
    holder = SimpleNamespace(settings=original)
    ready, release = Signal(), Signal()
    calls = []
    preflight = worker.validated_live_provider

    def copy_secret(secret, memo):
        captured = SecretStr(secret.get_secret_value())
        if captured.get_secret_value() == key_a:
            ready.set()
            assert release.wait(10)
        return captured

    def pause_preflight(configured):
        ready.set()
        assert release.wait(10)
        return preflight(configured)

    monkeypatch.setattr(SecretStr, "__deepcopy__", copy_secret, raising=False)
    monkeypatch.setattr(worker, "validated_live_provider", pause_preflight)
    monkeypatch.setattr(worker, "get_settings", lambda: holder.settings)

    def client(configured, ledger):
        def respond(request):
            calls.append((str(request.url), request.headers["authorization"]))
            return httpx.Response(200, json=compatible.response_data())

        return LiveLLMClient(
            configured, ledger, transport=httpx.MockTransport(respond), clock=lambda: NOW
        )

    accept_input(
        seeded,
        thread_id="one",
        client_event_key="one",
        text=TEXT_BLACK_TO_WHITE,
        runtime_mode="LIVE",
    )
    blocked = []
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.run_once, seeded, client_factory=client)
        assert ready.wait(10)
        try:
            for name, value in (("llm_base_url", url_b), ("llm_api_key", SecretStr(key_b))):
                try:
                    setattr(original, name, value)
                except ValidationError as exc:
                    blocked.append(exc.errors()[0]["type"])
            holder.settings = replacement
        finally:
            release.set()
        assert future.result(timeout=15).status == "AWAITING_APPROVAL"
    assert calls == [(url_a + "/chat/completions", "Bearer " + key_a)]
    assert blocked == ["frozen_instance", "frozen_instance"]
    accept_input(
        seeded,
        thread_id="two",
        client_event_key="two",
        text=TEXT_BLACK_TO_WHITE,
        runtime_mode="LIVE",
    )
    assert worker.run_once(seeded, client_factory=client).status == "AWAITING_APPROVAL"
    assert calls[-1] == (url_b + "/chat/completions", "Bearer " + key_b)


def test_http_config_rejection_then_restart_accepts_same_key(seeded, database_url, monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "LiveLLMClient", mock_client(calls))
    payload = {"text": TEXT_BLACK_TO_WHITE, "client_event_key": "http-config-retry"}
    with TestClient(
        create_app(settings(database_url=database_url, llm_model=None)),
        base_url="http://127.0.0.1",
    ) as client:
        response = client.post("/dispatch", json=payload)
        assert response.status_code == 503
    with seeded() as session:
        assert session.scalar(select(func.count()).select_from(SyncDispatchReceipt)) == 0
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 0
    with TestClient(
        create_app(settings(database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        response = client.post("/dispatch", json=payload)
        assert response.status_code == 200
        assert response.json()["plan_id"]
        assert (
            client.post("/dispatch", json=payload).json()["task_id"] == response.json()["task_id"]
        )
    assert len(calls) == 1


@pytest.mark.parametrize("live", [False, True])
def test_invalid_current_config_does_not_break_replay_or_conflict(seeded, live):
    calls = []
    options = dict(
        text=TEXT_BLACK_TO_WHITE,
        client_event_key="completed",
        now=NOW,
        client_factory=mock_client(calls),
    )
    first = dispatch.dispatch_text(seeded, settings=settings(live_enabled=live), **options)
    invalid = settings(llm_model=None)
    assert dispatch.dispatch_text(seeded, settings=invalid, **options) == first
    with pytest.raises(AppError) as error:
        dispatch.dispatch_text(seeded, settings=invalid, **{**options, "text": "different request"})
    assert error.value.code == "DISPATCH_REQUEST_CONFLICT"
    assert len(calls) == int(live)


@pytest.mark.parametrize("invalid_first", [True, False])
def test_valid_and_invalid_concurrent_sync_request(seeded, monkeypatch, invalid_first):
    entered, release = Signal(), Signal()
    calls = []
    options = dict(
        text=TEXT_BLACK_TO_WHITE,
        client_event_key="race",
        now=NOW,
        client_factory=mock_client(calls),
    )
    valid, invalid = settings(), settings(llm_model=None)
    validate = dispatch.validated_live_provider

    def pause(configured):
        if configured is invalid:
            entered.set()
            assert release.wait(10)
        return validate(configured)

    def pause_parser(ctx):
        if ctx.spec.agent_id == "parser":
            entered.set()
            assert release.wait(10)

    with ThreadPoolExecutor(2) as pool:
        if invalid_first:
            monkeypatch.setattr(dispatch, "validated_live_provider", pause)
            bad = pool.submit(dispatch.dispatch_text, seeded, settings=invalid, **options)
            assert entered.wait(10)
            good = pool.submit(dispatch.dispatch_text, seeded, settings=valid, **options)
            release.set()
            with pytest.raises(LiveStartRefused):
                bad.result(timeout=15)
            result = good.result(timeout=15)
        else:
            good = pool.submit(
                dispatch.dispatch_text, seeded, settings=valid, before_step=pause_parser, **options
            )
            assert entered.wait(10)
            try:
                with pytest.raises(AppError) as error:
                    dispatch.dispatch_text(seeded, settings=invalid, **options)
                assert error.value.code == "DISPATCH_IN_PROGRESS"
            finally:
                release.set()
            result = good.result(timeout=15)
    assert result.plan_id
    assert dispatch.dispatch_text(seeded, settings=invalid, **options) == result
    assert len(calls) == 1
    with seeded() as session:
        assert session.scalar(select(func.count()).select_from(SyncDispatchReceipt)) == 1
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 1


def test_lost_response_replays_with_invalid_config(seeded, monkeypatch):
    calls = []
    options = dict(
        text=TEXT_BLACK_TO_WHITE,
        client_event_key="lost-response",
        now=NOW,
        client_factory=mock_client(calls),
    )

    def lost(*args, **kwargs):
        raise RuntimeError("synthetic lost response")

    with monkeypatch.context() as patch:
        patch.setattr(dispatch, "_snapshot", lost)
        with pytest.raises(RuntimeError, match="lost response"):
            dispatch.dispatch_text(seeded, settings=settings(), **options)
    recovered = dispatch.dispatch_text(seeded, settings=settings(llm_model=None), **options)
    assert recovered.plan_id
    assert recovered.status == "VALIDATED"
    assert len(calls) == 1


def test_old_preflight_poisoned_receipt_does_not_silently_restart(seeded, monkeypatch):
    """No usage cannot prove whether old code performed other effects before dying."""
    calls = []
    options = dict(
        text=TEXT_BLACK_TO_WHITE,
        client_event_key="old-poisoned",
        client_factory=mock_client(calls),
    )
    with monkeypatch.context() as patch:
        # Reproduce the previous protocol: validation omitted before persistence.
        patch.setattr(dispatch, "validated_live_provider", lambda _: None)
        with pytest.raises(LiveStartRefused):
            dispatch.dispatch_text(seeded, settings=settings(llm_model=None), now=NOW, **options)
    with pytest.raises(AppError) as error:
        dispatch.dispatch_text(seeded, settings=settings(), now=NOW, **options)
    assert error.value.code == "DISPATCH_IN_PROGRESS"
    outcome = dispatch.dispatch_text(
        seeded, settings=settings(), now=NOW + timedelta(seconds=61), **options
    )
    assert outcome.status == "FAILED" and outcome.error_code == "INPUT_UNAVAILABLE"
    assert calls == []
    with seeded() as session:
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 1
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0


def test_unexpected_preflight_failure_rolls_back_identity(seeded, monkeypatch):
    def broken(_):
        raise RuntimeError("synthetic local validation interruption")

    with monkeypatch.context() as patch:
        patch.setattr(dispatch, "validated_live_provider", broken)
        with pytest.raises(RuntimeError, match="local validation interruption"):
            dispatch.dispatch_text(
                seeded,
                text=TEXT_BLACK_TO_WHITE,
                client_event_key="preflight-crash",
                settings=settings(),
            )
    with seeded() as session:
        assert session.scalar(select(func.count()).select_from(SyncDispatchReceipt)) == 0
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 0


def test_settings_normal_api_rejects_deletion_and_keeps_replacement_frozen():
    original = settings()
    with pytest.raises(ValidationError, match="frozen_instance"):
        del original.llm_model
    replacement = settings(llm_model="replacement")
    assert original.llm_model != replacement.llm_model
    with pytest.raises(ValidationError, match="frozen_instance"):
        replacement.llm_model = "another"
