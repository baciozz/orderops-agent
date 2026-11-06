"""Synthetic production capture/readback, uncertainty, recovery and scope boundaries."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Event

import pytest
from app import cli
from app.config import Settings
from app.db.models import (
    Approval,
    AuditRecord,
    DownstreamLink,
    ExecutionEvidence,
    Operation,
    OrderLine,
    OrderRevision,
    Plan,
    Reservation,
    StockBalance,
    StockMovement,
)
from app.db.session import begin_immediate, make_engine, make_session_factory
from app.domain.hashing import canonical_hash
from app.errors import AppError
from app.services import execution_audit as audit
from app.services import kernel, link_audit
from sqlalchemy import delete, func, select
from tests import test_link_audit as support
from tests import test_link_manifest_design as design

seeded = support.seeded
supplement = support.supplement
sheet_env = support.sheet_env
NOW = support.NOW


def read(database_url, result):
    before = support.dump(database_url)
    report = audit.audit_execution(database_url, result.operation_id)
    assert support.dump(database_url) == before
    assert report.global_completeness == "not_assessed"
    assert report.parent_graph == "not_bound_by_approval"
    assert report.dependency_closure == "not_established"
    assert report.authorizes_business_action is False
    return report


def mutate_body(factory, operation_id, change, *, reseal=False):
    with factory() as session, begin_immediate(session):
        evidence = session.get(ExecutionEvidence, operation_id)
        body = deepcopy(evidence.body)
        change(body)
        evidence.body = body
        if reseal:
            evidence.digest = canonical_hash(body)
            op = session.get(Operation, operation_id)
            op.result_json = {
                **op.result_json,
                "execution_evidence": {"version": 1, "digest": evidence.digest},
            }


def test_capture_binds_actual_approval_and_reports_only_retained_fields(seeded, database_url):
    result = support.commit(seeded)
    report = read(database_url, result)
    assert report.status == "consistent_for_retained_fields"
    assert {f.field for f in report.fields} == {
        "revision_lines",
        "operation_links",
        "reservation_movements",
    }
    assert all(f.status == "consistent" for f in report.fields)
    assert "SO-1002" in report.observed_unrequested_order_ids
    assert any(s.reason_code == "SHARED_STOCK_CLOSURE_NOT_ESTABLISHED" for s in report.uncovered)
    with seeded() as session:
        evidence = session.get(ExecutionEvidence, result.operation_id)
        approved = evidence.body["approved_payload"]
        plan = session.get(Plan, result.plan_id)
        assert canonical_hash(approved) == plan.plan_hash
        assert evidence.digest == report.evidence_digest


def test_missing_current_line_and_all_links_remain_detectable(seeded, database_url):
    result = support.commit(seeded)
    design.erase_line_and_origin(seeded, result.revision)
    with seeded() as session:
        assert kernel._verification_problems(session) == []  # Existing global blind spot.
    with seeded() as session, begin_immediate(session):
        session.get(Operation, result.operation_id).committed_verification_pending = True
    kernel._post_verify(seeded, result.operation_id)
    with seeded() as session:
        # NEGATIVE CONTROL: this is an existing rollout blocker, not desired
        # behavior. A partial read-only report does not change the old verifier.
        assert not session.get(Operation, result.operation_id).committed_verification_pending
    assert link_audit.audit_operation_links(database_url, result.operation_id).status == "uncertain"
    report = read(database_url, result)
    fields = {f.field: f for f in report.fields}
    assert report.status == "findings"
    assert fields["revision_lines"].reason_codes == ["REVISION_LINES_MISMATCH"]
    assert "EXPECTED_LINK_MISSING" in fields["operation_links"].reason_codes


def test_new_approval_does_not_certify_already_unknown_legacy_parent(seeded, database_url):
    design.erase_line_and_origin(seeded, 1)
    result = support.commit(seeded)
    report = read(database_url, result)
    assert report.status == "consistent_for_retained_fields"
    assert any(s.reason_code == "PARENT_GRAPH_NOT_BOUND_BY_APPROVAL" for s in report.uncovered)
    assert report.fields[1].expected_count == 0


def test_recomputed_payload_survives_mutable_plan_json(seeded, database_url):
    request = design.approved(seeded)
    with seeded() as session, begin_immediate(session):
        plan = session.get(Plan, request["plan_id"])
        plan.normalized_changes = {**plan.normalized_changes, "downstream": []}
    result = kernel.commit_plan(seeded, **request)
    assert link_audit.audit_operation_links(database_url, result.operation_id).status == "uncertain"
    report = read(database_url, result)
    assert report.status == "consistent_for_retained_fields"
    assert next(f for f in report.fields if f.field == "operation_links").expected_count == 4


@pytest.mark.parametrize(
    "changes",
    [
        [{"mode": "cancel_remaining", "sku_id": "C-BLACK-L"}],
        [{"mode": "set_total", "sku_id": "C-BLACK-L", "qty": 210}],
        [{"mode": "replace_remaining", "from_sku": "C-BLACK-L", "to_sku": "C-NAVY-L", "qty": 180}],
        support.STOCK,
    ],
)
def test_cancellation_partial_tail_color_and_stock_history(seeded, database_url, changes):
    first = support.commit(seeded, changes=changes)
    second = support.commit(seeded, 2)
    for result in (first, second):
        assert read(database_url, result).status == "consistent_for_retained_fields"
    if changes[0]["mode"] == "cancel_remaining":
        with seeded() as session, begin_immediate(session):
            session.execute(
                delete(DownstreamLink).where(DownstreamLink.doc_id.in_(["MO-2002", "PUR-3002"]))
            )
        assert read(database_url, first).status == "findings"
        # Latest operation has no promised cancellation edges. It stays explicitly
        # limited: this success MUST NOT become a complete current-order claim.
        assert read(database_url, second).status == "consistent_for_retained_fields"


def test_create_has_explicit_empty_link_set_but_no_automatic_trusted_root(seeded, database_url):
    plan = kernel.plan_new_order(
        seeded,
        customer_name="华东服饰",
        lines=[{"sku_id": "A-WHITE-M", "qty": 1}],
        client_event_key="retained-create",
        now=NOW,
    )
    result = support.approve_commit(seeded, plan)
    report = read(database_url, result)
    assert report.status == "consistent_for_retained_fields"
    assert next(f for f in report.fields if f.field == "operation_links").expected_count == 0


def test_received_purchase_supplement_keeps_approved_identity(supplement, database_url):
    plan = support.supplement_support._plan(supplement, key="execution-supplement")
    result = support.approve_commit(supplement, plan, now=support.supplement_support.NOW)
    assert read(database_url, result).status == "consistent_for_retained_fields"
    with supplement() as session, begin_immediate(session):
        session.execute(
            delete(DownstreamLink).where(
                DownstreamLink.doc_id == support.supplement_support.PO_ID + "-S1"
            )
        )
    assert read(database_url, result).status == "findings"


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "digest",
        "version",
        "boolean_version",
        "payload_hash",
        "payload_schema",
        "missing_key",
    ],
)
def test_missing_corrupt_or_future_evidence_never_passes(seeded, database_url, fault):
    result = support.commit(seeded)
    if fault == "missing":
        with seeded() as session, begin_immediate(session):
            session.execute(delete(ExecutionEvidence))
    else:

        def change(body):
            if fault in {"version", "boolean_version"}:
                body["version"] = 2 if fault == "version" else True
            elif fault == "payload_hash":
                body["approved_payload"]["lines"][0]["ordered_qty"] += 1
            elif fault == "payload_schema":
                body["approved_payload"]["base_revision"] = True
            elif fault == "missing_key":
                del body["approved_payload"]["lines"]
            else:
                body["scope"] = "tampered"

        mutate_body(seeded, result.operation_id, change, reseal=fault != "digest")
    report = read(database_url, result)
    assert report.status == "uncertain" and report.fields == []


@pytest.mark.parametrize(
    "fault",
    [
        "approval_actor",
        "approval_status",
        "consumed_operation",
        "plan_hash",
        "audit_hash",
        "request_hash",
        "receipt_order",
    ],
)
def test_retained_hash_is_not_a_substitute_for_commit_attestation(seeded, database_url, fault):
    result = support.commit(seeded)
    with seeded() as session, begin_immediate(session):
        op = session.get(Operation, result.operation_id)
        evidence = session.get(ExecutionEvidence, result.operation_id)
        approval = session.get(Approval, evidence.body["approval_id"])
        plan = session.get(Plan, result.plan_id)
        if fault == "approval_actor":
            approval.actor_id = "someone-else"
        elif fault == "approval_status":
            approval.status = "REVOKED"
        elif fault == "consumed_operation":
            approval.consumed_operation_id = "another"
        elif fault == "plan_hash":
            plan.plan_hash = "changed"
        elif fault == "audit_hash":
            session.get(AuditRecord, result.audit_id).before_hash = "changed"
        elif fault == "request_hash":
            op.request_hash = "changed"
        else:
            op.result_json = {**op.result_json, "order_id": "SO-1002"}
    assert read(database_url, result).status == "uncertain"


@pytest.mark.parametrize(
    "fault", ["line_extra", "line_qty", "revision", "movement", "extra_link", "cross_order"]
)
def test_field_and_global_owner_conflicts(seeded, database_url, fault):
    result = support.commit(seeded)
    with seeded() as session, begin_immediate(session):
        revision = session.scalar(
            select(OrderRevision).where(
                OrderRevision.order_id == result.order_id,
                OrderRevision.revision_no == result.revision,
            )
        )
        if fault == "line_extra":
            session.add(
                OrderLine(
                    revision_id=revision.id,
                    sku_id="C-NAVY-L",
                    ordered_qty=1,
                    fulfillment_mode="MTO",
                )
            )
        elif fault == "line_qty":
            session.scalar(
                select(OrderLine).where(OrderLine.revision_id == revision.id)
            ).ordered_qty += 1
        elif fault == "revision":
            revision.base_revision = 0
        elif fault == "movement":
            session.execute(
                delete(StockMovement).where(StockMovement.operation_id == result.operation_id)
            )
        else:
            session.add(
                DownstreamLink(
                    id="unexpected",
                    doc_type="production_order",
                    doc_id="MO-2002",
                    order_id="SO-1002" if fault == "cross_order" else result.order_id,
                    order_revision=1,
                    order_line_sku="C-BLACK-L",
                    link_seq=999,
                    operation_id=None if fault == "cross_order" else result.operation_id,
                )
            )
    report = read(database_url, result)
    assert report.status == "findings"
    if fault == "cross_order":
        assert any(s.reason_code == "CROSS_ORDER_LINK_OBSERVED" for s in report.uncovered)
        assert any(
            d.observed_order_ids == ["SO-1001", "SO-1002"] for d in report.observed_dependencies
        )


def test_shared_stock_and_current_balance_are_explicitly_unverified(seeded, database_url):
    result = support.commit(seeded)
    with seeded() as session, begin_immediate(session):
        session.add(
            Reservation(
                order_id="SO-1002", warehouse_id="W1", sku_id="A-WHITE-M", qty=1, revision=1
            )
        )
        session.get(StockBalance, ("W1", "A-WHITE-M")).reserved += 1
    report = read(database_url, result)
    assert report.status == "consistent_for_retained_fields"
    stock = next(d for d in report.observed_dependencies if d.object_id == "W1:A-WHITE-M")
    assert "SO-1002" in stock.observed_order_ids
    assert stock.membership_completeness == "not_established"


@pytest.mark.parametrize(
    "stage", ["before_execution_evidence", "after_execution_evidence", "after_audit"]
)
def test_capture_failure_rolls_back_business_approval_receipt_and_evidence(
    seeded, database_url, stage
):
    request = design.approved(seeded)
    before = support.dump(database_url)
    token = kernel.commit_failpoint.set(stage)
    try:
        with pytest.raises(RuntimeError, match=stage):
            kernel.commit_plan(seeded, **request)
    finally:
        kernel.commit_failpoint.reset(token)
    assert support.dump(database_url) == before


def test_capture_validation_failure_is_before_business_effects(seeded, database_url, monkeypatch):
    request = design.approved(seeded)
    before = support.dump(database_url)

    def invalid(*args, **kwargs):
        raise audit._Uncertain("synthetic invalid payload")

    monkeypatch.setattr(audit, "_payload_valid", invalid)
    with pytest.raises(AppError) as error:
        kernel.commit_plan(seeded, **request)
    assert error.value.code == "EXECUTION_EVIDENCE_INVALID"
    assert support.dump(database_url) == before


def test_crash_after_commit_recovers_receipt_and_does_not_retain_twice(
    seeded, database_url, monkeypatch
):
    request = design.approved(seeded)

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic loss after commit")

    with monkeypatch.context() as patch:
        patch.setattr(kernel, "_post_verify", crash)
        with pytest.raises(RuntimeError, match="loss after commit"):
            kernel.commit_plan(seeded, **request)
    engine = make_engine(database_url)
    try:
        factory = make_session_factory(engine)
        replay = kernel.commit_plan(factory, **request)
        assert (
            replay.replayed
            and read(database_url, replay).status == "consistent_for_retained_fields"
        )
        before = support.dump(database_url)
        assert kernel.commit_plan(factory, **request).replayed
        assert support.dump(database_url) == before
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(ExecutionEvidence)) == 1
    finally:
        engine.dispose()


def test_same_key_concurrency_has_one_evidence_row(seeded, database_url):
    request = design.approved(seeded)
    barrier = Barrier(2)

    def run():
        barrier.wait(timeout=10)
        return kernel.commit_plan(seeded, **request)

    with ThreadPoolExecutor(2) as pool:
        outcomes = [f.result(timeout=15) for f in [pool.submit(run), pool.submit(run)]]
    assert sorted(r.replayed for r in outcomes) == [False, True]
    with seeded() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionEvidence)) == 1
    assert read(database_url, outcomes[0]).status == "consistent_for_retained_fields"


def test_missing_evidence_replay_keeps_committed_identity_and_never_reconstructs(
    seeded, database_url
):
    request = design.approved(seeded)
    result = kernel.commit_plan(seeded, **request)
    with seeded() as session, begin_immediate(session):
        session.execute(delete(ExecutionEvidence))
    before = support.dump(database_url)
    replay = kernel.commit_plan(seeded, **request)
    assert replay.replayed and replay.operation_id == result.operation_id
    assert support.dump(database_url) == before
    assert read(database_url, replay).reason_code == "EXECUTION_EVIDENCE_MISSING"


def test_exact_backup_restoration_recovers_readback_without_automatic_repair(seeded, database_url):
    result = support.commit(seeded)
    original = read(database_url, result)
    with seeded() as session:
        evidence = session.get(ExecutionEvidence, result.operation_id)
        body, digest = deepcopy(evidence.body), evidence.digest
    with seeded() as session, begin_immediate(session):
        session.execute(delete(ExecutionEvidence))
    assert read(database_url, result).status == "uncertain"
    with seeded() as session, begin_immediate(session):
        session.add(ExecutionEvidence(operation_id=result.operation_id, body=body, digest=digest))
    assert read(database_url, result) == original


def test_single_snapshot_is_an_observation_not_a_freshness_or_pending_claim(
    seeded, database_url, monkeypatch
):
    result = support.commit(seeded)
    ready, release = Event(), Event()
    original = audit._bound_payload

    def pause(session, operation):
        found = original(session, operation)
        ready.set()
        assert release.wait(10)
        return found

    with monkeypatch.context() as patch:
        patch.setattr(audit, "_bound_payload", pause)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(audit.audit_execution, database_url, result.operation_id)
            assert ready.wait(10)
            try:
                design.erase_line_and_origin(seeded, result.revision)
            finally:
                release.set()
            assert future.result(timeout=15).status == "consistent_for_retained_fields"
    assert read(database_url, result).status == "findings"


def test_cli_is_read_only_and_prints_coverage(seeded, database_url, monkeypatch, capsys):
    result = support.commit(seeded)
    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, database_url=database_url)
    )
    monkeypatch.setattr(cli, "init_db", lambda _: pytest.fail("audit must not migrate"))
    before = support.dump(database_url)
    assert cli.main(["audit-execution", result.operation_id]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["scope"] == "retained_execution_fields" and output["uncovered"]
    assert support.dump(database_url) == before


@pytest.mark.parametrize(
    "url,code",
    [
        ("sqlite:///:memory:", "FILE_SQLITE_REQUIRED"),
        ("sqlite:////tmp/never-created-execution-audit.db", "DATABASE_UNAVAILABLE"),
    ],
)
def test_invalid_database_never_initializes(url, code):
    assert audit.audit_execution(url, "OP-unknown").reason_code == code


def test_evidence_limit_does_not_return_truncated_success(seeded, database_url, monkeypatch):
    result = support.commit(seeded)
    monkeypatch.setattr(audit, "MAX_OUTPUT_BYTES", 10)
    assert read(database_url, result).status == "uncertain"


def test_retention_does_not_silently_truncate_or_apply_report_limits_to_business(seeded):
    result = support.commit(seeded)
    with seeded() as session:
        payload = deepcopy(
            session.get(ExecutionEvidence, result.operation_id).body["approved_payload"]
        )
    payload["lines"] = [
        {"sku_id": f"synthetic-{index}", "ordered_qty": 1, "fulfillment_mode": "MTO"}
        for index in range(audit.MAX_ROWS + 1)
    ]
    retained = audit.capture_payload(payload)
    assert retained == payload and retained is not payload
    with pytest.raises(audit._Uncertain, match="EVIDENCE_LIMIT"):
        audit._payload_valid(retained)


def test_unknown_operation_is_uncertain(seeded, database_url):
    assert audit.audit_execution(database_url, "OP-absent").reason_code == "OPERATION_MISSING"


def test_read_connection_rejects_accidental_write(seeded, database_url, monkeypatch):
    from sqlalchemy import text

    result = support.commit(seeded)
    original = audit._project

    def attempt(session, identity):
        from sqlalchemy.exc import OperationalError

        with pytest.raises(OperationalError, match="readonly"):
            session.execute(text("DELETE FROM execution_evidence"))
        return original(session, identity)

    monkeypatch.setattr(audit, "_project", attempt)
    assert read(database_url, result).status == "consistent_for_retained_fields"


def test_ambiguous_consumed_approval_is_not_accepted(seeded, database_url):
    result = support.commit(seeded)
    with seeded() as session, begin_immediate(session):
        evidence = session.get(ExecutionEvidence, result.operation_id)
        original = session.get(Approval, evidence.body["approval_id"])
        session.add(
            Approval(
                id="AP-duplicate",
                plan_id=original.plan_id,
                plan_hash=original.plan_hash,
                actor_id=original.actor_id,
                intent_revision=original.intent_revision,
                dependencies_hash=original.dependencies_hash,
                expires_at=original.expires_at,
                status="CONSUMED",
                consumed_operation_id=result.operation_id,
            )
        )
    assert read(database_url, result).reason_code == "CONSUMED_APPROVAL_MISSING_OR_AMBIGUOUS"


def test_sheet_hash_extension_is_retained_exactly(sheet_env, session_factory, database_url):
    from app.api.orders import commit as api_commit
    from schemas.api import CommitRequest

    request, _, outcome, _ = support.sheet_integrity.candidate(sheet_env, session_factory)
    approval = support.sheet.approve_current(request, outcome)
    result = api_commit(
        outcome.plan_id,
        CommitRequest(
            plan_hash=outcome.plan_hash,
            approval_id=approval.approval_id,
            idempotency_key="retained-sheet",
        ),
        request,
    )
    report = read(database_url, result)
    assert report.status == "consistent_for_retained_fields"
    with session_factory() as session:
        evidence = session.get(ExecutionEvidence, result.operation_id)
        assert "sheet_order_v1" in evidence.body["approved_payload"]
        assert canonical_hash(evidence.body["approved_payload"]) == outcome.plan_hash
