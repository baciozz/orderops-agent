"""Qi's received-fabric decision: preserve receipts and draft only the shortfall."""

from datetime import UTC, datetime
from typing import Any

import pytest
from app.db.models import (
    DocRevision,
    DownstreamLink,
    Operation,
    OrderLine,
    OrderRevision,
    ProductionOrder,
    PurchaseOrder,
    SalesOrder,
)
from app.domain.changes import SnapLine
from app.domain.downstream import assess_downstream
from app.services.kernel import (
    approve_plan,
    commit_plan,
    consistency_problems,
    downstream_problems,
    plan_change,
)
from app.services.seed import load_seed
from sqlalchemy import func, select

NOW = datetime(2026, 11, 4, tzinfo=UTC)
ORDER_ID = "SO-QI-SUPPLEMENT"
MO_ID = "MO-QI-SUPPLEMENT"
PO_ID = "PUR-QI-RECEIVED"
SKU = "C-NAVY-L"
MATERIAL = "FAB-NAVY"
SUPPLIER = "SUP-01"


def _production(planned=15, cut=0) -> dict[str, Any]:
    return {
        "doc_id": MO_ID,
        "sku_id": SKU,
        "planned_qty": planned,
        "cut_qty": cut,
        "status": "PARTIALLY_CUT" if cut else "NOT_STARTED",
        "doc_revision": 1,
    }


def _purchase(doc_id=PO_ID, ordered=20, received=20, status="RECEIVED") -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "production_order_id": MO_ID,
        "supplier_id": SUPPLIER,
        "material_id": MATERIAL,
        "ordered_qty": ordered,
        "received_qty": received,
        "status": status,
        "change_state": "NONE",
        "doc_revision": 1,
        "order_line_sku": SKU,
    }


def _mo_body(row) -> dict[str, Any]:
    return {key: row[key] for key in ("sku_id", "planned_qty", "cut_qty", "status")}


def _po_body(row) -> dict[str, Any]:
    return {
        key: row[key]
        for key in ("material_id", "ordered_qty", "received_qty", "status", "change_state")
    }


def _supplement_verdict(doc_id=f"{PO_ID}-S1", qty=10, garment_sku=SKU) -> dict[str, Any]:
    # Ownership travels at the verdict's top level; `after` is the document snapshot.
    return {
        "doc_type": "purchase_order",
        "doc_id": doc_id,
        "verdict": "CHANGE_DRAFT",
        "before": None,
        "after": {
            "material_id": MATERIAL,
            "ordered_qty": qty,
            "received_qty": 0,
            "status": "PLACED",
            "change_state": "PENDING_SUPPLIER",
        },
        "production_order_id": MO_ID,
        "supplier_id": SUPPLIER,
        "order_line_sku": garment_sku,
    }


def _without_reason(verdict) -> dict[str, Any]:
    return {key: value for key, value in verdict.items() if key != "reason"}


@pytest.mark.parametrize("cut", [0, 3])
def test_received_po_is_relinked_and_shortfall_gets_its_own_draft(cut):
    # Demand fits the existing MO, so its planned quantity stays unchanged.
    production = _production(cut=cut)
    purchase = _purchase()
    verdicts, error = assess_downstream(
        new_lines={SKU: SnapLine(SKU, 15, "MTO", cut_qty=cut)},
        production=[production],
        purchase=[purchase],
        usage={SKU: (MATERIAL, 2)},
    )
    assert error is None, error
    assert sorted((_without_reason(v) for v in verdicts), key=lambda v: v["doc_id"]) == [
        {
            "doc_type": "production_order",
            "doc_id": MO_ID,
            "verdict": "UNAFFECTED_RELINK",
            "before": _mo_body(production),
            "after": _mo_body(production),
            "order_line_sku": SKU,
        },
        {
            "doc_type": "purchase_order",
            "doc_id": PO_ID,
            "verdict": "UNAFFECTED_RELINK",
            "before": _po_body(purchase),
            "after": _po_body(purchase),
            "order_line_sku": SKU,
        },
        _supplement_verdict(),
    ]


def test_reassigned_mo_with_higher_usage_of_same_fabric_gets_supplement():
    target_sku = "C-NAVY-HEAVY"
    production = _production(planned=10)
    purchase = _purchase()
    verdicts, error = assess_downstream(
        new_lines={target_sku: SnapLine(target_sku, 10, "MTO")},
        production=[production],
        purchase=[purchase],
        usage={SKU: (MATERIAL, 2), target_sku: (MATERIAL, 3)},
    )
    assert error is None, error
    # Same ten-piece MO, same fabric: the new SKU needs 30 metres rather than 20.
    assert sorted((_without_reason(v) for v in verdicts), key=lambda v: v["doc_id"]) == [
        {
            "doc_type": "production_order",
            "doc_id": MO_ID,
            "verdict": "CHANGE_DRAFT",
            "before": _mo_body(production),
            "after": {**_mo_body(production), "sku_id": target_sku},
            "order_line_sku": target_sku,
        },
        {
            "doc_type": "purchase_order",
            "doc_id": PO_ID,
            "verdict": "UNAFFECTED_RELINK",
            "before": _po_body(purchase),
            "after": _po_body(purchase),
            "order_line_sku": target_sku,
        },
        _supplement_verdict(garment_sku=target_sku),
    ]


@pytest.mark.parametrize("cut", [0, 3])
def test_demand_beyond_mo_planned_qty_expands_only_uncut_production(cut):
    verdicts, error = assess_downstream(
        new_lines={SKU: SnapLine(SKU, 16, "MTO", cut_qty=cut)},
        production=[_production(cut=cut)],
        purchase=[_purchase()],
        usage={SKU: (MATERIAL, 2)},
    )
    if cut:
        assert (verdicts, error) == ([], "P0 不支持为剩余数量追加生产单")
    else:
        assert error is None, error
        by_id = {v["doc_id"]: v for v in verdicts}
        assert by_id[MO_ID]["verdict"] == "CHANGE_DRAFT"
        assert by_id[MO_ID]["after"] == _mo_body(_production(planned=16))
        assert by_id[PO_ID]["verdict"] == "UNAFFECTED_RELINK"
        assert by_id[PO_ID]["before"] == by_id[PO_ID]["after"] == _po_body(_purchase())
        assert _without_reason(by_id[f"{PO_ID}-S1"]) == _supplement_verdict(qty=12)


@pytest.mark.parametrize("cancelled_suffix", [None, 2, 3])
def test_supplement_counts_existing_coverage_and_uses_next_free_suffix(cancelled_suffix):
    # The MO already has capacity here, isolating PO allocation from MO growth.
    purchases = [_purchase(), _purchase(f"{PO_ID}-S1", ordered=4, received=4)]
    if cancelled_suffix is not None:
        purchases.append(
            _purchase(f"{PO_ID}-S{cancelled_suffix}", ordered=100, received=0, status="CANCELLED")
        )
    verdicts, error = assess_downstream(
        new_lines={SKU: SnapLine(SKU, 15, "MTO")},
        production=[_production(planned=15)],
        purchase=purchases,
        usage={SKU: (MATERIAL, 2)},
    )
    assert error is None, error
    po_verdicts = {
        v["doc_id"]: _without_reason(v) for v in verdicts if v["doc_type"] == "purchase_order"
    }
    next_suffix = 3 if cancelled_suffix == 2 else 2
    expected_id = f"{PO_ID}-S{next_suffix}"
    assert set(po_verdicts) == {PO_ID, f"{PO_ID}-S1", expected_id}
    # 15 pieces * 2 metres - (20 received + 4 received) = 6, excluding cancellations.
    assert po_verdicts[expected_id] == _supplement_verdict(expected_id, qty=6)
    for row in purchases:
        if row["status"] != "CANCELLED":
            assert po_verdicts[row["doc_id"]] == {
                "doc_type": "purchase_order",
                "doc_id": row["doc_id"],
                "verdict": "UNAFFECTED_RELINK",
                "before": _po_body(row),
                "after": _po_body(row),
                "order_line_sku": SKU,
            }


def test_existing_supplement_covers_same_total_without_another_purchase_draft():
    verdicts, error = assess_downstream(
        new_lines={SKU: SnapLine(SKU, 15, "MTO")},
        production=[_production(planned=15)],
        purchase=[
            _purchase(),
            {
                **_purchase(f"{PO_ID}-S1", ordered=10, received=0, status="PLACED"),
                "change_state": "PENDING_SUPPLIER",
            },
        ],
        usage={SKU: (MATERIAL, 2)},
    )
    assert error is None, error
    assert {v["doc_id"] for v in verdicts} == {MO_ID, PO_ID, f"{PO_ID}-S1"}
    assert all(v["verdict"] == "UNAFFECTED_RELINK" for v in verdicts)


@pytest.mark.parametrize("received,status", [(0, "PLACED"), (8, "PARTIALLY_RECEIVED")])
def test_unreceived_or_partially_received_po_still_changes_in_place(received, status):
    # Demand fits the existing MO; an open PO still takes the quantity increase.
    purchase = _purchase(received=received, status=status)
    verdicts, error = assess_downstream(
        new_lines={SKU: SnapLine(SKU, 15, "MTO")},
        production=[_production(planned=15)],
        purchase=[purchase],
        usage={SKU: (MATERIAL, 2)},
    )
    assert error is None, error
    assert verdicts[0]["verdict"] == "UNAFFECTED_RELINK"
    assert [_without_reason(v) for v in verdicts if v["doc_type"] == "purchase_order"] == [
        {
            "doc_type": "purchase_order",
            "doc_id": PO_ID,
            "verdict": "CHANGE_DRAFT",
            "before": _po_body(purchase),
            "after": {**_po_body(purchase), "ordered_qty": 30, "change_state": "PENDING_SUPPLIER"},
            "order_line_sku": SKU,
        }
    ]


@pytest.fixture()
def kernel(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    revision_id = f"SOR-{ORDER_ID}-1"
    with session_factory() as session:
        session.add(
            SalesOrder(
                id=ORDER_ID,
                customer_id="CUST-HD",
                current_revision=1,
                intent_revision=1,
                status="OPEN",
            )
        )
        session.add(ProductionOrder(id=MO_ID, **_mo_body(_production()), doc_revision=1))
        session.flush()
        session.add(
            OrderRevision(
                id=revision_id,
                order_id=ORDER_ID,
                revision_no=1,
                base_revision=0,
                reason="合成测试：全收货后增加同面料需求",
                created_by="qi-test",
                created_at=NOW,
            )
        )
        session.add(
            PurchaseOrder(
                id=PO_ID,
                supplier_id=SUPPLIER,
                production_order_id=MO_ID,
                **_po_body(_purchase()),
                doc_revision=1,
            )
        )
        session.flush()
        session.add(
            OrderLine(revision_id=revision_id, sku_id=SKU, ordered_qty=10, fulfillment_mode="MTO")
        )
        for doc_type, doc_id, snapshot in [
            ("production_order", MO_ID, _mo_body(_production())),
            ("purchase_order", PO_ID, _po_body(_purchase())),
        ]:
            session.add(
                DocRevision(
                    id=f"DR-{doc_id}-1",
                    doc_type=doc_type,
                    doc_id=doc_id,
                    rev_no=1,
                    snapshot_json=snapshot,
                )
            )
            session.add(
                DownstreamLink(
                    id=f"LNK-{doc_id}-1",
                    doc_type=doc_type,
                    doc_id=doc_id,
                    order_id=ORDER_ID,
                    order_revision=1,
                    order_line_sku=SKU,
                    link_seq=1,
                )
            )
        session.commit()
        assert consistency_problems(session) == []
        # Deliberately out of sync: MO capacity is 15, the line is 10, and the
        # received PO covers only 10 pieces. The change must reconcile these
        # quantities through a supplement without growing the MO.
    return session_factory


def _plan(kernel, *, key, mode="set_total", base_revision=1, intent_revision=1, bump_intent=True):
    return plan_change(
        kernel,
        order_id=ORDER_ID,
        base_revision=base_revision,
        intent_revision=intent_revision,
        changes=[{"mode": mode, "sku_id": SKU, "qty": 5 if mode == "increment" else 15}],
        client_event_key=key,
        bump_intent=bump_intent,
        now=NOW,
    )


def _po_state(row) -> dict[str, Any]:
    return {
        key: getattr(row, key)
        for key in (
            "material_id",
            "ordered_qty",
            "received_qty",
            "status",
            "change_state",
            "doc_revision",
            "supplier_id",
            "production_order_id",
        )
    }


def _revisions(session, doc_id):
    return [
        (row.id, row.rev_no, row.snapshot_json, row.operation_id)
        for row in session.scalars(
            select(DocRevision)
            .where(DocRevision.doc_type == "purchase_order", DocRevision.doc_id == doc_id)
            .order_by(DocRevision.rev_no)
        )
    ]


def _counts(kernel):
    with kernel() as session:
        return {
            model.__name__: session.scalar(select(func.count()).select_from(model))
            for model in (PurchaseOrder, DocRevision, DownstreamLink, Operation, OrderRevision)
        }


def test_planning_twice_before_commit_keeps_supplement_id_and_quantity(kernel):
    # Different events force recomputation with identical dependencies, rather than
    # merely reading a cached plan; suppress the intentional new-event intent bump.
    before = _counts(kernel)
    first = _plan(kernel, key="qi-plan-first", bump_intent=False)
    second = _plan(kernel, key="qi-plan-second", bump_intent=False)
    assert (first.status, second.status) == ("VALIDATED", "VALIDATED"), (first, second)
    assert first.plan_id != second.plan_id
    assert first.plan_hash == second.plan_hash
    assert first.verdicts == second.verdicts
    mo_verdict = next(v for v in first.verdicts if v["doc_id"] == MO_ID)
    assert mo_verdict["verdict"] == "UNAFFECTED_RELINK"
    assert mo_verdict["before"] == mo_verdict["after"] == _mo_body(_production())
    supplements = [v for v in first.verdicts if v.get("before") is None]
    assert [_without_reason(v) for v in supplements] == [_supplement_verdict()]
    assert _counts(kernel) == before


@pytest.mark.parametrize("mode", ["increment", "set_total"])
def test_received_po_supplement_commits_verifies_and_is_idempotent(kernel, mode):
    with kernel() as session:
        received_before = _po_state(session.get(PurchaseOrder, PO_ID))
        revisions_before = _revisions(session, PO_ID)
    plan = _plan(kernel, key=f"qi-supplement-{mode}", mode=mode)
    assert plan.status == "VALIDATED", plan
    verdicts = {v["doc_id"]: v for v in plan.verdicts}
    assert verdicts[MO_ID]["verdict"] == "UNAFFECTED_RELINK"
    assert verdicts[MO_ID]["before"] == verdicts[MO_ID]["after"] == _mo_body(_production())
    assert verdicts[PO_ID]["verdict"] == "UNAFFECTED_RELINK"
    assert verdicts[PO_ID]["before"] == verdicts[PO_ID]["after"] == _po_body(_purchase())
    assert _without_reason(verdicts[f"{PO_ID}-S1"]) == _supplement_verdict()
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
        "idempotency_key": f"qi-supplement-commit-{mode}",
        "now": NOW,
    }
    committed = commit_plan(kernel, **request)
    assert committed.revision == 2
    assert committed.replayed is False
    with kernel() as session:
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
        for doc_id, expected_seq in [(PO_ID, 2), (supplement.id, 1)]:
            link = session.scalars(
                select(DownstreamLink)
                .where(DownstreamLink.doc_type == "purchase_order", DownstreamLink.doc_id == doc_id)
                .order_by(DownstreamLink.link_seq.desc())
            ).first()
            assert link is not None
            assert (link.order_id, link.order_revision, link.order_line_sku, link.link_seq) == (
                ORDER_ID,
                2,
                SKU,
                expected_seq,
            )
            assert link.operation_id == committed.operation_id
        operation = session.get(Operation, committed.operation_id)
        assert operation is not None
        assert operation.committed_verification_pending is False
        assert consistency_problems(session) == []
        assert downstream_problems(session) == []
        order = session.get(SalesOrder, ORDER_ID)
        assert order is not None
        current_intent = order.intent_revision
    before_replay = _counts(kernel)
    replay = commit_plan(kernel, **request)
    assert replay.replayed is True
    assert replay.operation_id == committed.operation_id
    assert replay.revision == committed.revision
    assert _counts(kernel) == before_replay

    same_total = _plan(
        kernel, key=f"qi-same-total-{mode}", base_revision=2, intent_revision=current_intent
    )
    assert same_total.status == "VALIDATED", same_total
    assert not any(
        v["doc_type"] == "purchase_order" and v["verdict"] == "CHANGE_DRAFT"
        for v in same_total.verdicts
    )
    assert all(v["verdict"] == "UNAFFECTED_RELINK" for v in same_total.verdicts)
    assert _counts(kernel) == before_replay
