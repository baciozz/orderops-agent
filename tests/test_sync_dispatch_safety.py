"""Synthetic synchronous retry, lease, takeover and recovery regressions."""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event as Signal

import app.orchestration.dispatch as dispatch
import pytest
from app.db.models import InputPayload, JobAttempt, Plan, SyncDispatchReceipt, Task
from app.db.session import begin_immediate
from app.errors import AppError
from app.orchestration.fixtures import TEXT_BAD_SCHEMA, TEXT_BLACK_TO_WHITE, TEXT_NEW_ORDER
from app.orchestration.watchdog import scan
from app.orchestration.worker import run_once
from app.services.kernel import approve_plan, commit_plan
from sqlalchemy import func, select
from tests import test_stage2 as support

durable = support.durable
submit = support.submit


def run(durable, **options):
    factory, clock, _, _ = durable
    return dispatch.dispatch_text(
        factory,
        text=options.pop("text", TEXT_BLACK_TO_WHITE),
        client_event_key="sync-key",
        clock=clock,
        now=clock(),
        **options,
    )


def test_identical_retry_uses_one_task_and_plan(durable, caplog):
    first = run(durable)
    replay = run(durable)
    assert first == replay
    assert len([r for r in caplog.records if getattr(r, "event", None) == "dispatch.finish"]) == 2
    assert scan(durable[0], clock=durable[1]) == []
    assert run_once(durable[0], clock=durable[1]) is None
    with durable[0]() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 1
        assert session.scalar(select(func.count()).select_from(Plan)) == 1
        assert session.scalar(select(func.count()).select_from(InputPayload)) == 0


@pytest.mark.parametrize(
    "options", [{"text": TEXT_NEW_ORDER}, {"input_mode": "xlsx"}, {"source_ids": ["other"]}]
)
def test_changed_request_conflicts(durable, options):
    first = run(durable)
    with pytest.raises(AppError) as error:
        run(durable, **options)
    assert error.value.code == "DISPATCH_REQUEST_CONFLICT"
    assert run(durable).task_id == first.task_id


def test_watchdog_cannot_take_live_sync_lease(durable):
    def scan_during_step(ctx):
        assert scan(durable[0], clock=durable[1]) == []

    result = run(durable, before_step=scan_during_step)
    assert result.status == "VALIDATED"


def test_expired_result_cannot_write_after_watchdog(durable):
    def expire(ctx):
        durable[2][0] += timedelta(seconds=61)
        assert scan(durable[0], clock=durable[1]) == [ctx.task.id]
        assert run_once(durable[0], clock=durable[1]) is None

    with pytest.raises(AppError) as error:
        run(durable, before_step=expire)
    assert error.value.code == "STALE_ATTEMPT"
    result = run(durable)
    assert result.status == "FAILED"
    assert result.error_code == "INPUT_UNAVAILABLE"
    with durable[0]() as session:
        assert session.scalar(select(func.count()).select_from(Plan)) == 0


def test_concurrent_retry_does_not_start_second_dispatch(durable):
    entered, release = Signal(), Signal()

    def block(ctx):
        if ctx.spec.agent_id == "parser":
            entered.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, durable, before_step=block)
        assert entered.wait(10)
        try:
            with pytest.raises(AppError) as error:
                run(durable)
            assert error.value.code == "DISPATCH_IN_PROGRESS"
        finally:
            release.set()
        first = future.result()
    assert run(durable) == first


def test_crash_before_plan_is_failed_on_retry_after_lease(durable):
    def crash(ctx):
        raise RuntimeError("synthetic process loss")

    with pytest.raises(RuntimeError):
        run(durable, before_step=crash)
    durable[2][0] += timedelta(seconds=61)
    assert run(durable).error_code == "INPUT_UNAVAILABLE"
    assert run(durable).status == "FAILED"
    assert run_once(durable[0], clock=durable[1]) is None


def test_crash_after_plan_recovers_original_task(durable, monkeypatch):
    def lost_response(*args, **kwargs):
        raise RuntimeError("lost response")

    with monkeypatch.context() as patch:
        patch.setattr(dispatch, "_snapshot", lost_response)
        with pytest.raises(RuntimeError):
            run(durable)
    result = run(durable)
    assert result.status == "VALIDATED"
    with durable[0]() as session:
        assert session.get(Plan, result.plan_id).task_id == result.task_id
        assert session.scalar(select(func.count()).select_from(Task)) == 1


def test_replay_after_commit_reports_current_state(durable):
    result = run(durable)
    approval = approve_plan(
        durable[0],
        plan_id=result.plan_id,
        plan_hash=result.plan_hash,
        expected_intent_revision=result.intent_revision,
        now=durable[1](),
    )
    commit_plan(
        durable[0],
        plan_id=result.plan_id,
        plan_hash=result.plan_hash,
        approval_id=approval.approval_id,
        idempotency_key="commit",
        now=durable[1](),
    )
    replay = run(durable)
    assert replay.task_id == result.task_id
    assert replay.status == "SUCCEEDED"
    assert not replay.requires_human_approval


def test_schema_failure_replay_is_stable(durable):
    first = run(durable, text=TEXT_BAD_SCHEMA)
    assert run(durable, text=TEXT_BAD_SCHEMA) == first


@pytest.mark.parametrize("phase", ["before_plan", "after_plan"])
def test_real_process_exit_recovery(durable, phase):
    script = """
import os, sys
from datetime import datetime
from app.config import Settings
from app.db.session import make_engine, make_session_factory
import app.orchestration.dispatch as dispatch
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
url, phase, now = sys.argv[1:]
def crash(*args, **kwargs):
    os._exit(77)
options = {"before_step": crash} if phase == "before_plan" else {}
if phase == "after_plan":
    dispatch._snapshot = crash
dispatch.dispatch_text(make_session_factory(make_engine(url)), text=TEXT_BLACK_TO_WHITE,
    client_event_key="sync-key", now=datetime.fromisoformat(now),
    settings=Settings(_env_file=None, database_url=url, live_enabled=False), **options)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, durable[3], phase, durable[1]().isoformat()],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == 77, child.stderr.decode()
    durable[2][0] += timedelta(seconds=61)
    recovered = run(durable)
    assert recovered.status == ("FAILED" if phase == "before_plan" else "VALIDATED")
    assert run(durable).task_id == recovered.task_id
    with durable[0]() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 1
        assert session.scalar(select(func.count()).select_from(Plan)) == int(phase == "after_plan")


def test_replay_after_answer_keeps_original_plan(durable):
    from app.orchestration.fixtures import TEXT_ALL_NAVY, TEXT_ONLY_UNCUT
    from app.orchestration.inbox import accept_input

    first = run(durable, text=TEXT_ALL_NAVY)
    assert first.status == "NEEDS_CLARIFICATION"
    accept_input(
        durable[0],
        task_id=first.task_id,
        thread_id="answer",
        client_event_key="answer",
        text=TEXT_ONLY_UNCUT,
        clock=durable[1],
    )
    second = run_once(durable[0], clock=durable[1])
    assert second.status == "AWAITING_APPROVAL"
    replay = run(durable, text=TEXT_ALL_NAVY)
    assert replay.plan_id == first.plan_id != second.plan_id
    assert not replay.requires_human_approval


def test_sync_replay_does_not_fail_expired_durable_answer(durable):
    from app.orchestration.fixtures import TEXT_ATTACH_SHEET
    from app.orchestration.inbox import accept_input
    from app.orchestration.worker import claim

    original = run(durable, text=TEXT_ATTACH_SHEET)
    assert original.plan_id is None
    accept_input(
        durable[0],
        thread_id="answer",
        client_event_key="answer",
        text=TEXT_BLACK_TO_WHITE,
        task_id=original.task_id,
        clock=durable[1],
    )
    assert claim(durable[0], "crashed-worker", clock=durable[1])
    durable[2][0] += timedelta(seconds=61)
    assert run(durable, text=TEXT_ATTACH_SHEET).status == "RUNNING"
    assert scan(durable[0], clock=durable[1]) == [original.task_id]
    assert run_once(durable[0], clock=durable[1]).status == "AWAITING_APPROVAL"


@pytest.mark.parametrize("status", ["RUNNING", "QUEUED"])
def test_legacy_poison_task_does_not_stop_durable_worker(durable, status):
    with durable[0]() as session, begin_immediate(session):
        session.add(Task(id="0-legacy", chain_id="old", status=status, intent_revision=0))
        session.flush()
        session.add(
            JobAttempt(
                id="old-attempt",
                task_id="0-legacy",
                attempt_no=1,
                lease_owner="dispatcher",
                lease_until=None,
                fencing_token=1,
            )
        )
    if status == "RUNNING":
        assert scan(durable[0], clock=durable[1]) == ["0-legacy"]
    submit(durable)
    assert run_once(durable[0], clock=durable[1]).status == "AWAITING_APPROVAL"
    with durable[0]() as session:
        assert session.get(Task, "0-legacy").error_code == "INPUT_UNAVAILABLE"
        assert session.scalar(select(func.count()).select_from(SyncDispatchReceipt)) == 0
