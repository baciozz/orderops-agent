"""Persistent bounded readback retries cannot reapply business operations."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from alembic import command
from app.cli import main
from app.config import Settings
from app.db.models import Operation
from app.db.schema import _migration_config, init_db
from app.db.session import make_engine
from app.errors import AppError
from app.orchestration.state import aware
from app.services import kernel, verification
from pydantic import ValidationError
from sqlalchemy import text

NOW = datetime(2026, 11, 6, tzinfo=UTC)


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def add_operation(factory, name="op"):
    with factory() as session:
        session.add(
            Operation(
                id=name,
                idempotency_key=name,
                request_hash="hash",
                status="COMMITTED",
                result_json={"receipt": "unchanged"},
                committed_verification_pending=True,
            )
        )
        session.commit()
    return name


def read(factory, name="op"):
    with factory() as session:
        operation = session.get(Operation, name)
        session.expunge(operation)
        return operation


def test_backoff_is_persistent_exponential_and_capped(session_factory, monkeypatch):
    add_operation(session_factory)
    attempt = Mock()
    monkeypatch.setattr(kernel, "_post_verify", attempt)
    config = settings(
        verification_max_retries=4,
        verification_base_delay_seconds=10,
        verification_max_delay_seconds=15,
    )
    moment = NOW
    for retry, delay in [(1, 10), (2, 15), (3, 15), (4, None)]:
        assert verification.retry_pending(session_factory, now=moment, settings=config) == 1
        op = read(session_factory)
        assert op.verification_retry_attempts == retry
        assert op.committed_verification_pending and op.status == "COMMITTED"
        assert op.result_json == {"receipt": "unchanged"}
        assert op.verification_lease_until is None and op.verification_lease_token is None
        if delay is None:
            assert op.verification_manual_required and op.verification_next_attempt_at is None
        else:
            due = moment + timedelta(seconds=delay)
            assert aware(op.verification_next_attempt_at) == due
            # Every caller, including one targeting this operation, observes the persisted schedule.
            assert (
                verification.retry_pending(
                    session_factory, "op", now=due - timedelta(microseconds=1), settings=config
                )
                == 0
            )
            moment = due
    assert attempt.call_count == 4
    assert (
        verification.retry_pending(session_factory, now=NOW + timedelta(days=365), settings=config)
        == 0
    )
    assert attempt.call_count == 4


def test_readback_exceptions_consume_budget_without_exposing_messages(session_factory, monkeypatch):
    add_operation(session_factory)
    monkeypatch.setattr(kernel, "_post_verify", Mock(side_effect=RuntimeError("PRIVATE_READBACK")))
    assert (
        verification.retry_pending(
            session_factory, now=NOW, settings=settings(verification_max_retries=1)
        )
        == 1
    )
    op = read(session_factory)
    assert op.verification_manual_required and op.verification_retry_attempts == 1
    assert op.verification_last_error_code and "PRIVATE" not in op.verification_last_error_code


def test_zero_retries_escalates_without_readback(session_factory, monkeypatch):
    add_operation(session_factory)
    attempt = Mock()
    monkeypatch.setattr(kernel, "_post_verify", attempt)
    assert (
        verification.retry_pending(
            session_factory, now=NOW, settings=settings(verification_max_retries=0)
        )
        == 0
    )
    assert read(session_factory).verification_manual_required
    attempt.assert_not_called()


def test_success_clears_schedule_and_is_sticky(session_factory):
    add_operation(session_factory)
    assert verification.retry_pending(session_factory, now=NOW) == 1
    op = read(session_factory)
    assert not op.committed_verification_pending
    assert op.verification_next_attempt_at is None and not op.verification_manual_required
    assert op.verification_last_error_code is None
    assert verification.retry_pending(session_factory, now=NOW + timedelta(days=1)) == 0


def test_recursive_concurrent_caller_cannot_claim_same_lease(session_factory, monkeypatch):
    add_operation(session_factory)
    config = settings()

    def concurrent(*args, **kwargs):
        assert verification.retry_pending(session_factory, now=NOW, settings=config) == 0

    attempt = Mock(side_effect=concurrent)
    monkeypatch.setattr(kernel, "_post_verify", attempt)
    assert verification.retry_pending(session_factory, now=NOW, settings=config) == 1
    assert attempt.call_count == 1 and read(session_factory).verification_retry_attempts == 1


def test_crashed_claim_expires_and_exhausted_claim_requires_human(session_factory, monkeypatch):
    add_operation(session_factory)
    config = settings(verification_max_retries=2, verification_lease_seconds=10)
    assert verification._claim(session_factory, "op", NOW, config)
    assert verification.retry_pending(session_factory, now=NOW, settings=config) == 0
    assert verification._claim(session_factory, "op", NOW + timedelta(seconds=10), config)
    assert (
        verification.retry_pending(
            session_factory, now=NOW + timedelta(seconds=19), settings=config
        )
        == 0
    )
    attempt = Mock()
    monkeypatch.setattr(kernel, "_post_verify", attempt)
    assert (
        verification.retry_pending(
            session_factory, now=NOW + timedelta(seconds=20), settings=config
        )
        == 0
    )
    assert read(session_factory).verification_manual_required
    attempt.assert_not_called()


def test_stale_attempt_cannot_clear_newer_lease_or_schedule(session_factory, monkeypatch):
    add_operation(session_factory)
    config = settings(verification_lease_seconds=10)
    first = verification._claim(session_factory, "op", NOW, config)
    later = NOW + timedelta(seconds=10)
    second = verification._claim(session_factory, "op", later, config)
    monkeypatch.setattr(kernel, "_post_verify", Mock())
    verification._attempt(session_factory, "op", first, NOW, config)
    op = read(session_factory)
    assert op.verification_lease_token == second
    assert aware(op.verification_lease_until) == later + timedelta(seconds=10)
    assert op.verification_next_attempt_at is None and op.verification_retry_attempts == 2


def test_batch_bound_does_not_starve_other_pending_operations(session_factory, monkeypatch):
    for name in ["a", "b", "c"]:
        add_operation(session_factory, name)
    monkeypatch.setattr(kernel, "_post_verify", Mock())
    config = settings(verification_batch_size=1)
    assert [
        verification.retry_pending(session_factory, now=NOW, settings=config) for _ in range(4)
    ] == [1, 1, 1, 0]
    assert [read(session_factory, n).verification_retry_attempts for n in ["a", "b", "c"]] == [
        1,
        1,
        1,
    ]


def test_manual_retry_does_not_reset_automatic_budget(session_factory, monkeypatch):
    add_operation(session_factory)
    config = settings(verification_max_retries=0)
    verification.retry_pending(session_factory, now=NOW, settings=config)
    with monkeypatch.context() as patch:
        patch.setattr(kernel, "_post_verify", Mock())
        assert (
            verification.verify_manually(session_factory, "op", now=NOW, settings=config) is False
        )
    op = read(session_factory)
    assert op.verification_manual_required and op.committed_verification_pending
    assert op.verification_retry_attempts == 0
    assert verification.retry_pending(session_factory, now=NOW, settings=config) == 0
    assert verification.verify_manually(session_factory, "op", now=NOW, settings=config) is True
    assert not read(session_factory).verification_manual_required
    assert verification.verify_manually(session_factory, "op", now=NOW, settings=config) is True


def test_manual_path_rejects_automatic_inflight_and_missing(session_factory):
    add_operation(session_factory)
    with pytest.raises(AppError) as error:
        verification.verify_manually(session_factory, "op", now=NOW)
    assert error.value.code == "VERIFICATION_NOT_READY"
    with pytest.raises(AppError) as error:
        verification.verify_manually(session_factory, "missing", now=NOW)
    assert error.value.code == "NOT_FOUND"
    config = settings(verification_max_retries=0)
    verification.retry_pending(session_factory, now=NOW, settings=config)
    verification._claim(session_factory, "op", NOW, config, manual=True)
    with pytest.raises(AppError) as error:
        verification.verify_manually(session_factory, "op", now=NOW)
    assert error.value.code == "VERIFICATION_NOT_READY"


@pytest.mark.parametrize(
    "field,value",
    [
        ("verification_max_retries", -1),
        ("verification_max_retries", 101),
        ("verification_base_delay_seconds", 0),
        ("verification_max_delay_seconds", 0),
        ("verification_lease_seconds", 0),
        ("verification_batch_size", 0),
    ],
)
def test_invalid_retry_configuration_is_rejected(field, value):
    with pytest.raises(ValidationError):
        settings(**{field: value})


def test_migration_preserves_historical_operation_and_downgrades(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    cfg = _migration_config()
    try:
        with engine.connect() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "budget_price_snapshot")
            connection.execute(
                text(
                    "INSERT INTO operation (id,idempotency_key,request_hash,status,"
                    "result_json,committed_verification_pending) "
                    "VALUES ('old','old','hash','COMMITTED','{}',1)"
                )
            )
            connection.commit()
            command.upgrade(cfg, "head")
            op = connection.execute(text("SELECT * FROM operation WHERE id='old'")).mappings().one()
            assert op["committed_verification_pending"] == 1 and op["result_json"] == "{}"
            assert (
                op["verification_retry_attempts"] == 0 and op["verification_manual_required"] == 0
            )
            assert op["verification_next_attempt_at"] is None
            command.downgrade(cfg, "budget_price_snapshot")
            assert (
                connection.execute(text("SELECT status FROM operation WHERE id='old'")).scalar()
                == "COMMITTED"
            )
        init_db(engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "state,expected", [("passed", 0), ("pending", 1), ("not_ready", 2), ("missing", 2)]
)
def test_manual_cli_returns_honest_outcome(
    session_factory, database_url, monkeypatch, capsys, state, expected
):
    monkeypatch.setenv("OOS_DATABASE_URL", database_url)
    if state != "missing":
        add_operation(session_factory)
        if state != "not_ready":
            verification.retry_pending(
                session_factory, now=NOW, settings=settings(verification_max_retries=0)
            )
    if state == "pending":
        monkeypatch.setattr(kernel, "_post_verify", Mock())
    assert main(["verify-operation", "op"]) == expected
    output = capsys.readouterr().out
    assert "核验" in output


def test_atomic_claim_across_connections(session_factory):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    add_operation(session_factory)
    barrier = Barrier(2)

    def claim(_):
        barrier.wait(timeout=5)
        return verification._claim(session_factory, "op", NOW, settings())

    with ThreadPoolExecutor(max_workers=2) as workers:
        tokens = list(workers.map(claim, range(2)))
    assert sum(token is not None for token in tokens) == 1
    assert read(session_factory).verification_retry_attempts == 1


def test_manual_escalation_metric_counts_transition_not_repeated_scans(session_factory):
    from app.metrics import snapshot

    add_operation(session_factory)
    config = settings(verification_max_retries=0)
    assert verification.retry_pending(session_factory, now=NOW, settings=config) == 0
    assert verification.retry_pending(session_factory, now=NOW, settings=config) == 0
    assert snapshot()["verification_manual_required"] == 1
    assert snapshot()["verification_pending_retries"] == 0


def test_slow_batch_uses_fresh_claim_time_and_backoff_from_completion(session_factory, monkeypatch):
    add_operation(session_factory, "a")
    add_operation(session_factory, "b")
    config = settings(verification_lease_seconds=60)
    clock = [NOW]

    def slow_readback(factory, operation_id, **kwargs):
        lease = read(factory, operation_id).verification_lease_until
        assert aware(lease) == clock[0] + timedelta(seconds=60)
        clock[0] += timedelta(seconds=20)

    monkeypatch.setattr(kernel, "_post_verify", slow_readback)
    assert verification.retry_pending(session_factory, settings=config, clock=lambda: clock[0]) == 2
    assert aware(read(session_factory, "a").verification_next_attempt_at) == NOW + timedelta(
        seconds=50
    )
    assert aware(read(session_factory, "b").verification_next_attempt_at) == NOW + timedelta(
        seconds=70
    )


def test_claim_takes_fresh_clock_after_acquiring_write_lock(session_factory, monkeypatch):
    from contextlib import contextmanager

    add_operation(session_factory)
    original = verification.begin_immediate
    current = [NOW]

    @contextmanager
    def delayed_lock(session):
        with original(session):
            current[0] += timedelta(seconds=20)
            yield

    monkeypatch.setattr(verification, "begin_immediate", delayed_lock)
    assert verification._claim(
        session_factory,
        "op",
        NOW,
        settings(verification_lease_seconds=10),
        clock=lambda: current[0],
    )
    assert aware(read(session_factory).verification_lease_until) == NOW + timedelta(seconds=30)


def test_crash_after_success_cannot_leave_stale_retry_metadata(session_factory, monkeypatch):
    class ProcessExit(BaseException):
        pass

    add_operation(session_factory)
    config = settings(verification_max_retries=0)
    verification.retry_pending(session_factory, now=NOW, settings=config)
    original = kernel._post_verify

    def crash_after_pass(factory, operation_id, **kwargs):
        original(factory, operation_id, **kwargs)
        raise ProcessExit()

    monkeypatch.setattr(kernel, "_post_verify", crash_after_pass)
    with pytest.raises(ProcessExit):
        verification.verify_manually(session_factory, "op", now=NOW, settings=config)
    op = read(session_factory)
    assert not op.committed_verification_pending and not op.verification_manual_required
    assert op.verification_lease_token is None and op.verification_lease_until is None
    assert op.verification_next_attempt_at is None and op.verification_last_error_code is None
