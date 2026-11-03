"""Qi permits an active uncut MO to absorb an increase and draft a fabric supplement."""

from typing import Any

import pytest
from app.db.models import (
    DocRevision,
    DownstreamLink,
    Operation,
    OrderLine,
    ProductionOrder,
    PurchaseOrder,
    SalesOrder,
)
from app.domain.changes import SnapLine
from app.domain.downstream import assess_downstream
from app.services.kernel import approve_plan, commit_plan, consistency_problems, downstream_problems
from sqlalchemy import select
from tests import test_qi_received_po_supplement as supplement_tests
from tests.test_qi_received_po_supplement import (
    MATERIAL,
    MO_ID,
    NOW,
    ORDER_ID,
    PO_ID,
    SKU,
    SUPPLIER,
    _counts,
    _mo_body,
    _plan,
    _po_body,
    _po_state,
    _production,
    _purchase,
    _revisions,
    _supplement_verdict,
    _without_reason,
)

supplement_kernel = supplement_tests.kernel


def _expanded_mo_verdict() -> dict[str, Any]:
    return {
        "doc_type": "production_order",
        "doc_id": MO_ID,
        "verdict": "CHANGE_DRAFT",
        "before": _mo_body(_production(planned=10)),
        "after": _mo_body(_production(planned=15)),
        "order_line_sku": SKU,
    }


def _expansion_with_supplement() -> list[dict[str, Any]]:
    return [
        _expanded_mo_verdict(),
        {
            "doc_type": "purchase_order",
            "doc_id": PO_ID,
            "verdict": "UNAFFECTED_RELINK",
            "before": _po_body(_purchase()),
            "after": _po_body(_purchase()),
            "order_line_sku": SKU,
        },
        _supplement_verdict(),
    ]


def test_uncut_mo_expands_to_cover_same_sku_demand():
    verdicts, error = assess_downstream(
        new_lines={SKU: SnapLine(SKU, 15, "MTO")},
        production=[_production(planned=10)],
        purchase=[],
        usage={SKU: (MATERIAL, 2)},
    )
    assert error is None, error
    assert [_without_reason(v) for v in verdicts] == [_expanded_mo_verdict()]


def test_uncut_mo_expansion_relinks_received_po_and_supplements_only_shortfall():
    verdicts, error = assess_downstream(
        new_lines={SKU: SnapLine(SKU, 15, "MTO")},
        production=[_production(planned=10)],
        purchase=[_purchase()],
        usage={SKU: (MATERIAL, 2)},
    )
    assert error is None, error
    assert sorted((_without_reason(v) for v in verdicts), key=lambda v: v["doc_id"]) == (
        _expansion_with_supplement()
    )


@pytest.mark.parametrize("cut,status", [(3, "PARTIALLY_CUT"), (10, "CUT"), (0, "CANCELLED")])
def test_cut_or_cancelled_mo_cannot_expand_to_cover_increased_demand(cut, status):
    production = {**_production(planned=10, cut=cut), "status": status}
    verdicts, error = assess_downstream(
        new_lines={SKU: SnapLine(SKU, 15, "MTO", cut_qty=cut)},
        production=[production],
        purchase=[],
        usage={SKU: (MATERIAL, 2)},
    )
    assert error == "P0 不支持为剩余数量追加生产单"
    assert not any(
        v["doc_type"] == "production_order" and v["after"]["planned_qty"] > 10 for v in verdicts
    )
    assert verdicts == []


def test_uncut_mo_does_not_create_another_mo_for_leftover_sku_demand():
    verdicts, error = assess_downstream(
        new_lines={sku: SnapLine(sku, qty, "MTO") for sku, qty in {"A": 4, "B": 6}.items()},
        production=[{**_production(planned=10), "sku_id": "A"}],
        purchase=[],
        usage={"A": ("FAB-A", 2), "B": ("FAB-B", 3)},
    )
    assert (verdicts, error) == ([], "P0 不支持为剩余数量追加生产单")


@pytest.fixture()
def kernel(supplement_kernel):
    # Reuse the received-PO fixture with a consistent initial MO/line quantity of 10.
    with supplement_kernel() as session:
        production = session.get(ProductionOrder, MO_ID)
        assert production is not None
        production.planned_qty = 10
        revision = session.get(DocRevision, f"DR-{MO_ID}-1")
        assert revision is not None
        revision.snapshot_json = _mo_body(_production(planned=10))
        session.commit()
        line = session.scalar(select(OrderLine).where(OrderLine.revision_id == f"SOR-{ORDER_ID}-1"))
        assert line is not None
        assert line.ordered_qty == production.planned_qty == 10
        assert (production.cut_qty, production.status) == (0, "NOT_STARTED")
        assert _po_state(session.get(PurchaseOrder, PO_ID)) == {
            **_po_body(_purchase()),
            "supplier_id": SUPPLIER,
            "production_order_id": MO_ID,
            "doc_revision": 1,
        }
        assert consistency_problems(session) == []
        assert downstream_problems(session) == []
    return supplement_kernel


@pytest.mark.parametrize("mode", ["set_total", "increment"])
def test_uncut_mo_expansion_and_supplement_commit_verify_and_replay_once(kernel, mode):
    with kernel() as session:
        received_before = _po_state(session.get(PurchaseOrder, PO_ID))
        revisions_before = _revisions(session, PO_ID)
    before = _counts(kernel)
    plan = _plan(kernel, key=f"qi-expand-{mode}", mode=mode)
    assert plan.status == "VALIDATED", plan
    assert sorted((_without_reason(v) for v in plan.verdicts), key=lambda v: v["doc_id"]) == (
        _expansion_with_supplement()
    )
    assert _counts(kernel) == before
    approval = approve_plan(
        kernel,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
        now=NOW,
    )
    request = {
        "plan_id": plan.plan_id,
        "approval_id": approval.approval_id,
        "plan_hash": plan.plan_hash,
        "idempotency_key": f"qi-expand-commit-{mode}",
        "now": NOW,
    }
    committed = commit_plan(kernel, **request)
    assert committed.revision == 2
    assert committed.replayed is False
    assert _counts(kernel)["PurchaseOrder"] == before["PurchaseOrder"] + 1
    with kernel() as session:
        production = session.get(ProductionOrder, MO_ID)
        assert production is not None
        assert (
            production.sku_id,
            production.planned_qty,
            production.cut_qty,
            production.status,
        ) == (
            SKU,
            15,
            0,
            "NOT_STARTED",
        )
        assert production.doc_revision == 2
        revision = session.get(DocRevision, f"DR-{MO_ID}-2")
        assert revision is not None
        assert revision.snapshot_json == _expanded_mo_verdict()["after"]
        assert revision.operation_id == committed.operation_id
        assert session.get(SalesOrder, ORDER_ID).current_revision == 2
        line = session.scalar(select(OrderLine).where(OrderLine.revision_id == f"SOR-{ORDER_ID}-2"))
        assert line is not None
        assert (line.sku_id, line.ordered_qty, line.fulfillment_mode) == (SKU, 15, "MTO")
        assert _po_state(session.get(PurchaseOrder, PO_ID)) == received_before
        assert _revisions(session, PO_ID) == revisions_before
        supplement = session.get(PurchaseOrder, f"{PO_ID}-S1")
        assert supplement is not None
        assert _po_state(supplement) == {
            **_supplement_verdict()["after"],
            "supplier_id": SUPPLIER,
            "production_order_id": MO_ID,
            "doc_revision": 1,
        }
        revisions = _revisions(session, supplement.id)
        assert len(revisions) == 1
        assert revisions[0][1:] == (1, _supplement_verdict()["after"], committed.operation_id)
        for doc_type, doc_id, seq in [
            ("production_order", MO_ID, 2),
            ("purchase_order", PO_ID, 2),
            ("purchase_order", supplement.id, 1),
        ]:
            link = session.scalars(
                select(DownstreamLink)
                .where(DownstreamLink.doc_type == doc_type, DownstreamLink.doc_id == doc_id)
                .order_by(DownstreamLink.link_seq.desc())
            ).first()
            assert link is not None
            assert (link.order_id, link.order_revision, link.order_line_sku, link.link_seq) == (
                ORDER_ID,
                2,
                SKU,
                seq,
            )
            assert link.operation_id == committed.operation_id
        operation = session.get(Operation, committed.operation_id)
        assert operation is not None
        assert operation.committed_verification_pending is False
        assert consistency_problems(session) == []
        assert downstream_problems(session) == []
    before_replay = _counts(kernel)
    replay = commit_plan(kernel, **request)
    assert replay.replayed is True
    assert (replay.operation_id, replay.revision) == (committed.operation_id, committed.revision)
    assert _counts(kernel) == before_replay
