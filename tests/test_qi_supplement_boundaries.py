"""Supplement allocation, concurrent creation and verification boundaries."""

import pytest
from app.db.models import Approval, DownstreamLink, Operation, PurchaseOrder, SalesOrder
from app.domain.changes import SnapLine
from app.domain.downstream import assess_downstream, locked_preview
from app.errors import AppError
from app.services.kernel import (
    approve_plan,
    commit_plan,
    downstream_problems,
    plan_change,
)
from tests.test_qi_received_po_supplement import (
    MATERIAL,
    MO_ID,
    NOW,
    ORDER_ID,
    PO_ID,
    SKU,
    SUPPLIER,
    _counts,
    _plan,
    _po_body,
    _production,
    _purchase,
)
from tests.test_qi_received_po_supplement import kernel as kernel


def _assess(qty=15, *, purchases=None, sku=SKU, material=MATERIAL, per_piece=2):
    return assess_downstream(
        new_lines={sku: SnapLine(sku, qty, "MTO")} if qty else {},
        production=[_production()],
        purchase=purchases
        if purchases is not None
        else [_purchase(), _purchase(f"{PO_ID}-S1", 10, 0, "PLACED")],
        usage={SKU: (MATERIAL, 2), sku: (material, per_piece)},
    )


@pytest.mark.parametrize(
    "qty,remaining,verdict", [(14, 8, "CHANGE_DRAFT"), (10, 10, "CANCEL_DRAFT")]
)
def test_reduction_uses_unreceived_supplement_and_preserves_received_po(qty, remaining, verdict):
    verdicts, error = _assess(qty)
    assert error is None
    by_id = {v["doc_id"]: v for v in verdicts}
    original = by_id[PO_ID]
    assert original["verdict"] == "UNAFFECTED_RELINK"
    assert original["before"] == original["after"] == _po_body(_purchase())
    supplement = by_id[f"{PO_ID}-S1"]
    assert supplement["verdict"] == verdict
    assert supplement["after"]["ordered_qty"] == remaining
    assert supplement["after"]["received_qty"] == 0


@pytest.mark.parametrize("qty", [0, 9])
def test_supplement_does_not_allow_reducing_or_cancelling_received_fabric(qty):
    verdicts, error = _assess(qty)
    assert verdicts == []
    assert error == (
        f"{PO_ID} 已到料，不能取消" if qty == 0 else f"{PO_ID} 已到料部分不能改面料或减量"
    )


def test_reduction_consumes_unreceived_supplement_before_partially_received_po():
    verdicts, error = _assess(
        9,
        purchases=[
            _purchase(ordered=20, received=8, status="PARTIALLY_RECEIVED"),
            _purchase(f"{PO_ID}-S1", 10, 0, "PLACED"),
        ],
    )
    assert error is None
    by_id = {v["doc_id"]: v for v in verdicts}
    assert by_id[f"{PO_ID}-S1"]["verdict"] == "CANCEL_DRAFT"
    assert by_id[PO_ID]["after"]["ordered_qty"] == 18
    assert by_id[PO_ID]["after"]["received_qty"] == 8


def test_two_open_purchases_share_one_material_requirement():
    verdicts, error = _assess(
        purchases=[
            _purchase(ordered=20, received=8, status="PARTIALLY_RECEIVED"),
            _purchase(f"{PO_ID}-S1", 10, 0, "PLACED"),
        ]
    )
    assert error is None
    assert all(v["verdict"] == "UNAFFECTED_RELINK" for v in verdicts)


def test_same_material_sku_change_increases_only_unreceived_supplement():
    verdicts, error = _assess(sku="C-NAVY-HEAVY", per_piece=3)
    assert error is None
    by_id = {v["doc_id"]: v for v in verdicts}
    assert by_id[PO_ID]["verdict"] == "UNAFFECTED_RELINK"
    assert by_id[PO_ID]["before"] == by_id[PO_ID]["after"]
    assert by_id[f"{PO_ID}-S1"]["after"]["ordered_qty"] == 25
    assert all(v["order_line_sku"] == "C-NAVY-HEAVY" for v in verdicts)
    assert not any(v["before"] is None for v in verdicts)


@pytest.mark.parametrize("sku,material,per_piece", [(SKU, MATERIAL, 1), ("BLACK", "FAB-BLACK", 2)])
def test_sku_change_cannot_replace_received_material_or_reduce_below_receipts(
    sku, material, per_piece
):
    assert _assess(sku=sku, material=material, per_piece=per_piece) == (
        [],
        f"{PO_ID} 已到料部分不能改面料或减量",
    )


def test_material_change_with_only_open_purchases_allocates_new_need_once():
    verdicts, error = _assess(
        sku="BLACK",
        material="FAB-BLACK",
        per_piece=3,
        purchases=[
            _purchase(ordered=20, received=0, status="PLACED"),
            _purchase(f"{PO_ID}-S1", 10, 0, "PLACED"),
        ],
    )
    assert error is None
    purchases = [v for v in verdicts if v["doc_type"] == "purchase_order"]
    assert sum(v["after"]["ordered_qty"] for v in purchases) == 45
    assert all(v["after"]["material_id"] == "FAB-BLACK" for v in purchases)
    assert next(v for v in purchases if v["doc_id"] == f"{PO_ID}-S1")["after"]["ordered_qty"] == 25


def test_preview_distinguishes_received_purchase_from_unreceived_supplement():
    preview = locked_preview(
        [_production()], [_purchase(), _purchase(f"{PO_ID}-S1", 10, 0, "PLACED")]
    )
    assert {v["doc_id"]: v["verdict"] for v in preview} == {
        MO_ID: "CHANGEABLE",
        PO_ID: "EXECUTED_LOCKED",
        f"{PO_ID}-S1": "CHANGEABLE",
    }


def _approve(kernel, plan):
    return approve_plan(
        kernel,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
        now=NOW,
    )


def _commit(kernel, plan, approval=None):
    approval = approval or _approve(kernel, plan)
    return commit_plan(
        kernel,
        plan_id=plan.plan_id,
        approval_id=approval.approval_id,
        plan_hash=plan.plan_hash,
        idempotency_key=f"commit-{plan.plan_id}",
        now=NOW,
    )


@pytest.mark.parametrize("linked", [False, True])
def test_supplement_id_created_after_approval_is_stale_and_never_overwritten(kernel, linked):
    plan = _plan(kernel, key="concurrent-supplement")
    approval = _approve(kernel, plan)
    doc_id = f"{PO_ID}-S1"
    with kernel() as session:
        session.add(
            PurchaseOrder(
                id=doc_id,
                supplier_id=SUPPLIER,
                production_order_id=MO_ID,
                **_po_body(_purchase(ordered=7, received=0, status="PLACED")),
                doc_revision=1,
            )
        )
        if linked:
            session.add(
                DownstreamLink(
                    id="LNK-CONCURRENT",
                    doc_type="purchase_order",
                    doc_id=doc_id,
                    order_id=ORDER_ID,
                    order_revision=1,
                    order_line_sku=SKU,
                    link_seq=1,
                )
            )
        session.commit()
    counts = _counts(kernel)
    with pytest.raises(AppError, match="下游单据版本或状态已变化") as exc:
        _commit(kernel, plan, approval)
    assert (exc.value.code, exc.value.http_status) == ("STALE_DEPENDENCY", 409)
    assert _counts(kernel) == counts
    with kernel() as session:
        assert session.get(Approval, approval.approval_id).status == "REVOKED"
        assert session.get(SalesOrder, ORDER_ID).current_revision == 1
        assert session.get(PurchaseOrder, doc_id).ordered_qty == 7


def test_existing_unlinked_supplement_id_is_reserved_at_planning(kernel):
    with kernel() as session:
        session.add(
            PurchaseOrder(
                id=f"{PO_ID}-S1",
                supplier_id=SUPPLIER,
                production_order_id=MO_ID,
                **_po_body(_purchase(ordered=7, received=0, status="CANCELLED")),
                doc_revision=1,
            )
        )
        session.commit()
    plan = _plan(kernel, key="reserved-supplement-id")
    assert [v["doc_id"] for v in plan.verdicts if v["before"] is None] == [f"{PO_ID}-S2"]
    committed = _commit(kernel, plan)
    with kernel() as session:
        assert session.get(PurchaseOrder, f"{PO_ID}-S1").ordered_qty == 7
        assert session.get(PurchaseOrder, f"{PO_ID}-S2").ordered_qty == 10
        assert (
            session.get(Operation, committed.operation_id).committed_verification_pending is False
        )


def test_followup_commits_relink_reduce_and_cancel_existing_supplement(kernel):
    _commit(kernel, _plan(kernel, key="first-supplement"))
    before = _counts(kernel)
    for revision, qty in [(2, 15), (3, 12), (4, 10)]:
        with kernel() as session:
            intent = session.get(SalesOrder, ORDER_ID).intent_revision
        plan = plan_change(
            kernel,
            order_id=ORDER_ID,
            base_revision=revision,
            intent_revision=intent,
            changes=[{"mode": "set_total", "sku_id": SKU, "qty": qty}],
            client_event_key=f"supplement-followup-{revision}",
            now=NOW,
        )
        assert plan.status == "VALIDATED"
        assert not any(v["before"] is None for v in plan.verdicts)
        committed = _commit(kernel, plan)
        with kernel() as session:
            assert downstream_problems(session) == []
            assert (
                session.get(Operation, committed.operation_id).committed_verification_pending
                is False
            )
            assert session.get(PurchaseOrder, PO_ID).doc_revision == 1
        if revision == 2:
            assert _counts(kernel)["DocRevision"] == before["DocRevision"]
    assert _counts(kernel)["PurchaseOrder"] == before["PurchaseOrder"]
    with kernel() as session:
        supplement = session.get(PurchaseOrder, f"{PO_ID}-S1")
        assert (supplement.ordered_qty, supplement.received_qty, supplement.status) == (
            4,
            0,
            "CANCELLED",
        )


def test_verification_does_not_offset_one_mos_shortfall_with_anothers_surplus(kernel):
    _commit(kernel, _plan(kernel, key="supplement-for-verification"))
    with kernel() as session:
        # The seeded SO-1001 has two MOs for FAB-BLACK. Preserve the order's
        # material total while shifting two metres between the MOs.
        first = session.get(PurchaseOrder, "PUR-3001")
        second = session.get(PurchaseOrder, "PUR-3002")
        first.ordered_qty -= 2
        first.received_qty -= 2
        second.ordered_qty += 2
        session.commit()
        problems = downstream_problems(session)
        assert len(problems) == 2
        assert any("MO-2001 面料采购" in problem for problem in problems)
        assert any("MO-2002 面料采购" in problem for problem in problems)
