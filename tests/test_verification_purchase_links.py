"""Purchase snapshots and the limits of inferring lost order/document associations."""

import pytest
from app.config import Settings
from app.db.models import DocRevision, DownstreamLink, ProductionOrder, PurchaseOrder
from app.db.session import begin_immediate, make_engine, make_session_factory
from app.services import kernel as service
from app.services import verification
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from tests import test_verification_missing_snapshots as snapshots
from tests import test_verification_mto_missing_lines as support
from tests import test_verification_result_fencing as fencing
from tests.verification_scale import NOW

kernel = support.kernel
mto = support.mto
approved_commit = support.approved_commit


def current_snapshot(session, doc_id="BP-0-0"):
    row = session.get(PurchaseOrder, doc_id)
    return session.scalar(
        select(DocRevision).where(
            DocRevision.doc_type == "purchase_order",
            DocRevision.doc_id == doc_id,
            DocRevision.rev_no == row.doc_revision,
        )
    )


def corrupt(factory, fault):
    """Return exact captured synthetic facts; only these are restored in tests."""
    with factory() as session, begin_immediate(session):
        if fault == "snapshot":
            row = current_snapshot(session)
            saved = {c.name: getattr(row, c.name) for c in DocRevision.__table__.columns}
            session.delete(row)
            return (DocRevision, [saved])
        if fault == "content":
            row = current_snapshot(session)
            saved = dict(row.snapshot_json)
            row.snapshot_json = {**saved, "ordered_qty": 999}
            return (row.id, saved)
        doc_type, doc_id = (
            ("production_order", "BM-0") if fault == "mo_link" else ("purchase_order", "BP-0-0")
        )
        rows = list(
            session.scalars(
                select(DownstreamLink).where(
                    DownstreamLink.doc_type == doc_type,
                    DownstreamLink.doc_id == doc_id,
                )
            )
        )
        saved = [
            {c.name: getattr(row, c.name) for c in DownstreamLink.__table__.columns} for row in rows
        ]
        for row in rows:
            session.delete(row)
        return (DownstreamLink, saved)


def restore(factory, captured):
    kind, saved = captured
    with factory() as session, begin_immediate(session):
        if isinstance(kind, str):
            session.get(DocRevision, kind).snapshot_json = saved
        else:
            session.add_all(kind(**row) for row in saved)


@pytest.mark.parametrize("state", ["PLACED", "PARTIALLY_RECEIVED", "RECEIVED", "CANCELLED"])
def test_current_purchase_snapshot_is_required_in_all_lifecycle_states(mto, state):
    if state == "CANCELLED":
        snapshots.cancel(mto)
    else:
        support.execution(mto, 0, {"PLACED": 0, "PARTIALLY_RECEIVED": 1, "RECEIVED": 2}[state])
    assert service.inspect_downstream(mto) == []
    captured = corrupt(mto, "snapshot")
    before = support.facts(mto)
    report = service.inspect_downstream(mto)
    assert len(report) == 1
    assert report[0]["problem"] == f"BP-0-0 当前单据修订 {captured[1][0]['rev_no']} 缺失"
    assert report[0]["repair"] == {
        "action": "manual_review",
        "doc_type": "purchase_order",
        "doc_id": "BP-0-0",
        "doc_revision": captured[1][0]["rev_no"],
        "order_id": "BENCH-0",
    }
    assert support.facts(mto) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("material_id", "FAB-NAVY"),
        ("ordered_qty", 999),
        ("ordered_qty", None),
        ("ordered_qty", True),
    ],
)
def test_snapshot_must_agree_on_purchase_material_and_ordered_quantity(mto, field, value):
    with mto() as session, begin_immediate(session):
        row = current_snapshot(session)
        row.snapshot_json = {**row.snapshot_json, field: value}
    report = service.inspect_downstream(mto)
    assert [r["problem"] for r in report] == ["BP-0-0 当前采购内容与单据修订 3 不一致"]
    assert report[0]["repair"]["action"] == "manual_review"


def test_compensating_purchase_quantity_changes_cannot_hide_per_document_drift(mto):
    with mto() as session, begin_immediate(session):
        session.get(PurchaseOrder, "BP-0-0").ordered_qty = 3
        session.get(PurchaseOrder, "BP-0-1").ordered_qty = 1
    report = service.inspect_downstream(mto)
    assert {r["doc_id"] for r in report} == {"BP-0-0", "BP-0-1"}
    assert all("当前采购内容" in r["problem"] for r in report)


@pytest.mark.parametrize("body", [{}, ["not-a-snapshot"]])
def test_missing_fields_or_nonobject_purchase_snapshot_requires_review(mto, body):
    with mto() as session, begin_immediate(session):
        current_snapshot(session).snapshot_json = body
    assert [r["problem"] for r in service.inspect_downstream(mto)] == [
        "BP-0-0 当前采购内容与单据修订 3 不一致"
    ]


def test_boolean_snapshot_quantity_cannot_impersonate_one(mto):
    with mto() as session, begin_immediate(session):
        for doc_id, quantity in [("BP-0-0", 1), ("BP-0-1", 3)]:
            session.get(PurchaseOrder, doc_id).ordered_qty = quantity
            row = current_snapshot(session, doc_id)
            row.snapshot_json = {**row.snapshot_json, "ordered_qty": quantity}
        row = current_snapshot(session)
        row.snapshot_json = {**row.snapshot_json, "ordered_qty": True}
        flag_modified(row, "snapshot_json")  # Python otherwise considers True equal to 1.
    with mto() as session:
        assert current_snapshot(session).snapshot_json["ordered_qty"] is True
    assert [r["problem"] for r in service.inspect_downstream(mto)] == [
        "BP-0-0 当前采购内容与单据修订 3 不一致"
    ]


@pytest.mark.parametrize("substitute", ["old_revision", "wrong_type", "wrong_id"])
def test_history_or_another_document_cannot_replace_current_purchase_snapshot(mto, substitute):
    _, rows = corrupt(mto, "snapshot")
    saved = rows[0]
    with mto() as session, begin_immediate(session):
        if substitute == "old_revision":
            session.get(DocRevision, "BD-BP-0-0-2").snapshot_json = saved["snapshot_json"]
        else:
            replacement = {**saved, "id": "wrong-snapshot"}
            replacement["doc_type" if substitute == "wrong_type" else "doc_id"] = (
                "production_order" if substitute == "wrong_type" else "OTHER-PO"
            )
            session.add(DocRevision(**replacement))
    assert [r["problem"] for r in service.inspect_downstream(mto)] == ["BP-0-0 当前单据修订 3 缺失"]


def test_noncurrent_purchase_history_is_not_required_or_compared(mto):
    with mto() as session, begin_immediate(session):
        session.delete(session.get(DocRevision, "BD-BP-0-0-1"))
        session.get(DocRevision, "BD-BP-0-0-2").snapshot_json = {"ordered_qty": -999}
    assert service.inspect_downstream(mto) == []


@pytest.mark.parametrize("status", ["PLACED", "CANCELLED"])
def test_unlinked_purchase_is_not_assigned_to_an_order_from_its_parent_alone(mto, status):
    with mto() as session, begin_immediate(session):
        # Existing schema permits unlinked IDs. No origin/ownership manifest proves a lost link.
        session.add(
            PurchaseOrder(
                id="UNLINKED-PO",
                supplier_id="SUP-01",
                production_order_id="BM-0",
                material_id="FAB-BLACK",
                ordered_qty=7,
                received_qty=0,
                status=status,
                change_state="NONE",
                doc_revision=1,
            )
        )
    assert service.inspect_downstream(mto) == []


def test_production_and_purchase_ids_have_separate_snapshot_namespaces(mto):
    with mto() as session, begin_immediate(session):
        session.add(
            PurchaseOrder(
                id="BM-0",
                supplier_id="SUP-01",
                production_order_id="BM-0",
                material_id="FAB-BLACK",
                ordered_qty=0,
                received_qty=0,
                status="PLACED",
                change_state="NONE",
                doc_revision=3,
            )
        )
        session.add(
            DocRevision(
                id="same-id-po",
                doc_type="purchase_order",
                doc_id="BM-0",
                rev_no=3,
                snapshot_json={"material_id": "FAB-BLACK", "ordered_qty": 0},
            )
        )
        session.add(
            DownstreamLink(
                id="same-id-po-link",
                doc_type="purchase_order",
                doc_id="BM-0",
                order_id="BENCH-0",
                order_revision=2,
                order_line_sku=support.SKU,
                link_seq=1,
            )
        )
    assert service.inspect_downstream(mto) == []
    with mto() as session, begin_immediate(session):
        session.delete(session.get(DocRevision, "same-id-po"))
    report = service.inspect_downstream(mto)
    assert len(report) == 1 and report[0]["repair"]["doc_type"] == "purchase_order"


def commit_change(factory, revision, changes):
    plan = service.plan_change(
        factory,
        order_id="SO-1001",
        base_revision=revision,
        intent_revision=revision,
        changes=changes,
        client_event_key=f"link-history-{revision}",
        now=NOW,
    )
    assert plan.status == "VALIDATED"
    approval = service.approve_plan(
        factory,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
        now=NOW,
    )
    return service.commit_plan(
        factory,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        approval_id=approval.approval_id,
        idempotency_key=f"link-history-{revision}",
        now=NOW,
        acknowledge=True,
    )


def test_cancelled_links_remain_historical_across_later_stock_commits(kernel):
    commit_change(kernel, 1, [{"mode": "cancel_remaining", "sku_id": support.SKU}])
    before = support.facts(kernel)
    for revision in (2, 3):
        result = commit_change(
            kernel,
            revision,
            [
                {
                    "mode": "replace_remaining",
                    "from_sku": "A-BLACK-M",
                    "to_sku": "A-WHITE-M",
                    "qty": 1,
                }
            ],
        )
        assert service.inspect_downstream(kernel) == []
        assert not fencing.operation(kernel, result.operation_id)["committed_verification_pending"]
    after = support.facts(kernel)
    for name in ("production_order", "purchase_order", "doc_revision"):
        assert after[name] == before[name]
    with kernel() as session:
        assert (
            max(
                session.scalars(
                    select(DownstreamLink.order_revision).where(DownstreamLink.doc_id == "PUR-3002")
                )
            )
            == 2
        )
    # A legal old link does not excuse a missing current snapshot of the cancelled PO.
    with kernel() as session, begin_immediate(session):
        session.delete(current_snapshot(session, "PUR-3002"))
    assert [r["problem"] for r in service.inspect_downstream(kernel)] == [
        "PUR-3002 当前单据修订 2 缺失"
    ]


def test_status_flip_alone_cannot_exempt_stale_links_from_verification(mto):
    support.remove_line(mto)
    with mto() as session, begin_immediate(session):
        session.get(ProductionOrder, "BM-0").status = "CANCELLED"
        for i in range(2):
            session.get(PurchaseOrder, f"BP-0-{i}").status = "CANCELLED"
        for link in session.scalars(
            select(DownstreamLink).where(DownstreamLink.order_id == "BENCH-0")
        ):
            link.order_revision = 1
    report = service.inspect_downstream(mto)
    assert {r["doc_id"] for r in report} == {"BM-0", "BP-0-0", "BP-0-1"}
    assert all("当前取消状态" in r["problem"] for r in report)
    assert all(r["repair"]["action"] == "manual_review" for r in report)


@pytest.mark.parametrize("fault", ["snapshot", "content", "po_link", "mo_link"])
def test_proven_defects_keep_pending_and_recover_only_after_trusted_fact_restoration(
    mto, approved_commit, database_url, fault
):
    operation_id = fencing.pending(mto, approved_commit[1])
    captured = corrupt(mto, fault)
    before = support.facts(mto)
    service._post_verify(mto, operation_id)
    assert fencing.operation(mto, operation_id)["committed_verification_pending"]
    config = Settings(_env_file=None, verification_max_retries=1)
    assert verification.retry_pending(mto, operation_id, now=NOW, settings=config) == 1
    assert fencing.operation(mto, operation_id)["verification_manual_required"]
    assert support.facts(mto) == before
    engine = make_engine(database_url)
    restarted = make_session_factory(engine)
    try:
        assert not verification.verify_manually(restarted, operation_id, now=NOW, settings=config)
        assert support.facts(restarted) == before
        restore(restarted, captured)
        repaired = support.facts(restarted)
        assert verification.verify_manually(restarted, operation_id, now=NOW, settings=config)
        assert support.facts(restarted) == repaired
        assert fencing.operation(restarted, operation_id)["verification_retry_attempts"] == 1
    finally:
        engine.dispose()
