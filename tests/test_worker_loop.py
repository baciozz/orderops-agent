"""Bounded, operator-started fixture scheduling, using only synthetic data and clocks."""

import logging
import signal
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from unittest.mock import Mock

import pytest
from app import cli
from app.config import Settings
from app.db.models import Approval, ConsumerReceipt, ModelUsage, Operation, SalesOrder, Task
from app.orchestration import outbox, watchdog, worker, worker_loop
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from app.orchestration.inbox import accept_input
from app.orchestration.state import aware
from app.orchestration.worker_cycle import CycleResult
from app.orchestration.worker_loop import (
    LoopOptions,
    LoopResult,
    StopFlag,
    run_loop,
    shutdown_signals,
)
from app.services import kernel
from app.services.seed import load_seed
from sqlalchemy import func, select
from tests.test_qi_verification_retry import _approved_request, _pending_commit
from tests.test_verification_backoff import add_operation, read

NOW = datetime(2026, 11, 6, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = NOW
        self.waits = []

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "interval", [0, -1, 0.099, 3601, float("inf"), float("nan"), 10**1000, True, "5"]
)
def test_interval_rejects_invalid_values(interval):
    with pytest.raises(ValueError, match="interval_seconds"):
        LoopOptions(interval_seconds=interval)


@pytest.mark.parametrize("iterations", [0, -1, 10001, 1.5, True, "2"])
def test_iterations_rejects_invalid_values(iterations):
    with pytest.raises(ValueError, match="max_iterations"):
        LoopOptions(max_iterations=iterations)


@pytest.mark.parametrize("interval,iterations", [(0.1, 1), (3600, 10000)])
def test_options_accept_bounds(interval, iterations):
    assert LoopOptions(interval, iterations).max_iterations == iterations


@pytest.mark.parametrize(
    "args",
    [
        ["--interval-seconds", "0"],
        ["--interval-seconds", "nan"],
        ["--interval-seconds", "inf"],
        ["--interval-seconds", "3601"],
        ["--max-iterations", "0"],
        ["--max-iterations", "10001"],
        ["--max-iterations", "1.5"],
    ],
)
def test_invalid_cli_options_fail_before_settings_or_database(monkeypatch, args):
    forbidden = Mock(side_effect=AssertionError("Must not initialize invalid CLI request"))
    monkeypatch.setattr(cli, "get_settings", forbidden)
    monkeypatch.setattr(cli, "make_engine", forbidden)
    with pytest.raises(SystemExit) as exc:
        cli.main(["worker-loop", *args])
    assert exc.value.code == 2
    forbidden.assert_not_called()


@pytest.mark.parametrize("busy", [False, True])
def test_serial_cycles_always_wait_and_pass_same_clock(monkeypatch, caplog, busy):
    calls = []
    clock = Clock()
    settings = Settings(_env_file=None, live_enabled=True, worker_max_active=1)
    caplog.set_level(logging.INFO, logger="oos")

    def scan(factory, **kwargs):
        assert kwargs["clock"] is clock
        assert kwargs["settings"] is not settings and not kwargs["settings"].live_enabled
        calls.append("scan")
        return []

    def run_once(factory, **kwargs):
        assert kwargs["clock"] is clock
        assert kwargs["limits"].active == 1
        assert not kwargs["settings"].live_enabled
        calls.append("worker")
        return object() if busy else None

    def publish(factory, **kwargs):
        assert kwargs["clock"] is clock
        calls.append("outbox")
        return 0

    def wait(seconds):
        calls.append("wait")
        clock.wait(seconds)

    monkeypatch.setattr(watchdog, "scan", scan)
    monkeypatch.setattr(worker, "run_once", run_once)
    monkeypatch.setattr(outbox, "publish_pending", publish)
    result = run_loop(None, settings, options=LoopOptions(2, 3), clock=clock, wait=wait)
    assert result == LoopResult(3, "max_iterations")
    assert calls == ["scan", "worker", "outbox", "wait"] * 2 + ["scan", "worker", "outbox"]
    assert clock.waits == [2, 2]
    assert settings.live_enabled
    waits = [record for record in caplog.records if record.event == "worker_loop.wait"]
    assert len(waits) == 2 and all(record.task_claimed == busy for record in waits)


def test_pre_stopped_loop_does_no_work_or_wait(monkeypatch):
    stop = Event()
    stop.set()
    cycle, wait = Mock(), Mock()
    monkeypatch.setattr(worker_loop, "run_cycle", cycle)
    assert run_loop(None, Settings(_env_file=None), stop=stop, wait=wait) == LoopResult(
        0, "stop_requested"
    )
    cycle.assert_not_called()
    wait.assert_not_called()


@pytest.mark.parametrize("stage", ["scan", "worker", "outbox"])
def test_stop_during_cycle_finishes_all_stages_without_another_wait(monkeypatch, stage):
    stop, wait, calls = Event(), Mock(), []

    def step(name, result):
        def call(*args, **kwargs):
            calls.append(name)
            if name == stage:
                stop.set()
            return result

        return call

    monkeypatch.setattr(watchdog, "scan", step("scan", []))
    monkeypatch.setattr(worker, "run_once", step("worker", None))
    monkeypatch.setattr(outbox, "publish_pending", step("outbox", 0))
    assert run_loop(None, Settings(_env_file=None), stop=stop, wait=wait) == LoopResult(
        1, "stop_requested"
    )
    assert calls == ["scan", "worker", "outbox"]
    wait.assert_not_called()


def test_default_wait_uses_stop_event_and_does_not_start_next_cycle(monkeypatch):
    stop = Event()
    cycle = Mock(return_value=CycleResult([], None, 0))
    wait = Mock(side_effect=lambda seconds: stop.set())
    monkeypatch.setattr(worker_loop, "run_cycle", cycle)
    monkeypatch.setattr(stop, "wait", wait)
    assert run_loop(None, Settings(_env_file=None), stop=stop) == LoopResult(1, "stop_requested")
    cycle.assert_called_once()
    wait.assert_called_once_with(5.0)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("failed", [False, True])
def test_signal_handlers_only_request_stop_and_are_restored(signum, failed):
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    stop = StopFlag()
    try:
        with shutdown_signals(stop):
            handler = signal.getsignal(signum)
            handler(signum, None)
            assert stop.is_set()
            if failed:
                raise RuntimeError("synthetic failure")
    except RuntimeError:
        assert failed
    assert {sig: signal.getsignal(sig) for sig in previous} == previous


def test_partial_signal_install_restores_already_installed_handler(monkeypatch):
    original = signal.getsignal(signal.SIGINT)
    real_signal = signal.signal

    def install(signum, handler):
        if signum == signal.SIGTERM:
            raise ValueError("synthetic signal registration failure")
        return real_signal(signum, handler)

    monkeypatch.setattr(signal, "signal", install)
    with pytest.raises(ValueError):
        with shutdown_signals(StopFlag()):
            pytest.fail("Partial signal registration must not run loop")
    assert signal.getsignal(signal.SIGINT) is original


@pytest.mark.parametrize("stage", ["scan", "worker", "outbox", "wait"])
def test_unhandled_failures_stop_immediately_without_logging_payload(monkeypatch, caplog, stage):
    calls = []
    caplog.set_level(logging.INFO, logger="oos")

    def step(name, result):
        def call(*args, **kwargs):
            calls.append(name)
            if name == stage:
                raise RuntimeError("PRIVATE_CUSTOMER_TEXT")
            return result

        return call

    monkeypatch.setattr(watchdog, "scan", step("scan", []))
    monkeypatch.setattr(worker, "run_once", step("worker", None))
    monkeypatch.setattr(outbox, "publish_pending", step("outbox", 0))
    with pytest.raises(RuntimeError):
        run_loop(None, Settings(_env_file=None), wait=step("wait", None))
    order = ["scan", "worker", "outbox", "wait"]
    assert calls == order[: order.index(stage) + 1]
    assert "PRIVATE_CUSTOMER_TEXT" not in caplog.text
    failure = [record for record in caplog.records if record.event == "worker_loop.failed"]
    assert len(failure) == 1 and failure[0].error_code == "INTERNAL_ERROR"
    assert not any(record.event == "worker_loop.stopped" for record in caplog.records)


@pytest.mark.parametrize("failed", [False, True])
def test_cli_restores_signals_disposes_engine_and_returns_exit_code(
    monkeypatch, engine, database_url, capsys, failed
):
    monkeypatch.setenv("OOS_DATABASE_URL", database_url)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    dispose = Mock(wraps=engine.dispose)
    monkeypatch.setattr(engine, "dispose", dispose)
    monkeypatch.setattr(cli, "make_engine", lambda url: engine)

    def run(factory, settings, *, options, stop):
        assert options == LoopOptions()
        assert settings.database_url == database_url
        if failed:
            raise RuntimeError("PRIVATE_CLI_PAYLOAD")
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert stop.is_set()
        return LoopResult(1, "stop_requested")

    monkeypatch.setattr(cli, "run_loop", run)
    assert cli.main(["worker-loop"]) == int(failed)
    dispose.assert_called_once()
    assert {sig: signal.getsignal(sig) for sig in previous} == previous
    output = capsys.readouterr()
    assert "PRIVATE_CLI_PAYLOAD" not in output.out + output.err
    assert "FIXTURE worker-loop" in output.out


@pytest.mark.parametrize("stage", ["get_settings", "make_engine", "init_db", "dispose"])
def test_cli_startup_and_cleanup_errors_are_sanitized(
    monkeypatch, engine, database_url, capsys, stage
):
    monkeypatch.setenv("OOS_DATABASE_URL", database_url)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    monkeypatch.setattr(cli, "make_engine", lambda url: engine)
    monkeypatch.setattr(cli, "run_loop", Mock(return_value=LoopResult(1, "max_iterations")))
    failure = Mock(side_effect=RuntimeError("PRIVATE_SETUP_PAYLOAD"))
    if stage == "dispose":
        monkeypatch.setattr(engine, "dispose", failure)
    else:
        monkeypatch.setattr(cli, stage, failure)
    assert cli.main(["worker-loop", "--max-iterations", "1"]) == 1
    assert {sig: signal.getsignal(sig) for sig in previous} == previous
    output = capsys.readouterr()
    assert "PRIVATE_SETUP_PAYLOAD" not in output.out + output.err
    assert "异常退出" in output.out
    monkeypatch.undo()  # Restore engine disposal before its fixture teardown.


def test_live_settings_cannot_escape_fixture_or_approve_commit(
    session_factory, database_url, monkeypatch
):
    load_seed(session_factory, database_url=database_url)
    task_id = accept_input(
        session_factory, thread_id="loop", client_event_key="input", text=TEXT_BLACK_TO_WHITE
    )[1]
    settings = Settings(
        _env_file=None,
        live_enabled=True,
        llm_provider="openai_compatible",
        llm_model="synthetic-no-call",
        llm_base_url="http://127.0.0.1:9999/v1",
        price_in_per_mtok=Decimal(0),
        price_out_per_mtok=Decimal(0),
    )
    before = settings.model_dump()
    forbidden = Mock(side_effect=AssertionError("Must not enter live or reload global settings"))
    monkeypatch.setattr("app.orchestration.dispatch.get_settings", forbidden)
    monkeypatch.setattr("app.orchestration.dispatch.LiveLLMClient", forbidden)
    clock = Clock()
    result = run_loop(
        session_factory, settings, options=LoopOptions(1, 3), clock=clock, wait=clock.wait
    )
    assert result.iterations == 3 and clock.waits == [1, 1]
    assert settings.model_dump() == before
    forbidden.assert_not_called()
    with session_factory() as session:
        assert session.get(Task, task_id).status == "AWAITING_APPROVAL"
        assert session.get(SalesOrder, "SO-1001").current_revision == 1
        for model in (Approval, Operation, ModelUsage):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_loop_advances_expired_lease_without_busy_spin(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    clock = Clock()
    task_id = accept_input(
        session_factory,
        thread_id="loop",
        client_event_key="lease",
        text=TEXT_BLACK_TO_WHITE,
        clock=clock,
    )[1]
    assert worker.claim(session_factory, "crashed-worker", clock=clock)[0] == task_id
    result = run_loop(
        session_factory,
        Settings(_env_file=None),
        options=LoopOptions(30, 3),
        clock=clock,
        wait=clock.wait,
    )
    assert result.iterations == 3 and clock.waits == [30, 30]
    with session_factory() as session:
        assert session.get(Task, task_id).status == "AWAITING_APPROVAL"
        assert session.scalar(select(func.count()).select_from(Operation)) == 0


def test_loop_advances_retry_schedule_and_stops_at_manual_limit(session_factory, monkeypatch):
    add_operation(session_factory)
    clock, attempts = Clock(), []
    monkeypatch.setattr(kernel, "_post_verify", lambda *_: attempts.append(clock.now))
    settings = Settings(
        _env_file=None,
        verification_max_retries=3,
        verification_base_delay_seconds=2,
        verification_max_delay_seconds=4,
    )
    result = run_loop(
        session_factory, settings, options=LoopOptions(1, 8), clock=clock, wait=clock.wait
    )
    assert result.iterations == 8 and clock.waits == [1] * 7
    assert attempts == [NOW, NOW + timedelta(seconds=2), NOW + timedelta(seconds=6)]
    operation = read(session_factory)
    assert operation.verification_retry_attempts == 3 and operation.verification_manual_required
    assert operation.status == "COMMITTED" and operation.committed_verification_pending
    assert operation.result_json == {"receipt": "unchanged"}


def test_loop_readback_after_synthetic_commit_never_recommits(
    session_factory, database_url, monkeypatch
):
    # Only test setup explicitly approves/commits synthetic data. The loop may only read it back.
    load_seed(session_factory, database_url=database_url)
    original = _pending_commit(
        session_factory, _approved_request(session_factory), monkeypatch, "crash"
    )
    forbidden = Mock(side_effect=AssertionError("Loop must not commit or approve"))
    monkeypatch.setattr(kernel, "commit_plan", forbidden)
    monkeypatch.setattr(kernel, "approve_plan", forbidden)
    clock = Clock()
    sends = []
    real_publish = outbox.publish_pending

    def send(event_id, consumer):
        sends.append(clock.now)
        if len(sends) == 1:
            raise RuntimeError("PRIVATE_DELIVERY_PAYLOAD")
        outbox.consume(session_factory, event_id, consumer, clock=clock)

    monkeypatch.setattr(
        outbox, "publish_pending", lambda factory, **kw: real_publish(factory, send=send, **kw)
    )
    run_loop(
        session_factory,
        Settings(_env_file=None),
        options=LoopOptions(1, 3),
        clock=clock,
        wait=clock.wait,
    )
    forbidden.assert_not_called()
    assert sends == [NOW, NOW + timedelta(seconds=2)]
    assert clock.waits == [1, 1]
    with session_factory() as session:
        operation = session.get(Operation, original.operation_id)
        assert operation.status == "COMMITTED" and not operation.committed_verification_pending
        assert operation.verification_retry_attempts == 1
        assert operation.verification_next_attempt_at is None
        assert session.get(SalesOrder, "SO-1001").current_revision == original.revision == 2
        assert session.get(Task, original.task_id).status == "SUCCEEDED"
        assert session.scalar(select(func.count()).select_from(Operation)) == 1
        receipts = session.scalars(select(ConsumerReceipt)).all()
        assert len(receipts) == 1
        assert aware(receipts[0].received_at) == NOW + timedelta(seconds=2)


def test_signal_stop_waits_using_positive_monotonic_slices_without_busy_spin():
    now, waits = [0.0], []

    def sleep_for(seconds):
        assert 0 < seconds <= 0.1
        waits.append(seconds)
        now[0] += seconds

    stop = StopFlag(monotonic_clock=lambda: now[0], sleep_for=sleep_for)
    assert stop.wait(0.25) is False
    assert waits == pytest.approx([0.1, 0.1, 0.05])
    assert now[0] == pytest.approx(0.25)
    stop.set()
    assert stop.wait(3600) is True
    assert len(waits) == 3


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_real_signal_stops_sliced_wait_without_event_lock(monkeypatch, signum):
    # Hold the very lock Event.set would deadlock on; StopFlag must not consult it.
    event = Event()
    clock, waits = [0.0], []

    def forbidden_set(self):
        pytest.fail("Signal path must not use Event.set or threading locks")

    def sleep_for(seconds):
        waits.append(seconds)
        signal.raise_signal(signum)
        clock[0] += seconds

    monkeypatch.setattr(Event, "set", forbidden_set)
    stop = StopFlag(monotonic_clock=lambda: clock[0], sleep_for=sleep_for)
    with event._cond, shutdown_signals(stop):
        assert stop.wait(3600) is True
    assert waits == [0.1] and stop.is_set()


def test_default_stop_flag_wait_is_injectable(monkeypatch):
    now, waits = [0.0], []

    def sleep_for(seconds):
        waits.append(seconds)
        now[0] += seconds

    stop = StopFlag(monotonic_clock=lambda: now[0], sleep_for=sleep_for)
    monkeypatch.setattr(worker_loop, "StopFlag", lambda: stop)
    monkeypatch.setattr(worker_loop, "run_cycle", Mock(return_value=CycleResult([], None, 0)))
    assert run_loop(None, Settings(_env_file=None), options=LoopOptions(0.25, 2)) == LoopResult(
        2, "max_iterations"
    )
    assert waits == pytest.approx([0.1, 0.1, 0.05])
