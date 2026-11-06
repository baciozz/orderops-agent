"""Cancellation exempts only a proven past link, never future/missing revisions."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, get_ident

import pytest
from app.config import Settings
from app.db.models import DocRevision, DownstreamLink, Operation, OrderRevision
from app.db.session import begin_immediate
from app.services import kernel as service
from app.services import verification
from sqlalchemy import select
from tests import test_verification_mto_missing_lines as support
from tests import test_verification_result_fencing as fencing
from tests.test_verification_purchase_links import commit_change
from tests.verification_scale import NOW

kernel = support.kernel


@pytest.fixture()
def cancelled(kernel):
    commit_change(kernel, 1, [{"mode": "cancel_remaining", "sku_id": "C-BLACK-L"}])
    changes = [
        {"mode": "replace_remaining", "from_sku": "A-BLACK-M", "to_sku": "A-WHITE-M", "qty": 1}
    ]
    commit_change(kernel, 2, changes)
    marker = service.verify_failpoint.set(True)
    try:
        result = commit_change(kernel, 3, changes)
    finally:
        service.verify_failpoint.reset(marker)
    assert service.inspect_downstream(kernel) == []
    return result.operation_id


def corrupt(factory, database_url, doc_id, fault):
    """Return a restorer using exact captured synthetic evidence only."""
    with factory() as session, begin_immediate(session):
        link = session.scalar(
            select(DownstreamLink)
            .where(DownstreamLink.doc_id == doc_id)
            .order_by(DownstreamLink.link_seq.desc())
        )
        link_id, original_revision = link.id, link.order_revision
        assert original_revision == 2
        snapshot = session.scalar(
            select(DocRevision).where(DocRevision.doc_id == doc_id, DocRevision.rev_no == 2)
        )
        operation = session.get(Operation, link.operation_id)
        original_body = operation.result_json
        original_origin = snapshot.operation_id
        saved_revision = None
        if fault in {"future_missing", "future_exists"}:
            link.order_revision = 999
            if fault == "future_exists":
                session.add(
                    OrderRevision(
                        id="synthetic-future",
                        order_id="SO-1001",
                        revision_no=999,
                        base_revision=998,
                        reason="injected future revision",
                        created_by="test",
                        created_at=NOW,
                    )
                )
        elif fault == "past_wrong":
            link.order_revision = 3  # Exists, but was not the cancellation's commit.
        elif fault == "past_missing":
            row = session.get(OrderRevision, "SOR-SO-1001-2")
            saved_revision = {c.name: getattr(row, c.name) for c in OrderRevision.__table__.columns}
        elif fault == "snapshot_origin":
            snapshot.operation_id = None
        elif fault == "receipt_order":
            operation.result_json = {**original_body, "order_id": "SO-1002"}
        elif fault == "receipt_bool":
            operation.result_json = {**original_body, "revision": True}
        elif fault == "receipt_missing":
            operation.result_json = None
        elif fault == "receipt_malformed":
            operation.result_json = ["not-a-receipt"]
        else:
            raise AssertionError(fault)
        snapshot_id, operation_id = snapshot.id, operation.id
    if saved_revision:
        with sqlite3.connect(database_url.removeprefix("sqlite:///")) as db:
            db.execute("DELETE FROM order_revision WHERE id=?", (saved_revision["id"],))

    def restore():
        with factory() as session, begin_immediate(session):
            session.get(DownstreamLink, link_id).order_revision = original_revision
            session.get(DocRevision, snapshot_id).operation_id = original_origin
            session.get(Operation, operation_id).result_json = original_body
            if saved_revision:
                session.add(OrderRevision(**saved_revision))
            if fault == "future_exists":
                session.delete(session.get(OrderRevision, "synthetic-future"))

    return restore


@pytest.mark.parametrize("doc_id", ["MO-2002", "PUR-3002"])
@pytest.mark.parametrize(
    "fault",
    [
        "future_missing",
        "future_exists",
        "past_missing",
        "past_wrong",
        "snapshot_origin",
        "receipt_order",
        "receipt_bool",
        "receipt_missing",
        "receipt_malformed",
    ],
)
def test_invalid_cancelled_history_keeps_pending_until_trusted_recovery(
    kernel, cancelled, database_url, doc_id, fault
):
    restore = corrupt(kernel, database_url, doc_id, fault)
    before = support.facts(kernel)
    report = service.inspect_downstream(kernel)
    assert any(r["doc_id"] == doc_id and r["repair"]["action"] == "manual_review" for r in report)
    service._post_verify(kernel, cancelled)
    assert fencing.operation(kernel, cancelled)["committed_verification_pending"]
    settings = Settings(_env_file=None, verification_max_retries=0)
    assert verification.retry_pending(kernel, cancelled, now=NOW, settings=settings) == 0
    assert not verification.verify_manually(kernel, cancelled, now=NOW, settings=settings)
    assert support.facts(kernel) == before
    restore()
    assert service.inspect_downstream(kernel) == []
    repaired = support.facts(kernel)
    assert verification.verify_manually(kernel, cancelled, now=NOW, settings=settings)
    assert not fencing.operation(kernel, cancelled)["committed_verification_pending"]
    assert support.facts(kernel) == repaired


def test_legitimate_cancellation_revision_two_remains_valid_at_revision_four(kernel, cancelled):
    before = support.facts(kernel)
    service._post_verify(kernel, cancelled)
    assert not fencing.operation(kernel, cancelled)["committed_verification_pending"]
    assert support.facts(kernel) == before


@pytest.mark.parametrize("doc_id", ["MO-2002", "PUR-3002"])
@pytest.mark.parametrize("takeover", [False, True])
def test_future_cancelled_link_after_old_success_cannot_clear_pending(
    kernel, cancelled, database_url, monkeypatch, doc_id, takeover
):
    arrived, release = Event(), Event()
    thread = []
    original = service.downstream_problems
    current = [NOW]
    settings = Settings(_env_file=None, verification_lease_seconds=10, verification_max_retries=3)

    def pause(session):
        result = original(session)
        if thread and get_ident() == thread[0] and not arrived.is_set():
            assert result == []
            arrived.set()
            assert release.wait(15)
        return result

    def old():
        thread.append(get_ident())
        return verification.retry_pending(
            kernel, cancelled, settings=settings, clock=lambda: current[0]
        )

    monkeypatch.setattr(service, "downstream_problems", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(old)
        try:
            assert arrived.wait(10)
            corrupt(kernel, database_url, doc_id, "future_missing")
            if takeover:
                current[0] += timedelta(seconds=11)
                verification.retry_pending(
                    kernel, cancelled, settings=settings, clock=lambda: current[0]
                )
            before = fencing.operation(kernel, cancelled)
            assert before["committed_verification_pending"]
        finally:
            release.set()
        future.result(timeout=15)
    after = fencing.operation(kernel, cancelled)
    assert after["committed_verification_pending"]
    if takeover:
        assert after == before
