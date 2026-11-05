"""Split MTO allocations survive quantity-equivalent changes and real commits.

Call the real API route handlers and kernel without a server, browser, or IPC.
"""

from types import SimpleNamespace

import pytest
from app.api.orders import approve, commit, create_plan
from app.config import Settings
from app.db.models import DocRevision, DownstreamLink, Plan, ProductionOrder, PurchaseOrder
from app.domain.changes import SnapLine
from app.domain.downstream import assess_downstream
from app.services.kernel import consistency_problems, downstream_problems
from app.services.seed import load_seed
from schemas.api import ApproveRequest, CommitRequest, PlanChangeRequest
from sqlalchemy import select
from starlette.requests import Request


@pytest.fixture(scope="session", autouse=True)
def _sandbox_asyncio_polling():
    """These synchronous tests need no event loop or IPC probe."""
    yield


def snapshot(factory, models):
    with factory() as session:
        return {
            model.__tablename__: list(session.execute(select(model.__table__).order_by(model.id)))
            for model in models
        }


@pytest.fixture()
def split(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    with session_factory.begin() as session:
        session.get(ProductionOrder, "MO-2002").planned_qty = 90
        session.get(PurchaseOrder, "PUR-3002").ordered_qty = 180
        session.add(
            ProductionOrder(
                id="MO-2003",
                sku_id="C-BLACK-L",
                planned_qty=90,
                cut_qty=0,
                status="NOT_STARTED",
                doc_revision=1,
            )
        )
        session.flush()
        session.add(
            PurchaseOrder(
                id="PUR-3003",
                supplier_id="SUP-01",
                material_id="FAB-BLACK",
                production_order_id="MO-2003",
                ordered_qty=180,
                received_qty=0,
                status="PLACED",
                change_state="NONE",
                doc_revision=1,
            )
        )
        session.flush()
        for model, kind, fields in [
            (ProductionOrder, "production_order", ["sku_id", "planned_qty", "cut_qty", "status"]),
            (
                PurchaseOrder,
                "purchase_order",
                [
                    "material_id",
                    "production_order_id",
                    "ordered_qty",
                    "received_qty",
                    "status",
                    "change_state",
                ],
            ),
        ]:
            for row in session.scalars(select(model)):
                body = {field: getattr(row, field) for field in fields}
                rev = session.scalar(select(DocRevision).where(DocRevision.doc_id == row.id))
                if rev:
                    rev.snapshot_json = body
                else:
                    session.add(
                        DocRevision(
                            id=f"DR-{row.id}-1",
                            doc_type=kind,
                            doc_id=row.id,
                            rev_no=1,
                            snapshot_json=body,
                            operation_id=None,
                        )
                    )
                    session.add(
                        DownstreamLink(
                            id=f"LNK-{row.id}-1",
                            doc_type=kind,
                            doc_id=row.id,
                            order_id="SO-1001",
                            order_revision=1,
                            order_line_sku="C-BLACK-L",
                            link_seq=1,
                            operation_id=None,
                        )
                    )
    settings = Settings(_env_file=None, database_url=database_url, live_enabled=False)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(session_factory=session_factory, settings=settings)
            ),
            "state": {"request_id": "test", "correlation_id": "test"},
        }
    )
    return session_factory, request


def plan(request, changes):
    return create_plan(
        PlanChangeRequest.model_validate(
            {
                "order_id": "SO-1001",
                "base_revision": 1,
                "intent_revision": 1,
                "client_event_key": "split-preservation",
                "changes": changes,
            }
        ),
        request,
    )


def execute(request, preview):
    approval = approve(
        preview.plan_id,
        ApproveRequest(
            plan_hash=preview.plan_hash, expected_intent_revision=preview.intent_revision
        ),
        request,
    )
    return commit(
        preview.plan_id,
        CommitRequest(
            plan_hash=preview.plan_hash,
            approval_id=approval.approval_id,
            idempotency_key="split-commit",
        ),
        request,
    )


@pytest.mark.parametrize("receipts", [(0, 0), (180, 180), (180, 0), (40, 180), (40, 50)])
@pytest.mark.parametrize("net_zero", [False, True])
def test_api_plan_approval_commit_preserves_split_documents(split, receipts, net_zero):
    factory, request = split
    with factory.begin() as session:
        for doc_id, received in zip(["PUR-3002", "PUR-3003"], receipts, strict=True):
            row = session.get(PurchaseOrder, doc_id)
            row.received_qty = received
            row.status = (
                "RECEIVED" if received == 180 else "PARTIALLY_RECEIVED" if received else "PLACED"
            )
            rev = session.scalar(select(DocRevision).where(DocRevision.doc_id == doc_id))
            rev.snapshot_json = {
                **rev.snapshot_json,
                "received_qty": received,
                "status": row.status,
            }
    before = snapshot(factory, [ProductionOrder, PurchaseOrder, DocRevision])
    with factory() as session:
        assert consistency_problems(session) + downstream_problems(session) == []
    changes = [
        {"mode": "replace_remaining", "from_sku": "A-BLACK-M", "to_sku": "A-WHITE-M", "qty": 1}
    ]
    if net_zero:
        changes += [
            {"mode": "increment", "sku_id": "C-BLACK-L", "qty": 1},
            {"mode": "set_total", "sku_id": "C-BLACK-L", "qty": 300},
        ]
    preview = plan(request, changes)
    assert preview.status == "VALIDATED"
    assert preview.requires_human_approval
    assert snapshot(factory, [ProductionOrder, PurchaseOrder, DocRevision]) == before
    with factory() as session:
        verdicts = session.get(Plan, preview.plan_id).normalized_changes["downstream"]
        assert len(verdicts) == 6
        assert all(
            v["verdict"] == "UNAFFECTED_RELINK" and v["before"] == v["after"] for v in verdicts
        )
    assert execute(request, preview).revision == 2
    assert snapshot(factory, [ProductionOrder, PurchaseOrder, DocRevision]) == before
    with factory() as session:
        assert consistency_problems(session) + downstream_problems(session) == []
        links = list(
            session.scalars(select(DownstreamLink).where(DownstreamLink.order_revision == 2))
        )
        assert len(links) == 6


@pytest.mark.parametrize("qty,expected", [(90, "VALIDATED"), (181, "NEEDS_CLARIFICATION")])
def test_affected_split_color_change_keeps_legacy_guards(split, qty, expected):
    factory, request = split
    before = snapshot(factory, [ProductionOrder, PurchaseOrder, DocRevision])
    preview = plan(
        request,
        [{"mode": "replace_remaining", "from_sku": "C-BLACK-L", "to_sku": "C-NAVY-L", "qty": qty}],
    )
    assert preview.status == expected
    assert snapshot(factory, [ProductionOrder, PurchaseOrder, DocRevision]) == before
    if expected == "VALIDATED":
        assert execute(request, preview).revision == 2
        with factory() as session:
            assert session.get(ProductionOrder, "MO-2001").cut_qty == 120
            assert session.get(ProductionOrder, "MO-2003").sku_id == "C-NAVY-L"
            assert consistency_problems(session) + downstream_problems(session) == []


def mo(doc_id, sku, planned, cut=0, status="NOT_STARTED"):
    return dict(doc_id=doc_id, sku_id=sku, planned_qty=planned, cut_qty=cut, status=status)


def assess(rows, quantities, purchase=None, usage=None):
    return assess_downstream(
        new_lines={sku: SnapLine(sku, qty, "MTO") for sku, qty in quantities.items()},
        production=rows,
        purchase=purchase or [],
        usage=usage or {},
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_preserved_sku_reserved_before_changed_sku_allocation(reverse):
    rows = [
        mo("a", "same", 90),
        mo("b", "same", 90, 20, "PARTIALLY_CUT"),
        mo("c", "old", 40),
        mo("z", "same", 99, status="CANCELLED"),
    ]
    verdicts, error = assess(rows[::-1] if reverse else rows, {"same": 180, "new": 40})
    assert error is None
    by_id = {v["doc_id"]: v for v in verdicts}
    assert set(by_id) == {"a", "b", "c"}
    assert all(by_id[key]["verdict"] == "UNAFFECTED_RELINK" for key in ["a", "b"])
    assert by_id["c"]["after"]["sku_id"] == "new"


@pytest.mark.parametrize(
    "planned,cut,demand", [(90, 91, 90), (90, -1, 90), (-1, 0, 1), (90, 90, 89), (90, 40, 39)]
)
def test_inconsistent_or_executed_demand_fails_closed(planned, cut, demand):
    verdicts, error = assess([mo("a", "same", planned, cut)], {"same": demand})
    assert error
    assert verdicts == []


@pytest.mark.parametrize("qty", [120, 240])
def test_real_single_mo_quantity_change_stays_changeable(qty):
    verdicts, error = assess([mo("a", "same", 180)], {"same": qty})
    assert error is None
    assert verdicts[0]["verdict"] == "CHANGE_DRAFT"
    assert verdicts[0]["after"]["planned_qty"] == qty


def test_preservation_does_not_skip_purchase_usage_or_receipt_guards():
    rows = [mo("a", "same", 90), mo("b", "same", 90)]
    po = dict(
        doc_id="po",
        production_order_id="a",
        supplier_id="supplier",
        material_id="fabric",
        ordered_qty=180,
        received_qty=180,
        status="RECEIVED",
        change_state="NONE",
    )
    assert assess(rows, {"same": 180}, [po])[1] is not None
    assert assess(rows, {"same": 180}, [po], {"same": ("different", 2)})[1] is not None
    assert assess(rows, {"same": 180}, [po], {"same": ("fabric", 1)})[1] is not None
    verdicts, error = assess(rows, {"same": 180}, [po], {"same": ("fabric", 3)})
    assert error is None
    assert verdicts[-1]["before"] is None
    assert verdicts[-1]["after"]["ordered_qty"] == 90


def test_affected_received_split_still_blocks_material_change(split):
    factory, request = split
    with factory.begin() as session:
        for doc_id in ["PUR-3002", "PUR-3003"]:
            po = session.get(PurchaseOrder, doc_id)
            po.received_qty = po.ordered_qty
            po.status = "RECEIVED"
    before = snapshot(factory, [ProductionOrder, PurchaseOrder, DocRevision])
    preview = plan(
        request,
        [{"mode": "replace_remaining", "from_sku": "C-BLACK-L", "to_sku": "C-NAVY-L", "qty": 90}],
    )
    assert preview.status == "NEEDS_CLARIFICATION"
    assert snapshot(factory, [ProductionOrder, PurchaseOrder, DocRevision]) == before


def test_stock_only_with_production_shortfall_is_not_falsely_preserved(split):
    factory, request = split
    with factory.begin() as session:
        session.get(ProductionOrder, "MO-2003").planned_qty = 80
    preview = plan(
        request,
        [{"mode": "replace_remaining", "from_sku": "A-BLACK-M", "to_sku": "A-WHITE-M", "qty": 1}],
    )
    # This remains legacy affected-demand allocation, not a no-op bypass.
    assert preview.status == "VALIDATED"
    with factory() as session:
        verdicts = session.get(Plan, preview.plan_id).normalized_changes["downstream"]
        assert any(v["verdict"] != "UNAFFECTED_RELINK" for v in verdicts)


def test_cancel_and_unsupported_new_production_controls():
    verdicts, error = assess([mo("a", "old", 90)], {})
    assert error is None
    assert verdicts[0]["verdict"] == "CANCEL_DRAFT"
    verdicts, error = assess([mo("a", "same", 90)], {"same": 90, "new": 40})
    assert verdicts == []
    assert error == "P0 不支持为剩余数量追加生产单"
