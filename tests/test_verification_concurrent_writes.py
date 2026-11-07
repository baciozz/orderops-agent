"""Unrelated commits cannot starve readback; the locked recheck still fails closed."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from app.config import Settings
from app.db.models import (
    DocRevision,
    MaterialUsage,
    Operation,
    OrderLine,
    OrderRevision,
    OutboxDelivery,
    ProductionOrder,
    PurchaseOrder,
    SalesOrder,
    StockBalance,
)
from app.db.session import _BEGIN_IMMEDIATE, begin_immediate
from app.orchestration.state import aware
from app.services import kernel as service
from app.services import verification
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from tests import test_commit_verification_pending as commit_support
from tests import test_verification_result_fencing as support

kernel = support.kernel
approved_commit = support.approved_commit
NOW = support.NOW


def unrelated_write(factory):
    # Delivery bookkeeping is not an inventory/order/downstream fact.
    with factory() as session, begin_immediate(session):
        row = session.scalars(select(OutboxDelivery)).first()
        assert row is not None
        row.attempts += 1


def inject_noise(monkeypatch, factory):
    original = service.downstream_problems
    phases = []

    def readback(session):
        result = original(session)
        locked = _BEGIN_IMMEDIATE.get()
        phases.append(locked)
        if not locked:
            unrelated_write(factory)
        return result

    monkeypatch.setattr(service, "downstream_problems", readback)
    return phases


def business_contents(factory):
    contents = commit_support._contents_except_verification_flag(factory)
    contents.pop("outbox_delivery")  # The explicitly injected bookkeeping write.
    return contents


@pytest.mark.parametrize("kind", ["initial", "automatic", "manual"])
def test_unrelated_commits_do_not_exhaust_verification_budget(
    kernel, approved_commit, monkeypatch, kind
):
    operation_id = support.pending(kernel, approved_commit[1])
    config = Settings(_env_file=None, verification_max_retries=0 if kind == "manual" else 3)
    if kind == "manual":
        assert verification.retry_pending(kernel, operation_id, settings=config, now=NOW) == 0
    before = business_contents(kernel)
    phases = inject_noise(monkeypatch, kernel)
    if kind == "initial":
        service._post_verify(kernel, operation_id)
    elif kind == "manual":
        assert verification.verify_manually(kernel, operation_id, settings=config, now=NOW)
    else:
        moment = NOW
        for _ in range(config.verification_max_retries):
            assert (
                verification.retry_pending(kernel, operation_id, settings=config, now=moment) == 1
            )
            state = support.operation(kernel, operation_id)
            if not state["committed_verification_pending"]:
                break
            moment = (
                aware(state["verification_next_attempt_at"])
                if state["verification_next_attempt_at"]
                else moment + timedelta(days=1)
            )
    state = support.operation(kernel, operation_id)
    assert not state["committed_verification_pending"]
    assert not state["verification_manual_required"]
    assert state["verification_retry_attempts"] == (1 if kind == "automatic" else 0)
    assert phases == [False, True]  # At most one fresh recheck in the same attempt.
    assert business_contents(kernel) == before


@pytest.mark.parametrize(
    "fault", ["balance", "line", "production", "purchase", "usage", "snapshot", "link"]
)
def test_changed_facts_are_rechecked_despite_unrelated_write(
    kernel, approved_commit, monkeypatch, fault
):
    operation_id = support.pending(kernel, approved_commit[1])
    saved = []
    original = service.downstream_problems
    phases = []

    def readback(session):
        result = original(session)
        locked = _BEGIN_IMMEDIATE.get()
        phases.append(locked)
        if not locked:
            unrelated_write(kernel)
            with kernel() as writer, begin_immediate(writer):
                if fault == "balance":
                    row, field = writer.get(StockBalance, ("W1", "A-WHITE-M")), "on_hand"
                elif fault == "line":
                    revision = writer.scalar(
                        select(OrderRevision).where(
                            OrderRevision.order_id == "SO-1001", OrderRevision.revision_no == 2
                        )
                    )
                    row, field = writer.get(OrderLine, (revision.id, "A-WHITE-M")), "ordered_qty"
                elif fault == "production":
                    row, field = writer.get(ProductionOrder, "MO-2002"), "planned_qty"
                elif fault == "purchase":
                    row, field = writer.get(PurchaseOrder, "PUR-3002"), "ordered_qty"
                elif fault == "usage":
                    row, field = writer.scalars(select(MaterialUsage)).first(), "qty_per_piece"
                elif fault == "snapshot":
                    row = writer.scalars(
                        select(DocRevision).where(DocRevision.doc_type == "production_order")
                    ).first()
                    field = "snapshot_json"
                else:
                    row, field = service._current_links(writer)[0], "order_revision"
                old = getattr(row, field)
                saved.append(
                    (
                        type(row),
                        tuple(getattr(row, col.name) for col in row.__table__.primary_key.columns),
                        field,
                        old,
                    )
                )
                setattr(
                    row,
                    field,
                    {**old, "planned_qty": old["planned_qty"] + 1}
                    if fault == "snapshot"
                    else old + 1,
                )
        return result

    with monkeypatch.context() as patch:
        patch.setattr(service, "downstream_problems", readback)
        assert verification.retry_pending(kernel, operation_id, now=NOW) == 1
    state = support.operation(kernel, operation_id)
    assert state["committed_verification_pending"]
    assert phases == [False, True]
    with kernel() as session, begin_immediate(session):
        model, key, field, old = saved[0]
        setattr(session.get(model, key), field, old)
    assert (
        verification.retry_pending(
            kernel, operation_id, now=aware(state["verification_next_attempt_at"])
        )
        == 1
    )
    assert not support.operation(kernel, operation_id)["committed_verification_pending"]


def test_valid_concurrent_revision_uses_fresh_facts(kernel, approved_commit, monkeypatch):
    operation_id = support.pending(kernel, approved_commit[1])
    original = service.downstream_problems
    phases = []

    def readback(session):
        result = original(session)
        locked = _BEGIN_IMMEDIATE.get()
        phases.append(locked)
        if not locked:
            with monkeypatch.context() as patch:
                patch.setattr(service, "downstream_problems", original)
                with kernel() as reader:
                    order = reader.get(SalesOrder, "SO-1001")
                    revision, intent = order.current_revision, order.intent_revision
                plan = service.plan_change(
                    kernel,
                    order_id="SO-1001",
                    base_revision=revision,
                    intent_revision=intent,
                    changes=[{"mode": "increment", "sku_id": "A-WHITE-M", "qty": 1}],
                    client_event_key="valid-during-readback",
                )
                approval = service.approve_plan(
                    kernel,
                    plan_id=plan.plan_id,
                    plan_hash=plan.plan_hash,
                    expected_intent_revision=plan.intent_revision,
                )
                service.commit_plan(
                    kernel,
                    plan_id=plan.plan_id,
                    plan_hash=plan.plan_hash,
                    approval_id=approval.approval_id,
                    idempotency_key="valid-during-readback",
                )
        return result

    monkeypatch.setattr(service, "downstream_problems", readback)
    assert verification.retry_pending(kernel, operation_id, now=NOW) == 1
    assert not support.operation(kernel, operation_id)["committed_verification_pending"]
    assert phases == [False, True]
    with kernel() as session:
        assert session.get(SalesOrder, "SO-1001").current_revision == 3


@pytest.mark.parametrize("failure", ["exception", "crash", "expiry"])
def test_locked_recheck_failure_and_recovery(kernel, approved_commit, monkeypatch, failure):
    class ProcessExit(BaseException):
        pass

    operation_id = support.pending(kernel, approved_commit[1])
    before = business_contents(kernel)
    current = [NOW]
    config = Settings(_env_file=None, verification_lease_seconds=10)
    original = service.consistency_problems
    locked_checks = []

    def recheck(session):
        if _BEGIN_IMMEDIATE.get():
            locked_checks.append(failure)
            if failure == "exception":
                raise RuntimeError("synthetic locked readback failure")
            if failure == "crash":
                raise ProcessExit()
            current[0] += timedelta(seconds=10)
        return original(session)

    with monkeypatch.context() as patch:
        inject_noise(patch, kernel)
        patch.setattr(service, "consistency_problems", recheck)
        if failure == "crash":
            with pytest.raises(ProcessExit):
                verification.retry_pending(
                    kernel, operation_id, settings=config, clock=lambda: current[0]
                )
        else:
            assert (
                verification.retry_pending(
                    kernel, operation_id, settings=config, clock=lambda: current[0]
                )
                == 1
            )
    state = support.operation(kernel, operation_id)
    assert locked_checks == [failure]
    assert state["committed_verification_pending"]
    assert state["verification_retry_attempts"] == 1
    assert business_contents(kernel) == before
    if failure == "exception":
        assert state["verification_lease_token"] is None
        due = aware(state["verification_next_attempt_at"])
    else:
        assert state["verification_lease_token"]
        assert state["verification_next_attempt_at"] is None
        due = NOW + timedelta(seconds=10)
    assert verification.retry_pending(kernel, operation_id, settings=config, now=due) == 1
    assert not support.operation(kernel, operation_id)["committed_verification_pending"]
    assert business_contents(kernel) == before


def test_locked_recheck_excludes_writers_and_releases_lock(kernel, approved_commit, monkeypatch):
    operation_id = support.pending(kernel, approved_commit[1])
    phases = inject_noise(monkeypatch, kernel)
    original = service.consistency_problems
    blocked = []

    def concurrent_read():
        with kernel() as reader:
            assert reader.get(Operation, operation_id).committed_verification_pending

    def recheck(session):
        if _BEGIN_IMMEDIATE.get():
            # Another thread starts with its own transaction context and connection.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(concurrent_read).result(timeout=5)
                with pytest.raises(OperationalError, match="locked"):
                    pool.submit(unrelated_write, kernel).result(timeout=5)
            blocked.append(True)
        return original(session)

    monkeypatch.setattr(service, "consistency_problems", recheck)
    assert verification.retry_pending(kernel, operation_id, now=NOW) == 1
    assert not support.operation(kernel, operation_id)["committed_verification_pending"]
    assert phases == [False, True] and blocked == [True]
    unrelated_write(kernel)  # No lock is leaked by the pinned connection/session.


def test_concurrent_verifiers_do_not_invalidate_each_other(kernel, approved_commit, monkeypatch):
    first = support.pending(kernel, approved_commit[1])
    from tests import test_link_audit as execution_support

    marker = service.verify_failpoint.set(True)
    try:
        second = execution_support.commit(
            kernel, 2, changes=[{"mode": "set_total", "sku_id": "A-WHITE-M", "qty": 41}]
        ).operation_id
    finally:
        service.verify_failpoint.reset(marker)
    barrier = Barrier(2)
    original = service.downstream_problems

    def readback(session):
        result = original(session)
        if not _BEGIN_IMMEDIATE.get():
            barrier.wait(10)
        return result

    monkeypatch.setattr(service, "downstream_problems", readback)
    config = Settings(_env_file=None, verification_max_retries=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(
            pool.map(
                lambda op: verification.retry_pending(kernel, op, settings=config, now=NOW),
                [first, second],
            )
        )
    assert counts == [1, 1]
    for operation_id in (first, second):
        state = support.operation(kernel, operation_id)
        assert not state["committed_verification_pending"]
        assert not state["verification_manual_required"]
