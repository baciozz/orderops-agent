"""Design evidence, including known gaps; no production manifest is implemented.

Tests named ``existing_*`` document rollout blockers, not desired future behavior.
Replace their expectations when a complete manifest protocol is implemented.
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from app.db.models import Approval, DownstreamLink, Operation, OrderLine, OrderRevision, Plan
from app.db.schema import schema_ready
from app.db.session import begin_immediate, make_engine, make_session_factory
from app.services import kernel, link_audit
from sqlalchemy import delete, select, text
from tests import test_link_audit as support
from tests import test_qi_received_po_supplement as supplement_support
from tests.manifest_transaction_probe import install_probe, read_receipt, receipt_problems

NOW = support.NOW
STOCK = support.STOCK
seeded = support.seeded
commit = support.commit
dump = support.dump
supplement = supplement_support.kernel


def approved(factory):
    plan = kernel.plan_change(
        factory,
        order_id="SO-1001",
        base_revision=1,
        intent_revision=1,
        changes=STOCK,
        client_event_key="manifest-design",
        now=NOW,
    )
    approval = kernel.approve_plan(
        factory,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
        now=NOW,
    )
    return dict(
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        approval_id=approval.approval_id,
        idempotency_key=plan.plan_id,
        now=NOW,
    )


def erase_line_and_origin(factory, revision):
    with factory() as session, begin_immediate(session):
        revision_id = session.scalar(
            select(OrderRevision.id).where(
                OrderRevision.order_id == "SO-1001", OrderRevision.revision_no == revision
            )
        )
        assert revision_id is not None
        session.execute(
            delete(OrderLine).where(
                OrderLine.revision_id == revision_id,
                OrderLine.sku_id == "C-BLACK-L",
            )
        )
        session.execute(delete(DownstreamLink).where(DownstreamLink.order_id == "SO-1001"))


def test_existing_unknown_legacy_origin_is_not_repaired_by_new_approval(seeded, database_url):
    erase_line_and_origin(seeded, 1)
    with seeded() as session:
        assert kernel._verification_problems(session) == []
    result = commit(seeded)
    with seeded() as session:
        op = session.get(Operation, result.operation_id)
        assert not op.committed_verification_pending
        assert session.get(Plan, result.plan_id).normalized_changes["downstream"] == []
    report = link_audit.audit_operation_links(database_url, result.operation_id)
    assert report.status == "consistent_for_operation"
    assert report.expected_link_count == 0 and report.global_completeness == "not_assessed"


def test_approved_execution_payload_survives_mutable_plan_json(seeded, monkeypatch, database_url):
    install_probe(seeded, monkeypatch)
    request = approved(seeded)
    with seeded() as session, begin_immediate(session):
        plan = session.get(Plan, request["plan_id"])
        plan.normalized_changes = {**plan.normalized_changes, "downstream": []}
    result = kernel.commit_plan(seeded, **request)
    with seeded() as session:
        body = read_receipt(session, result.operation_id)
        assert len(body["approved_payload"]["downstream"]) == body["edge_count"] == 4
        assert receipt_problems(session, result.operation_id) == []
    # Approval hash binds actual recomputation, not the now-corrupt stored JSON.
    assert link_audit.audit_operation_links(database_url, result.operation_id).status == "uncertain"


def test_existing_latest_delta_cannot_prove_cancelled_historical_origin(seeded, database_url):
    cancel = commit(seeded, changes=[{"mode": "cancel_remaining", "sku_id": "C-BLACK-L"}])
    later = commit(seeded, 2)
    with seeded() as session, begin_immediate(session):
        session.execute(
            delete(DownstreamLink).where(DownstreamLink.doc_id.in_(["MO-2002", "PUR-3002"]))
        )
    with seeded() as session:
        assert kernel._verification_problems(session) == []
    assert link_audit.audit_operation_links(database_url, later.operation_id).status == (
        "consistent_for_operation"
    )
    assert link_audit.audit_operation_links(database_url, cancel.operation_id).status == "findings"


@pytest.mark.parametrize(
    "stage", ["before_header", "after_header", "during_edges", "after_receipt_binding"]
)
def test_proposed_receipt_failure_rolls_back_entire_real_commit(
    seeded, database_url, monkeypatch, stage
):
    install_probe(seeded, monkeypatch, fail_at=stage)
    request = approved(seeded)
    before = dump(database_url)
    with pytest.raises(RuntimeError, match="design fault"):
        kernel.commit_plan(seeded, **request)
    # All tables: business rows, approval, task, outbox, operation AND sidecar.
    assert dump(database_url) == before
    with seeded() as session:
        assert session.get(Approval, request["approval_id"]).status == "ACTIVE"


def test_proposed_receipt_recovers_after_committed_process_loss_and_replays_once(
    seeded, database_url, monkeypatch
):
    install_probe(seeded, monkeypatch)
    request = approved(seeded)
    with monkeypatch.context() as patch:

        def crash(*args, **kwargs):
            raise RuntimeError("process lost after durable commit")

        patch.setattr(kernel, "_post_verify", crash)
        with pytest.raises(RuntimeError, match="process lost"):
            kernel.commit_plan(seeded, **request)
    # A fresh engine cannot observe a partially written header/edges/receipt.
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            op = session.scalar(select(Operation))
            assert op.committed_verification_pending
            op_id = op.id
            assert receipt_problems(session, op_id) == []
        replay = kernel.commit_plan(factory, **request)
        assert replay.replayed and replay.operation_id == op_id
        before = dump(database_url)
        assert kernel.commit_plan(factory, **request).operation_id == op_id
        assert dump(database_url) == before
        with factory() as session:
            assert session.scalar(text("SELECT count(*) FROM design_receipt")) == 1
            assert session.scalar(text("SELECT count(*) FROM design_edge")) == 4
    finally:
        engine.dispose()


def test_proposed_concurrent_same_key_has_one_receipt_and_operation(seeded, monkeypatch):
    install_probe(seeded, monkeypatch)
    request = approved(seeded)
    ready = Barrier(2)

    def execute():
        ready.wait(timeout=10)
        return kernel.commit_plan(seeded, **request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute) for _ in range(2)]
        results = [f.result(timeout=15) for f in futures]
    assert len({r.operation_id for r in results}) == 1
    assert sorted(r.replayed for r in results) == [False, True]
    with seeded() as session:
        assert session.scalar(text("SELECT count(*) FROM operation")) == 1
        assert session.scalar(text("SELECT count(*) FROM design_receipt")) == 1
        assert receipt_problems(session, results[0].operation_id) == []


@pytest.mark.parametrize("fault", ["missing", "version", "hash", "edge", "approval"])
def test_proposed_replay_never_regenerates_unknown_or_damaged_evidence(
    seeded, database_url, monkeypatch, fault
):
    install_probe(seeded, monkeypatch)
    request = approved(seeded)
    result = kernel.commit_plan(seeded, **request)
    with seeded() as session, begin_immediate(session):
        if fault == "missing":
            session.execute(text("DELETE FROM design_receipt"))
        elif fault == "version":
            body = read_receipt(session, result.operation_id)
            body["version"] = 999
            session.execute(
                text("UPDATE design_receipt SET body=:body"), {"body": json.dumps(body)}
            )
        elif fault == "hash":
            op = session.get(Operation, result.operation_id)
            op.result_json = {**op.result_json, "design_receipt_hash": "wrong"}
        elif fault == "edge":
            session.execute(text("DELETE FROM design_edge WHERE doc_id='MO-2002'"))
        else:
            session.get(Approval, request["approval_id"]).consumed_operation_id = "wrong"
    before = dump(database_url)
    with pytest.raises(RuntimeError, match="design replay"):
        kernel.commit_plan(seeded, **request)
    assert dump(database_url) == before


def test_proposed_receipt_detects_lost_line_and_all_links_without_reconstructing_plan(
    seeded, monkeypatch
):
    install_probe(seeded, monkeypatch)
    result = commit(seeded)
    erase_line_and_origin(seeded, 2)
    with seeded() as session:
        assert kernel._verification_problems(session) == []
        assert receipt_problems(session, result.operation_id) == [
            "business_edges",
            "business_lines",
        ]
        assert read_receipt(session, result.operation_id)["origin_completeness"] == "unknown"


def test_proposed_receipt_does_not_promote_damaged_legacy_base(seeded, monkeypatch):
    erase_line_and_origin(seeded, 1)
    install_probe(seeded, monkeypatch)
    result = commit(seeded)
    with seeded() as session:
        body = read_receipt(session, result.operation_id)
        assert receipt_problems(session, result.operation_id) == []
        assert body["edge_count"] == 0
        assert body["origin_completeness"] == "unknown"  # Must NOT become VERIFIED.


def test_explicit_empty_receipt_is_distinct_from_missing_receipt(seeded, monkeypatch):
    install_probe(seeded, monkeypatch)
    plan = kernel.plan_new_order(
        seeded,
        customer_name="华东服饰",
        lines=[{"sku_id": "A-WHITE-M", "qty": 1}],
        client_event_key="manifest-new-stock",
        now=NOW,
    )
    result = support.approve_commit(seeded, plan)
    with seeded() as session, begin_immediate(session):
        assert receipt_problems(session, result.operation_id) == []
        assert read_receipt(session, result.operation_id)["edge_count"] == 0
        session.execute(text("DELETE FROM design_receipt"))
    with seeded() as session:
        assert receipt_problems(session, result.operation_id) == ["missing_receipt"]


@pytest.mark.parametrize(
    "changes",
    [
        [{"mode": "cancel_remaining", "sku_id": "C-BLACK-L"}],
        [{"mode": "set_total", "sku_id": "C-BLACK-L", "qty": 210}],
        [{"mode": "replace_remaining", "from_sku": "C-BLACK-L", "to_sku": "C-NAVY-L", "qty": 180}],
        STOCK,
    ],
    ids=["cancel_zero_tail", "partial_tail", "recolor", "mixed_stock_mto"],
)
def test_receipt_preserves_exact_history_across_lifecycle_changes(seeded, monkeypatch, changes):
    install_probe(seeded, monkeypatch)
    first = commit(seeded, changes=changes)
    later = commit(seeded, 2)
    with seeded() as session:
        assert receipt_problems(session, first.operation_id) == []
        assert receipt_problems(session, later.operation_id) == []
        assert read_receipt(session, first.operation_id)["edge_count"] == 4
        # Current graph needs cancellation anchors; latest operation delta omits them.
        assert read_receipt(session, later.operation_id)["edge_count"] == (
            2 if changes[0]["mode"] == "cancel_remaining" else 4
        )


def test_received_purchase_supplement_uses_approved_new_identity(supplement, monkeypatch):
    install_probe(supplement, monkeypatch)
    plan = supplement_support._plan(supplement, key="manifest-supplement")
    result = support.approve_commit(supplement, plan, now=supplement_support.NOW)
    with supplement() as session:
        body = read_receipt(session, result.operation_id)
        assert receipt_problems(session, result.operation_id) == []
        payload = body["approved_payload"]
        new_doc = supplement_support.PO_ID + "-S1"
        assert payload["expected_doc_revisions"][new_doc] == {
            "doc_type": "purchase_order",
            "exists": False,
        }
        added = [v for v in payload["downstream"] if v["doc_id"] == new_doc]
        assert len(added) == 1 and added[0]["before"] is None
        assert added[0]["order_line_sku"] == supplement_support.SKU


def test_only_exact_captured_receipt_recovery_restores_check(seeded, database_url, monkeypatch):
    install_probe(seeded, monkeypatch)
    result = commit(seeded)
    before = dump(database_url)
    with seeded() as session, begin_immediate(session):
        raw = session.scalar(text("SELECT body FROM design_receipt"))
        session.execute(text("UPDATE design_receipt SET body=:value"), {"value": "{}"})
    with seeded() as session:
        assert receipt_problems(session, result.operation_id) == ["unsupported_version"]
    with seeded() as session, begin_immediate(session):
        session.execute(text("UPDATE design_receipt SET body=:value"), {"value": raw})
    with seeded() as session:
        assert receipt_problems(session, result.operation_id) == []
    assert dump(database_url) == before


def test_candidate_receipt_check_uses_existing_result_fence(seeded, monkeypatch):
    install_probe(seeded, monkeypatch)
    result = commit(seeded)
    with seeded() as session, begin_immediate(session):
        session.get(Operation, result.operation_id).committed_verification_pending = True
    read = Event()
    resume = Event()
    original = kernel._verification_problems

    def check(session):
        problems = original(session) + receipt_problems(session, result.operation_id)
        if not read.is_set():
            read.set()
            assert resume.wait(timeout=10)
        return problems

    monkeypatch.setattr(kernel, "_verification_problems", check)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_success = pool.submit(kernel._post_verify, seeded, result.operation_id)
        try:
            assert read.wait(timeout=10)
            erase_line_and_origin(seeded, 2)
            kernel._post_verify(seeded, result.operation_id)
        finally:
            resume.set()
        old_success.result(timeout=15)
    with seeded() as session:
        assert session.get(Operation, result.operation_id).committed_verification_pending
        assert receipt_problems(session, result.operation_id) == [
            "business_edges",
            "business_lines",
        ]


def test_receipt_read_snapshot_can_become_stale_before_publication(seeded, monkeypatch):
    install_probe(seeded, monkeypatch)
    result = commit(seeded)
    read = Event()
    changed = Event()

    def old_reader():
        with seeded() as session:
            assert receipt_problems(session, result.operation_id) == []
            read.set()
            assert changed.wait(timeout=10)
            assert receipt_problems(session, result.operation_id) == []
            session.rollback()
            assert receipt_problems(session, result.operation_id) == ["business_edges"]

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(old_reader)
        try:
            assert read.wait(timeout=10)
            with seeded() as session, begin_immediate(session):
                session.execute(delete(DownstreamLink))
        finally:
            changed.set()
        future.result(timeout=15)


def test_existing_startup_guard_does_not_stop_already_running_old_writer(seeded, engine):
    request = approved(seeded)
    with seeded() as session, begin_immediate(session):
        session.execute(text("UPDATE alembic_version SET version_num='manifest_future_probe'"))
    with pytest.raises(RuntimeError, match="数据库迁移版本信息"):
        schema_ready(engine)
    # The existing factory remains usable. DDL-only rollout needs a writer fence.
    result = kernel.commit_plan(seeded, **request)
    assert result.revision == 2


@pytest.mark.parametrize("action", ["old_update", "old_insert", "new_atomic", "missing_header"])
def test_deferred_pointer_constraint_can_fence_legacy_writers(tmp_path, action):
    # Isolated relational sketch, not an Alembic migration of the application.
    with sqlite3.connect(tmp_path / "constraint.db") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(
            "CREATE TABLE header (order_id TEXT, revision INTEGER, id TEXT, "
            "PRIMARY KEY(order_id, revision, id));"
            "CREATE TABLE orders (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
            "manifest_id TEXT NOT NULL, FOREIGN KEY(id, revision, manifest_id) "
            "REFERENCES header(order_id, revision, id) DEFERRABLE INITIALLY DEFERRED);"
            "INSERT INTO header VALUES ('known', 1, 'h1');"
            "INSERT INTO orders VALUES ('known', 1, 'h1');"
        )
        if action == "old_insert":
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("INSERT INTO orders(id, revision) VALUES ('old', 1)")
            db.rollback()
        else:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE orders SET revision=2, manifest_id=?",
                ("h1" if action == "old_update" else "h2",),
            )
            if action == "new_atomic":
                db.execute("INSERT INTO header VALUES ('known', 2, 'h2')")
                db.commit()
            else:
                with pytest.raises(sqlite3.IntegrityError):
                    db.commit()
                db.rollback()
        assert db.execute("SELECT revision FROM orders").fetchone()[0] == (
            2 if action == "new_atomic" else 1
        )
