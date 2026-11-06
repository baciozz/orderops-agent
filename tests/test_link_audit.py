"""Synthetic retained-commit proofs, uncertainty, recovery and read-only snapshots."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

import pytest
from app import cli
from app.config import Settings
from app.db.models import (
    Approval,
    AuditRecord,
    DocRevision,
    DownstreamLink,
    Operation,
    OrderLine,
    OrderRevision,
    Plan,
    ProductionOrder,
    PurchaseOrder,
    SalesOrder,
    StockMovement,
    Task,
)
from app.db.session import begin_immediate
from app.services import kernel, link_audit
from app.services.seed import load_seed
from sqlalchemy import delete, select, text
from tests import test_bounded_sheet_durable as sheet
from tests import test_bounded_sheet_integrity as sheet_integrity
from tests import test_qi_received_po_supplement as supplement_support

sheet_env = sheet.sheet_env
supplement = supplement_support.kernel

NOW = datetime(2026, 11, 3, tzinfo=UTC)
STOCK = [{"mode": "replace_remaining", "from_sku": "A-BLACK-M", "to_sku": "A-WHITE-M", "qty": 1}]


@pytest.fixture()
def seeded(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    return session_factory


def commit(factory, base=1, changes=None):
    plan = kernel.plan_change(
        factory,
        order_id="SO-1001",
        base_revision=base,
        intent_revision=base,
        changes=changes if changes is not None else STOCK,
        client_event_key=f"audit-{base}",
        now=NOW,
    )
    return approve_commit(factory, plan)


def approve_commit(factory, plan, now=NOW):
    assert plan.status == "VALIDATED"
    approval = kernel.approve_plan(
        factory,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
        now=now,
    )
    return kernel.commit_plan(
        factory,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        approval_id=approval.approval_id,
        idempotency_key=plan.plan_id,
        acknowledge=True,
        now=now,
    )


@pytest.fixture()
def committed(seeded):
    return commit(seeded)


def dump(database_url):
    with sqlite3.connect(database_url.removeprefix("sqlite:///")) as db:
        return "\n".join(db.iterdump())


def audit(database_url, result):
    before = dump(database_url)
    report = link_audit.audit_operation_links(database_url, result.operation_id)
    assert dump(database_url) == before  # Includes operation/approval/task/lease/outbox state.
    assert not report.authorizes_business_action
    assert report.global_completeness == report.current_ownership == "not_assessed"
    return report


def remove_links(factory, operation_id, *, all_history=False):
    with factory() as session, begin_immediate(session):
        rows = list(
            session.scalars(
                select(DownstreamLink).where(
                    DownstreamLink.order_id == "SO-1001"
                    if all_history
                    else DownstreamLink.operation_id == operation_id
                )
            )
        )
        saved = [
            {c.name: getattr(row, c.name) for c in DownstreamLink.__table__.columns} for row in rows
        ]
        for row in rows:
            session.delete(row)
    return saved


def test_real_commit_proves_four_links_without_touching_business_state(
    seeded, committed, database_url
):
    report = audit(database_url, committed)
    assert report.status == "consistent_for_operation"
    assert report.expected_link_count == report.matched_link_count == 4
    assert report.order_id == "SO-1001" and report.order_revision == 2


@pytest.mark.parametrize("all_history", [False, True])
def test_all_links_lost_and_restored_from_captured_evidence(
    seeded, committed, database_url, all_history
):
    captured = remove_links(seeded, committed.operation_id, all_history=all_history)
    report = audit(database_url, committed)
    assert report.status == "findings" and report.matched_link_count == 0
    assert {f.doc_id for f in report.findings} == {row["doc_id"] for row in captured}
    assert all(f.code == "EXPECTED_LINK_MISSING" for f in report.findings)
    with seeded() as session, begin_immediate(session):
        session.add_all(DownstreamLink(**row) for row in captured)
    assert audit(database_url, committed).status == "consistent_for_operation"


def test_zero_quantity_cancel_links_are_still_promised(seeded, database_url):
    result = commit(seeded, changes=[{"mode": "cancel_remaining", "sku_id": "C-BLACK-L"}])
    with seeded() as session, begin_immediate(session):
        assert session.get(ProductionOrder, "MO-2002").planned_qty == 0
        assert session.get(PurchaseOrder, "PUR-3002").status == "CANCELLED"
        session.execute(
            delete(DownstreamLink).where(
                DownstreamLink.operation_id == result.operation_id,
                DownstreamLink.doc_id.in_(["MO-2002", "PUR-3002"]),
            )
        )
    report = audit(database_url, result)
    assert {f.doc_id for f in report.findings} == {"MO-2002", "PUR-3002"}
    assert all(f.code == "EXPECTED_LINK_MISSING" for f in report.findings)


def test_cancel_and_later_stock_commits_preserve_distinct_historical_manifests(
    seeded, database_url
):
    cancel = commit(seeded, changes=[{"mode": "cancel_remaining", "sku_id": "C-BLACK-L"}])
    second = commit(seeded, 2)
    third = commit(seeded, 3)
    for result, count in [(cancel, 4), (second, 2), (third, 2)]:
        report = audit(database_url, result)
        assert report.status == "consistent_for_operation"
        assert report.expected_link_count == count
    # A later valid link does not replace an append-only historical edge.
    remove_links(seeded, second.operation_id)
    assert audit(database_url, second).status == "findings"
    assert audit(database_url, third).status == "consistent_for_operation"


def test_recolor_then_new_revision_does_not_compare_current_document_content(seeded, database_url):
    recolor = commit(
        seeded,
        changes=[
            {
                "mode": "replace_remaining",
                "from_sku": "C-BLACK-L",
                "to_sku": "C-NAVY-L",
                "qty": 180,
            }
        ],
    )
    later = commit(seeded, 2)
    assert audit(database_url, recolor).status == "consistent_for_operation"
    assert audit(database_url, later).status == "consistent_for_operation"
    # The audit is about the retained historical edge, not live document existence.
    with seeded() as session, begin_immediate(session):
        session.get(ProductionOrder, "MO-2002").planned_qty = 999
    assert audit(database_url, recolor).status == "consistent_for_operation"


def test_zero_quantity_purchase_link_loss_is_detected_without_quantity_inference(
    seeded, database_url
):
    with seeded() as session, begin_immediate(session):
        session.add(
            PurchaseOrder(
                id="ZERO-PO",
                supplier_id="SUP-01",
                production_order_id="MO-2002",
                material_id="FAB-BLACK",
                ordered_qty=0,
                received_qty=0,
                status="PLACED",
                change_state="NONE",
                doc_revision=1,
            )
        )
        session.add(
            DocRevision(
                id="ZERO-PO-1",
                doc_type="purchase_order",
                doc_id="ZERO-PO",
                rev_no=1,
                snapshot_json={"material_id": "FAB-BLACK", "ordered_qty": 0, "status": "PLACED"},
            )
        )
        session.add(
            DownstreamLink(
                id="ZERO-PO-LINK",
                doc_type="purchase_order",
                doc_id="ZERO-PO",
                order_id="SO-1001",
                order_revision=1,
                order_line_sku="C-BLACK-L",
                link_seq=1,
            )
        )
    result = commit(seeded)
    assert audit(database_url, result).expected_link_count == 5
    with seeded() as session, begin_immediate(session):
        session.execute(delete(DownstreamLink).where(DownstreamLink.doc_id == "ZERO-PO"))
    assert kernel.inspect_downstream(seeded) == []
    report = audit(database_url, result)
    assert [(f.doc_id, f.code) for f in report.findings] == [("ZERO-PO", "EXPECTED_LINK_MISSING")]


def test_missing_line_and_all_links_is_uncertain_until_hash_evidence_is_restored(
    seeded, committed, database_url
):
    remove_links(seeded, committed.operation_id, all_history=True)
    with seeded() as session, begin_immediate(session):
        row = session.get(OrderLine, ("SOR-SO-1001-2", "C-BLACK-L"))
        captured = {c.name: getattr(row, c.name) for c in OrderLine.__table__.columns}
        session.delete(row)
    assert kernel.inspect_downstream(seeded) == []
    report = audit(database_url, committed)
    assert report.status == "uncertain" and report.reason_code == "PLAN_HASH_UNVERIFIABLE"
    with seeded() as session, begin_immediate(session):
        session.add(OrderLine(**captured))
    report = audit(database_url, committed)
    assert report.status == "findings" and len(report.findings) == 4


def test_report_does_not_export_source_text_receipt_extras_or_secrets(
    seeded, committed, database_url
):
    secret = "PRIVATE-CANARY-NOT-FOR-REPORT"
    with seeded() as session, begin_immediate(session):
        op = session.get(Operation, committed.operation_id)
        op.idempotency_key = secret
        op.verification_lease_token = secret
        op.result_json = {**op.result_json, "private_extra": secret}
        plan = session.get(Plan, op.plan_id)
        plan.source_refs = [{"text_excerpt": secret}]
        plan.normalized_changes = {**plan.normalized_changes, "private_extra": secret}
    report = audit(database_url, committed)
    assert report.status == "consistent_for_operation"
    assert secret not in report.model_dump_json()


@pytest.mark.parametrize("fault", ["order", "revision", "sku", "operation", "duplicate", "extra"])
def test_exact_edge_identity_no_substitution_or_inferred_ownership(
    seeded, committed, database_url, fault
):
    with seeded() as session, begin_immediate(session):
        link = session.scalar(
            select(DownstreamLink).where(
                DownstreamLink.operation_id == committed.operation_id,
                DownstreamLink.doc_id == "MO-2002",
            )
        )
        if fault in {"duplicate", "extra"}:
            copied = {c.name: getattr(link, c.name) for c in DownstreamLink.__table__.columns}
            copied.update(id="synthetic-added-link", link_seq=link.link_seq + 1)
            if fault == "extra":
                copied["doc_id"] = "UNCLAIMED-DOCUMENT"
            session.add(DownstreamLink(**copied))
        else:
            setattr(
                link,
                {
                    "order": "order_id",
                    "revision": "order_revision",
                    "sku": "order_line_sku",
                    "operation": "operation_id",
                }[fault],
                {"order": "SO-1002", "revision": 1, "sku": "C-NAVY-L", "operation": None}[fault],
            )
    report = audit(database_url, committed)
    assert report.status == "findings" and len(report.findings) == 1
    assert report.findings[0].code == {
        "duplicate": "DUPLICATE_LINK",
        "extra": "UNEXPECTED_LINK",
        "operation": "EXPECTED_LINK_MISSING",
    }.get(fault, "LINK_IDENTITY_MISMATCH")


@pytest.mark.parametrize(
    "fault",
    [
        "plan_status",
        "plan_kind",
        "plan_owner",
        "plan_hash",
        "manifest_missing",
        "manifest_null",
        "manifest_removed_entry",
        "manifest_duplicate",
        "manifest_bad_type",
        "manifest_wrong_sku",
        "receipt_type",
        "receipt_revision_bool",
        "receipt_order",
        "receipt_plan",
        "receipt_task",
        "receipt_chain",
        "receipt_audit",
        "task_order",
        "task_result",
        "order_version",
        "revision_base",
        "approval_status",
        "approval_hash",
        "approval_intent",
        "approval_missing",
        "audit_hash",
        "audit_operation",
        "request_hash",
        "stock_missing",
        "stock_changed",
        "stock_extra",
        "operation_status",
        "lines_missing",
    ],
)
def test_damaged_or_unverifiable_evidence_is_uncertain_not_a_missing_link_claim(
    seeded, committed, database_url, fault
):
    with seeded() as session, begin_immediate(session):
        op = session.get(Operation, committed.operation_id)
        plan = session.get(Plan, op.plan_id)
        task = session.get(Task, plan.task_id)
        approval = session.scalar(select(Approval).where(Approval.consumed_operation_id == op.id))
        record = session.get(AuditRecord, op.result_json["audit_id"])
        revision = session.scalar(
            select(OrderRevision).where(
                OrderRevision.order_id == "SO-1001",
                OrderRevision.revision_no == 2,
            )
        )
        if fault.startswith("manifest_"):
            body = dict(plan.normalized_changes)
            items = body["downstream"]
            if fault == "manifest_missing":
                del body["downstream"]
            elif fault == "manifest_null":
                body["downstream"] = None
            elif fault == "manifest_removed_entry":
                body["downstream"] = items[1:]
            elif fault == "manifest_duplicate":
                body["downstream"] = items + [items[0]]
            elif fault == "manifest_bad_type":
                body["downstream"] = [{**items[0], "doc_type": "unknown"}, *items[1:]]
            else:
                body["downstream"] = [{**items[0], "order_line_sku": "C-NAVY-L"}, *items[1:]]
            plan.normalized_changes = body
        elif fault.startswith("receipt_"):
            if fault == "receipt_type":
                op.result_json = []
            else:
                field = fault.removeprefix("receipt_")
                key = "revision" if field == "revision_bool" else field + "_id"
                op.result_json = {
                    **op.result_json,
                    key: True if field == "revision_bool" else "wrong",
                }
        elif fault == "approval_missing":
            session.delete(approval)
        elif fault.startswith("stock_"):
            row = session.scalar(select(StockMovement).where(StockMovement.operation_id == op.id))
            if fault == "stock_missing":
                session.delete(row)
            elif fault == "stock_changed":
                row.delta_reserved -= 1
            else:
                values = {
                    c.name: getattr(row, c.key)
                    for c in StockMovement.__table__.columns
                    if c.name != "type"
                }
                session.add(
                    StockMovement(
                        **values
                        | {
                            "id": "extra-movement",
                            "seq": 999,
                            "movement_type": row.movement_type,
                        }
                    )
                )
        elif fault == "lines_missing":
            session.delete(session.get(OrderLine, (revision.id, "C-BLACK-L")))
        else:
            target, field, value = {
                "plan_status": (plan, "status", "APPROVED"),
                "plan_kind": (plan, "plan_kind", "cancel"),
                "plan_owner": (plan, "task_id", "wrong-task"),
                "plan_hash": (plan, "plan_hash", "sha256:" + "0" * 64),
                "task_order": (task, "order_id", "SO-1002"),
                "task_result": (task, "result_ref", None),
                "order_version": (session.get(SalesOrder, "SO-1001"), "current_revision", 1),
                "revision_base": (revision, "base_revision", 0),
                "approval_status": (approval, "status", "ACTIVE"),
                "approval_hash": (approval, "plan_hash", "wrong"),
                "approval_intent": (approval, "intent_revision", 999),
                "audit_hash": (record, "after_hash", "wrong"),
                "audit_operation": (record, "operation_id", None),
                "request_hash": (op, "request_hash", "wrong"),
                "operation_status": (op, "status", "FAILED"),
            }[fault]
            if fault == "plan_owner":
                # A real other task satisfies the FK while breaking the receipt.
                session.add(
                    Task(
                        id="wrong-task",
                        chain_id="other",
                        order_id="SO-1001",
                        status="SUCCEEDED",
                        intent_revision=1,
                    )
                )
                session.flush()
                value = "wrong-task"
            setattr(target, field, value)
    remove_links(seeded, committed.operation_id)
    report = audit(database_url, committed)
    assert report.status == "uncertain"
    assert report.reason_code and report.expected_link_count is None and report.findings == []


@pytest.mark.parametrize("fault", ["plan_deleted", "revision_deleted", "malformed_json"])
def test_external_loss_and_invalid_json_never_produce_a_partial_pass(
    seeded, committed, database_url, fault
):
    # Only this isolated synthetic database; simulate storage corruption below ORM/FK guards.
    with seeded() as session:
        plan_id = session.get(Operation, committed.operation_id).plan_id
    with sqlite3.connect(database_url.removeprefix("sqlite:///")) as db:
        if fault == "plan_deleted":
            db.execute("DELETE FROM plan WHERE id=?", (plan_id,))
        elif fault == "revision_deleted":
            db.execute(
                "DELETE FROM order_revision WHERE order_id=? AND revision_no=2", ("SO-1001",)
            )
        else:
            db.execute("UPDATE plan SET normalized_changes=? WHERE id=?", ("{broken", plan_id))
    assert audit(database_url, committed).status == "uncertain"


def test_missing_plan_can_only_recover_from_trusted_record_restoration(
    seeded, committed, database_url
):
    with seeded() as session:
        plan = session.get(Plan, session.get(Operation, committed.operation_id).plan_id)
        captured = {c.name: getattr(plan, c.name) for c in Plan.__table__.columns}
    with sqlite3.connect(database_url.removeprefix("sqlite:///")) as db:
        db.execute("DELETE FROM plan WHERE id=?", (captured["id"],))
    assert audit(database_url, committed).reason_code == "PLAN_MISSING"
    with seeded() as session, begin_immediate(session):
        session.add(Plan(**captured))
    assert audit(database_url, committed).status == "consistent_for_operation"


def test_no_retained_operation_cannot_establish_seed_or_unlinked_origin(seeded, database_url):
    report = link_audit.audit_operation_links(database_url, "no-operation")
    assert report.status == "uncertain" and report.reason_code == "OPERATION_MISSING"
    assert report.expected_link_count is None


def test_create_stock_order_has_verified_empty_manifest_only_for_that_operation(
    seeded, database_url
):
    plan = kernel.plan_new_order(
        seeded,
        customer_name="华东服饰",
        lines=[
            {
                "sku_id": "A-WHITE-M",
                "qty": 1,
            }
        ],
        client_event_key="new-audit",
        now=NOW,
    )
    result = approve_commit(seeded, plan)
    report = audit(database_url, result)
    assert report.status == "consistent_for_operation" and report.expected_link_count == 0


def test_supplement_creation_proves_new_purchase_edge(supplement, database_url):
    plan = supplement_support._plan(supplement, key="audit-supplement")
    result = approve_commit(supplement, plan, now=supplement_support.NOW)
    report = audit(database_url, result)
    assert report.status == "consistent_for_operation" and report.expected_link_count == 3
    with supplement() as session, begin_immediate(session):
        session.execute(
            delete(DownstreamLink).where(DownstreamLink.doc_id == supplement_support.PO_ID + "-S1")
        )
    report = audit(database_url, result)
    assert [f.doc_id for f in report.findings] == [supplement_support.PO_ID + "-S1"]


def test_sheet_create_hash_extension_and_missing_extension(
    sheet_env, session_factory, database_url
):
    from app.api.orders import commit as api_commit
    from schemas.api import CommitRequest

    request, _, out, _ = sheet_integrity.candidate(sheet_env, session_factory)
    approved = sheet.approve_current(request, out)
    result = api_commit(
        out.plan_id,
        CommitRequest(
            plan_hash=out.plan_hash, approval_id=approved.approval_id, idempotency_key="audit-sheet"
        ),
        request,
    )
    assert audit(database_url, result).status == "consistent_for_operation"
    with session_factory() as session, begin_immediate(session):
        plan = session.get(Plan, out.plan_id)
        plan.normalized_changes = {
            k: v for k, v in plan.normalized_changes.items() if k != "sheet_order_v1"
        }
    assert audit(database_url, result).reason_code == "PLAN_HASH_UNVERIFIABLE"


@pytest.mark.parametrize("initially_missing", [False, True])
def test_concurrent_mutation_is_seen_on_next_snapshot_not_mixed_into_old_report(
    seeded, committed, database_url, monkeypatch, initially_missing
):
    captured = remove_links(seeded, committed.operation_id) if initially_missing else None
    arrived, release = Event(), Event()
    original = link_audit._compare_links

    def pause(*args):
        arrived.set()
        assert release.wait(10)
        return original(*args)

    with monkeypatch.context() as patch:
        patch.setattr(link_audit, "_compare_links", pause)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                link_audit.audit_operation_links, database_url, committed.operation_id
            )
            try:
                assert arrived.wait(10)
                if captured:
                    with seeded() as session, begin_immediate(session):
                        session.add_all(DownstreamLink(**row) for row in captured)
                else:
                    remove_links(seeded, committed.operation_id)
            finally:
                release.set()
            old = future.result(timeout=10)
    new = audit(database_url, committed)
    assert old.status == ("findings" if initially_missing else "consistent_for_operation")
    assert new.status == ("consistent_for_operation" if initially_missing else "findings")


def test_database_connection_rejects_accidental_writes(
    seeded, committed, database_url, monkeypatch
):
    def accidental_write(session, operation_id):
        session.execute(text("DELETE FROM downstream_link"))
        raise AssertionError("read-only guard failed")

    monkeypatch.setattr(link_audit, "_project", accidental_write)
    assert audit(database_url, committed).status == "uncertain"


@pytest.mark.parametrize("limit", ["MAX_ROWS", "MAX_OUTPUT_BYTES"])
def test_limits_never_return_truncated_success(seeded, committed, database_url, monkeypatch, limit):
    monkeypatch.setattr(link_audit, limit, 1)
    assert audit(database_url, committed).reason_code == "EVIDENCE_LIMIT"


@pytest.mark.parametrize("state,expected_exit", [("good", 0), ("missing", 1), ("unknown", 2)])
def test_cli_has_scoped_exit_codes_and_bypasses_all_initialization(
    seeded, committed, database_url, monkeypatch, capsys, state, expected_exit
):
    if state == "missing":
        remove_links(seeded, committed.operation_id)

    def forbidden(*args):
        raise AssertionError("audit must not initialize, reconcile, or create directories")

    for name in ("ensure_sqlite_parent", "make_engine", "init_db", "configure_logging"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, database_url=database_url)
    )
    before = dump(database_url)
    capsys.readouterr()
    assert (
        cli.main(["audit-links", "unknown" if state == "unknown" else committed.operation_id])
        == expected_exit
    )
    report = json.loads(capsys.readouterr().out)
    assert not report["authorizes_business_action"]
    assert dump(database_url) == before


@pytest.mark.parametrize(
    "url",
    ["sqlite:///:memory:", "postgresql://localhost/db", "sqlite:///file:test?mode=rw&uri=true"],
)
def test_unsupported_database_urls_are_uncertain(url):
    assert link_audit.audit_operation_links(url, "OP").reason_code == "FILE_SQLITE_REQUIRED"


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "absent" / "db.sqlite"
    report = link_audit.audit_operation_links(f"sqlite:///{path}", "OP")
    assert report.status == "uncertain" and not path.parent.exists()


def test_file_uri_characters_and_unicode_are_literal(seeded, committed, database_url, tmp_path):
    path = tmp_path / "审计 # %.sqlite"
    with sqlite3.connect(database_url.removeprefix("sqlite:///")) as source:
        with sqlite3.connect(path) as destination:
            source.backup(destination)
    assert audit(f"sqlite:///{path}", committed).status == "consistent_for_operation"
