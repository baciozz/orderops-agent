"""Readback detects per-order corruption even when SKU aggregates still balance."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from app.db.models import Operation, OrderLine, OrderRevision, Reservation, SalesOrder
from app.db.session import begin_immediate
from app.orchestration.state import aware
from app.services.kernel import (
    approve_plan,
    commit_plan,
    consistency_problems,
    plan_change,
    retry_pending_verification,
    verify_failpoint,
)
from sqlalchemy import select
from tests import test_commit_verification_pending as support

approved_commit = support.approved_commit
kernel = support.kernel


def test_order_line_fault_keeps_pending_until_repaired(kernel, approved_commit):
    token = verify_failpoint.set(True)
    try:
        result = commit_plan(kernel, **approved_commit[1])
    finally:
        verify_failpoint.reset(token)
    with kernel() as session, begin_immediate(session):
        revision = session.scalar(
            select(OrderRevision).where(
                OrderRevision.order_id == "SO-1001", OrderRevision.revision_no == result.revision
            )
        )
        row = session.get(OrderLine, (revision.id, "A-WHITE-M"))
        row.ordered_qty += 1
        revision_id = revision.id
    retry_pending_verification(kernel, result.operation_id)
    with kernel() as session:
        assert session.get(Operation, result.operation_id).committed_verification_pending
        assert any("SO-1001/W1/A-WHITE-M" in p for p in consistency_problems(session))
    with kernel() as session, begin_immediate(session):
        session.get(OrderLine, (revision_id, "A-WHITE-M")).ordered_qty -= 1
        next_retry = aware(session.get(Operation, result.operation_id).verification_next_attempt_at)
    retry_pending_verification(kernel, result.operation_id, clock=lambda: next_retry)
    with kernel() as session:
        assert not session.get(Operation, result.operation_id).committed_verification_pending
        assert consistency_problems(session) == []


def test_balanced_reservation_transfer_between_orders_is_detected(kernel):
    with kernel() as session, begin_immediate(session):
        row = session.get(Reservation, ("SO-1001", "W1", "A-BLACK-M"))
        row.qty -= 1
        session.add(
            Reservation(
                order_id="SO-1002", warehouse_id="W1", sku_id="A-BLACK-M", qty=1, revision=1
            )
        )
    with kernel() as session:
        errors = consistency_problems(session)
        assert any("SO-1001/W1/A-BLACK-M" in p for p in errors)
        assert any("SO-1002/W1/A-BLACK-M" in p for p in errors)
        assert not any("预占总和与余额" in p for p in errors)


@pytest.mark.parametrize("fault", ["missing_reservation", "missing_revision", "below_shipped"])
def test_current_facts_faults_are_visible(kernel, fault):
    with kernel() as session, begin_immediate(session):
        order = session.get(SalesOrder, "SO-1001")
        if fault == "missing_reservation":
            session.delete(session.get(Reservation, (order.id, "W1", "A-BLACK-M")))
        elif fault == "missing_revision":
            order.current_revision += 1
        else:
            revision = session.scalar(
                select(OrderRevision).where(
                    OrderRevision.order_id == order.id,
                    OrderRevision.revision_no == order.current_revision,
                )
            )
            session.get(OrderLine, (revision.id, "A-BLACK-M")).ordered_qty = 0
    with kernel() as session:
        assert consistency_problems(session)


def test_pending_old_operation_and_concurrent_valid_revision(kernel, approved_commit):
    token = verify_failpoint.set(True)
    try:
        old = commit_plan(kernel, **approved_commit[1])
    finally:
        verify_failpoint.reset(token)
    with kernel() as session:
        order = session.get(SalesOrder, "SO-1001")
        revision, intent = order.current_revision, order.intent_revision
    plan = plan_change(
        kernel,
        order_id="SO-1001",
        base_revision=revision,
        intent_revision=intent,
        changes=[{"mode": "increment", "sku_id": "A-WHITE-M", "qty": 1}],
        client_event_key="later-valid",
    )
    approval = approve_plan(
        kernel,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
    )
    barrier = Barrier(2)

    def action(kind):
        barrier.wait(5)
        if kind == "verify":
            retry_pending_verification(kernel, old.operation_id)
        else:
            commit_plan(
                kernel,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                approval_id=approval.approval_id,
                idempotency_key="later-valid",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(action, ["verify", "commit"]))
    with kernel() as session:
        assert consistency_problems(session) == []
        assert session.get(SalesOrder, "SO-1001").current_revision == old.revision + 1
        assert all(
            not row.committed_verification_pending for row in session.scalars(select(Operation))
        )
