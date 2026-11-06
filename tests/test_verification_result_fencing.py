"""Synthetic readback races: stale success must never erase newer failure."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, get_ident

import pytest
from app.config import Settings
from app.db.models import Operation, OrderLine, OrderRevision, SalesOrder, StockBalance
from app.db.session import begin_immediate, make_engine, make_session_factory
from app.orchestration.state import aware
from app.services import kernel as service
from app.services import verification
from sqlalchemy import event, select
from tests import test_commit_verification_pending as support

kernel = support.kernel
approved_commit = support.approved_commit
NOW = datetime(2026, 11, 6, tzinfo=UTC)


def pending(factory, request):
    marker = service.verify_failpoint.set(True)
    try:
        return service.commit_plan(factory, **request).operation_id
    finally:
        service.verify_failpoint.reset(marker)


def operation(factory, operation_id):
    with factory() as session:
        row = session.get(Operation, operation_id)
        return {column.name: getattr(row, column.name) for column in Operation.__table__.columns}


def change_balance(factory, delta):
    with factory() as session, begin_immediate(session):
        session.get(StockBalance, ("W1", "A-WHITE-M")).on_hand += delta


def pause_after_success(monkeypatch):
    arrived, release = Event(), Event()
    thread = []
    original = service.downstream_problems

    def readback(session):
        result = original(session)
        if thread and get_ident() == thread[0]:
            assert result == []
            arrived.set()
            assert release.wait(15)
        return result

    monkeypatch.setattr(service, "downstream_problems", readback)
    return arrived, release, thread


@pytest.mark.parametrize(
    ("kind", "exhausted"),
    [("initial", False), ("initial", True), ("retry", False), ("retry", True), ("manual", True)],
)
def test_old_success_cannot_clear_new_owner_failure(
    kernel, approved_commit, monkeypatch, kind, exhausted
):
    operation_id = pending(kernel, approved_commit[1])
    config = Settings(
        _env_file=None,
        verification_lease_seconds=10,
        verification_max_retries=(1 if kind == "initial" else 2) if exhausted else 4,
    )
    if kind == "manual":
        config = config.model_copy(update={"verification_max_retries": 0})
        assert verification.retry_pending(kernel, operation_id, now=NOW, settings=config) == 0
    arrived, release, thread = pause_after_success(monkeypatch)
    current = [NOW]

    def old():
        thread.append(get_ident())
        if kind == "initial":
            service._post_verify(kernel, operation_id)
        elif kind == "manual":
            assert not verification.verify_manually(
                kernel, operation_id, settings=config, clock=lambda: current[0]
            )
        else:
            verification.retry_pending(
                kernel, operation_id, settings=config, clock=lambda: current[0]
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(old)
        try:
            assert arrived.wait(10)
            change_balance(kernel, 1)
            current[0] += timedelta(seconds=11)
            if kind == "manual":
                assert not verification.verify_manually(
                    kernel, operation_id, settings=config, clock=lambda: current[0]
                )
            else:
                assert (
                    verification.retry_pending(
                        kernel, operation_id, settings=config, clock=lambda: current[0]
                    )
                    == 1
                )
            newer = operation(kernel, operation_id)
            assert newer["committed_verification_pending"]
            assert newer["verification_manual_required"] == exhausted
        finally:
            release.set()
        future.result(timeout=10)
    assert operation(kernel, operation_id) == newer
    # Repair only the injected fault; the current owner can then recover.
    change_balance(kernel, -1)
    if exhausted:
        assert verification.verify_manually(kernel, operation_id, settings=config, now=current[0])
    else:
        due = aware(newer["verification_next_attempt_at"])
        assert verification.retry_pending(kernel, operation_id, settings=config, now=due) == 1
    assert not operation(kernel, operation_id)["committed_verification_pending"]


@pytest.mark.parametrize("initial", [False, True])
def test_same_owner_cannot_publish_snapshot_before_business_change(
    kernel, approved_commit, monkeypatch, database_url, initial
):
    operation_id = pending(kernel, approved_commit[1])
    arrived, release, thread = pause_after_success(monkeypatch)

    def check():
        thread.append(get_ident())
        if initial:
            return service._post_verify(kernel, operation_id)
        return verification.retry_pending(kernel, operation_id, now=NOW)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(check)
        try:
            assert arrived.wait(10)
            # An independent engine models a write from another worker/process.
            engine = make_engine(database_url)
            try:
                change_balance(make_session_factory(engine), 1)
            finally:
                engine.dispose()
        finally:
            release.set()
        assert future.result(timeout=10) == (None if initial else 1)
    state = operation(kernel, operation_id)
    assert state["committed_verification_pending"]
    change_balance(kernel, -1)
    due = aware(state["verification_next_attempt_at"]) if not initial else NOW
    assert verification.retry_pending(kernel, operation_id, now=due) == 1
    assert not operation(kernel, operation_id)["committed_verification_pending"]


@pytest.mark.parametrize("manual", [False, True])
def test_expired_owner_cannot_publish_or_reschedule_without_takeover(
    kernel, approved_commit, monkeypatch, manual
):
    operation_id = pending(kernel, approved_commit[1])
    config = Settings(
        _env_file=None, verification_lease_seconds=10, verification_max_retries=0 if manual else 3
    )
    if manual:
        verification.retry_pending(kernel, operation_id, settings=config, now=NOW)
    arrived, release, thread = pause_after_success(monkeypatch)
    current = [NOW]
    run = verification.verify_manually if manual else verification.retry_pending

    def old():
        thread.append(get_ident())
        return run(kernel, operation_id, settings=config, clock=lambda: current[0])

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(old)
        try:
            assert arrived.wait(10)
            claimed = operation(kernel, operation_id)
            current[0] += timedelta(seconds=10)  # Exact expiry is not ownership.
        finally:
            release.set()
        assert future.result(timeout=10) == (False if manual else 1)
    assert operation(kernel, operation_id) == claimed
    assert run(kernel, operation_id, settings=config, now=current[0])
    assert not operation(kernel, operation_id)["committed_verification_pending"]


def test_result_checks_clock_after_actual_write_lock(kernel, approved_commit):
    operation_id = pending(kernel, approved_commit[1])
    config = Settings(_env_file=None, verification_lease_seconds=10)
    token = verification._claim(kernel, operation_id, NOW, config)
    before = operation(kernel, operation_id)
    current = [NOW]
    with kernel() as session:
        engine = session.get_bind()

    def lock_wait(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            current[0] += timedelta(seconds=10)

    event.listen(engine, "before_cursor_execute", lock_wait)
    try:
        verification._attempt(kernel, operation_id, token, NOW, config, clock=lambda: current[0])
    finally:
        event.remove(engine, "before_cursor_execute", lock_wait)
    assert operation(kernel, operation_id) == before
    assert verification.retry_pending(kernel, operation_id, settings=config, now=current[0]) == 1
    assert not operation(kernel, operation_id)["committed_verification_pending"]


def test_claim_clock_is_after_sql_begin(kernel, approved_commit):
    operation_id = pending(kernel, approved_commit[1])
    config = Settings(_env_file=None, verification_lease_seconds=10)
    current = [NOW]
    with kernel() as session:
        engine = session.get_bind()

    def lock_wait(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            current[0] += timedelta(seconds=20)

    event.listen(engine, "before_cursor_execute", lock_wait)
    try:
        assert verification._claim(kernel, operation_id, NOW, config, clock=lambda: current[0])
    finally:
        event.remove(engine, "before_cursor_execute", lock_wait)
    state = operation(kernel, operation_id)
    assert aware(state["verification_lease_until"]) == NOW + timedelta(seconds=30)


def test_missing_fully_shipped_line_keeps_verification_pending(kernel, approved_commit):
    operation_id = pending(kernel, approved_commit[1])
    with kernel() as session, begin_immediate(session):
        revision = session.scalar(
            select(OrderRevision).where(
                OrderRevision.order_id == "SO-1001", OrderRevision.revision_no == 2
            )
        )
        line = session.get(OrderLine, (revision.id, "A-BLACK-M"))
        saved = dict(
            revision_id=line.revision_id,
            sku_id=line.sku_id,
            ordered_qty=line.ordered_qty,
            fulfillment_mode=line.fulfillment_mode,
        )
        session.delete(line)
    with kernel() as session:
        assert any("A-BLACK-M" in problem for problem in service.consistency_problems(session))
    verification.retry_pending(kernel, operation_id, now=NOW)
    state = operation(kernel, operation_id)
    assert state["committed_verification_pending"]
    with kernel() as session, begin_immediate(session):
        session.add(OrderLine(**saved))
    verification.retry_pending(
        kernel, operation_id, now=aware(state["verification_next_attempt_at"])
    )
    assert not operation(kernel, operation_id)["committed_verification_pending"]


@pytest.mark.parametrize("revision", [0, 99])
def test_shipment_facts_survive_missing_current_revision(kernel, revision):
    with kernel() as session, begin_immediate(session):
        session.get(SalesOrder, "SO-1001").current_revision = revision
    with kernel() as session:
        problems = service.consistency_problems(session)
        assert any("SO-1001/A-BLACK-M" in problem and "发货事实" in problem for problem in problems)
