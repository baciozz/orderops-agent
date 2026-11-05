"""Worker configuration reaches the CLI and preserves the claim concurrency bound."""

import logging
from dataclasses import replace

import pytest
from app.cli import main
from app.config import Settings, get_settings, reset_settings_cache
from app.db.models import Task
from app.orchestration import outbox, watchdog, worker
from app.orchestration.inbox import accept_input
from app.orchestration.worker import DEFAULT_LIMITS, Limits, claim
from pydantic import ValidationError


def test_worker_once_uses_configured_active_limit(monkeypatch, database_url):
    # Populate the cache first so this also exercises reloading the environment.
    get_settings()
    monkeypatch.setenv("OOS_WORKER_MAX_ACTIVE", "1")
    monkeypatch.setenv("OOS_DATABASE_URL", database_url)
    monkeypatch.setenv("OOS_VERIFICATION_MAX_RETRIES", "0")
    reset_settings_cache()
    calls = []

    def run_once(factory, *, limits):
        assert limits.active == 1
        assert limits == replace(DEFAULT_LIMITS, active=1)
        with factory() as session:
            assert str(session.get_bind().url) == database_url
        calls.append("run_once")

    def scan(factory, *, settings):
        assert settings is get_settings()
        assert settings.database_url == database_url
        assert settings.worker_max_active == 1
        assert settings.verification_max_retries == 0
        with factory() as session:
            assert str(session.get_bind().url) == database_url
        calls.append("scan")

    monkeypatch.setattr(watchdog, "scan", scan)
    monkeypatch.setattr(worker, "run_once", run_once)
    monkeypatch.setattr(outbox, "publish_pending", lambda factory: calls.append("publish_pending"))

    assert main(["worker-once"]) == 0
    assert calls == ["scan", "run_once", "publish_pending"]


@pytest.mark.parametrize("configured", [3, 2])
def test_worker_once_clamps_active_limit(monkeypatch, database_url, caplog, configured):
    monkeypatch.setenv("OOS_WORKER_MAX_ACTIVE", str(configured))
    monkeypatch.setenv("OOS_DATABASE_URL", database_url)
    monkeypatch.setenv("OOS_VERIFICATION_MAX_RETRIES", "0")
    reset_settings_cache()
    caplog.set_level(logging.WARNING, logger="oos.worker")
    calls = []

    def run_once(factory, *, limits):
        assert limits.active == 2
        assert limits == replace(DEFAULT_LIMITS, active=2)
        calls.append("run_once")

    def scan(factory, *, settings):
        assert settings is get_settings()
        assert settings.database_url == database_url
        assert settings.worker_max_active == configured
        assert settings.verification_max_retries == 0
        with factory() as session:
            assert str(session.get_bind().url) == database_url
        calls.append("scan")

    monkeypatch.setattr(watchdog, "scan", scan)
    monkeypatch.setattr(worker, "run_once", run_once)
    monkeypatch.setattr(outbox, "publish_pending", lambda factory: calls.append("publish_pending"))

    assert main(["worker-once"]) == 0
    assert calls == ["scan", "run_once", "publish_pending"]
    warnings = [
        record
        for record in caplog.records
        if record.name == "oos.worker" and record.levelno == logging.WARNING
    ]
    if configured == 3:
        assert len(warnings) == 1
        assert "3" in warnings[0].getMessage()
        assert "2" in warnings[0].getMessage()
    else:
        assert warnings == []


def test_claim_with_active_one_leaves_second_task_queued(session_factory):
    task_ids = [
        accept_input(
            session_factory,
            thread_id="cleanup",
            client_event_key=key,
            text="合成测试输入",
        )[1]
        for key in ("first", "second")
    ]
    limits = Limits(active=1)
    first = claim(session_factory, "first-worker", limits=limits)
    assert first is not None
    assert first[0] in task_ids
    with session_factory() as session:
        assert session.get(Task, first[0]).status == "RUNNING"

    assert claim(session_factory, "second-worker", limits=limits) is None
    with session_factory() as session:
        assert sorted(session.get(Task, task_id).status for task_id in task_ids) == [
            "QUEUED",
            "RUNNING",
        ]


def test_worker_max_active_rejects_zero():
    with pytest.raises(ValidationError, match="Worker 最大并发任务数必须大于等于 1"):
        Settings(_env_file=None, worker_max_active=0)
