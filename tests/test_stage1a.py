"""Stage 1A kernel cases. 1B and 1C stay in the skip list."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from app.adapters.uploads import read_order_sheet
from app.config import Settings
from app.db.models import (
    Approval,
    Customer,
    DownstreamLink,
    Operation,
    OrderLine,
    OrderRevision,
    OutboxDelivery,
    Plan,
    PurchaseProposal,
    Reservation,
    SalesOrder,
    ShipmentRecord,
    Sku,
    StockBalance,
    StockMovement,
)
from app.db.schema import init_db
from app.db.session import make_engine, make_session_factory
from app.errors import AppError
from app.main import create_app
from app.services.kernel import (
    approve_plan,
    commit_failpoint,
    commit_plan,
    note_ambiguous,
    plan_change,
    plan_new_order,
    record_receipt,
    verify_failpoint,
)
from app.services.seed import load_seed
from fastapi.testclient import TestClient
from seeds.golden import WAREHOUSE_ID
from sqlalchemy import func, select

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture()
def kernel(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'kernel.db'}", busy_timeout_ms=5000)
    init_db(engine)
    factory = make_session_factory(engine)
    load_seed(factory, database_url=str(tmp_path / "kernel.db"))
    yield factory
    engine.dispose()


def _replace(factory, key: str):
    return plan_change(
        factory,
        order_id="SO-1001",
        base_revision=1,
        intent_revision=1,
        changes=[
            {
                "mode": "replace_remaining",
                "from_sku": "A-BLACK-M",
                "to_sku": "A-WHITE-M",
                "qty": 40,
            }
        ],
        client_event_key=key,
        now=NOW,
    )


def _approve(factory, outcome, when=NOW):
    return approve_plan(
        factory,
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
        now=when,
    )


def _lines(factory, order_id: str) -> dict[str, int]:
    session = factory()
    try:
        order = session.get(SalesOrder, order_id)
        revision = session.scalar(
            select(OrderRevision).where(
                OrderRevision.order_id == order_id,
                OrderRevision.revision_no == order.current_revision,
            )
        )
        rows = session.scalars(select(OrderLine).where(OrderLine.revision_id == revision.id)).all()
        return {row.sku_id: row.ordered_qty for row in rows}
    finally:
        session.close()


def _revision_count(factory, order_id: str = "SO-1001") -> int:
    session = factory()
    try:
        total = session.scalar(
            select(func.count())
            .select_from(OrderRevision)
            .where(OrderRevision.order_id == order_id)
        )
        return int(total or 0)
    finally:
        session.close()


def _balance(factory, sku: str) -> tuple[int, int, int]:
    session = factory()
    try:
        row = session.get(StockBalance, (WAREHOUSE_ID, sku))
        assert row is not None
        return row.on_hand, row.reserved, row.revision
    finally:
        session.close()


def test_B01_replace_remaining_black_to_white(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'b01.db'}"
    engine = make_engine(url)
    init_db(engine)
    load_seed(make_session_factory(engine), database_url=url)
    engine.dispose()
    app = create_app(Settings(_env_file=None, database_url=url))
    client = TestClient(app)
    created = client.post(
        "/plans",
        json={
            "order_id": "SO-1001",
            "base_revision": 1,
            "intent_revision": 1,
            "client_event_key": "b01",
            "changes": [
                {
                    "mode": "replace_remaining",
                    "from_sku": "A-BLACK-M",
                    "to_sku": "A-WHITE-M",
                    "qty": 40,
                }
            ],
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["requires_human_approval"] is True
    assert body["intent_revision"] == 2
    approved = client.post(
        f"/plans/{body['plan_id']}/approve",
        json={"plan_hash": body["plan_hash"], "expected_intent_revision": 2},
    )
    assert approved.status_code == 200
    committed = client.post(
        f"/plans/{body['plan_id']}/commit",
        json={
            "approval_id": approved.json()["approval_id"],
            "plan_hash": body["plan_hash"],
            "idempotency_key": "b01-commit",
        },
    )
    assert committed.status_code == 200
    assert committed.json()["revision"] == 2
    order = client.get("/orders/SO-1001")
    assert order.status_code == 200
    quantities = {line["sku_id"]: line for line in order.json()["lines"]}
    assert quantities["A-BLACK-M"]["ordered_qty"] == 60
    assert quantities["A-BLACK-M"]["shipped_qty"] == 60
    assert quantities["A-WHITE-M"]["ordered_qty"] == 40
    assert quantities["C-BLACK-L"]["ordered_qty"] == 300
    payload = client.get("/stock", params={"sku_ids": ["A-BLACK-M", "A-WHITE-M"]})
    stock = {row["sku_id"]: row for row in payload.json()["rows"]}
    black = stock["A-BLACK-M"]
    white = stock["A-WHITE-M"]
    assert (black["on_hand"], black["reserved"], black["available"]) == (140, 0, 140)
    assert (white["on_hand"], white["reserved"], white["available"]) == (120, 40, 80)
    factory = app.state.session_factory
    session = factory()
    try:
        assert session.scalar(select(func.count()).select_from(Operation)) == 1
        operation = session.scalars(select(Operation)).one()
        assert operation.committed_verification_pending is False
        links = list(session.scalars(select(DownstreamLink)).all())
        current: dict[tuple[str, str], DownstreamLink] = {}
        for link in links:
            key = (link.doc_type, link.doc_id)
            if key not in current or link.link_seq > current[key].link_seq:
                current[key] = link
        assert len(current) == 4
        assert all(link.order_revision == 2 for link in current.values())
    finally:
        session.close()
        app.state.engine.dispose()


def test_B02_ambiguous_increment_writes_nothing(kernel) -> None:
    outcome = _replace(kernel, "b02-plan")
    _approve(kernel, outcome)
    noted = note_ambiguous(
        kernel,
        order_id="SO-1001",
        client_event_key="b02-ambiguous",
        question="是给白色未发货加 20，还是把白色改成总共 20，或加到其他款？",
        now=NOW,
    )
    assert noted.status == "NEEDS_CLARIFICATION"
    assert noted.questions
    session = kernel()
    try:
        approval = session.scalars(select(Approval)).one()
        assert approval.status == "REVOKED"
        assert session.get(SalesOrder, "SO-1001").current_revision == 1
        assert session.scalar(select(func.count()).select_from(StockMovement)) == 6
        assert _lines(kernel, "SO-1001")["A-BLACK-M"] == 100
    finally:
        session.close()


def test_B03_increment_white_by_20(kernel) -> None:
    first = _replace(kernel, "b03-base")
    commit_plan(
        kernel,
        plan_id=first.plan_id,
        approval_id=_approve(kernel, first).approval_id,
        plan_hash=first.plan_hash,
        idempotency_key="b03-base",
        now=NOW,
    )
    outcome = plan_change(
        kernel,
        order_id="SO-1001",
        base_revision=2,
        intent_revision=2,
        changes=[{"mode": "increment", "sku_id": "A-WHITE-M", "qty": 20}],
        client_event_key="b03",
        now=NOW,
    )
    commit_plan(
        kernel,
        plan_id=outcome.plan_id,
        approval_id=_approve(kernel, outcome).approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="b03",
        now=NOW,
    )
    assert _lines(kernel, "SO-1001")["A-BLACK-M"] == 60
    assert _lines(kernel, "SO-1001")["A-WHITE-M"] == 60
    assert _balance(kernel, "A-WHITE-M")[:2] == (120, 60)


def test_B04_set_white_remaining_to_20(kernel) -> None:
    first = _replace(kernel, "b04-base")
    commit_plan(
        kernel,
        plan_id=first.plan_id,
        approval_id=_approve(kernel, first).approval_id,
        plan_hash=first.plan_hash,
        idempotency_key="b04-base",
        now=NOW,
    )
    outcome = plan_change(
        kernel,
        order_id="SO-1001",
        base_revision=2,
        intent_revision=2,
        changes=[{"mode": "set_total", "sku_id": "A-WHITE-M", "qty": 20}],
        client_event_key="b04",
        now=NOW,
    )
    commit_plan(
        kernel,
        plan_id=outcome.plan_id,
        approval_id=_approve(kernel, outcome).approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="b04",
        now=NOW,
    )
    assert _lines(kernel, "SO-1001")["A-BLACK-M"] == 60
    assert _lines(kernel, "SO-1001")["A-WHITE-M"] == 20
    assert _balance(kernel, "A-WHITE-M")[:2] == (120, 20)


def test_B05_cannot_change_shipped_black(kernel) -> None:
    with pytest.raises(AppError) as exc:
        plan_change(
            kernel,
            order_id="SO-1001",
            base_revision=1,
            intent_revision=1,
            changes=[
                {
                    "mode": "replace_remaining",
                    "from_sku": "A-BLACK-M",
                    "to_sku": "A-WHITE-M",
                    "qty": 100,
                }
            ],
            client_event_key="b05",
            now=NOW,
        )
    assert exc.value.code == "EXECUTED_PORTION_IMMUTABLE"
    assert exc.value.http_status == 422
    session = kernel()
    try:
        assert session.scalar(select(func.sum(ShipmentRecord.qty))) == 60
        assert _lines(kernel, "SO-1001") == {"A-BLACK-M": 100, "C-BLACK-L": 300}
    finally:
        session.close()


def test_B06_total_below_shipped_is_422(kernel) -> None:
    with pytest.raises(AppError) as exc:
        plan_change(
            kernel,
            order_id="SO-1001",
            base_revision=1,
            intent_revision=1,
            changes=[{"mode": "set_total", "sku_id": "A-BLACK-M", "qty": 50}],
            client_event_key="b06",
            now=NOW,
        )
    assert exc.value.http_status == 422
    assert _lines(kernel, "SO-1001")["A-BLACK-M"] == 100


def test_B07_shortage_is_proposal_only(kernel) -> None:
    with pytest.raises(AppError) as exc:
        plan_new_order(
            kernel,
            customer_name="华东服饰",
            lines=[{"sku_id": "B-BLUE-L", "qty": 25}],
            client_event_key="b07",
            now=NOW,
        )
    assert exc.value.code == "SHORTAGE"
    session = kernel()
    try:
        assert session.scalar(select(func.count()).select_from(SalesOrder)) == 2
        assert session.get(Reservation, ("SO-1002", WAREHOUSE_ID, "B-BLUE-L")).qty == 10
        proposal = session.scalars(select(PurchaseProposal)).one()
        assert proposal.sku_id == "B-BLUE-L"
        assert proposal.shortage_qty == 5
        assert _balance(kernel, "B-BLUE-L")[:2] == (30, 10)
    finally:
        session.close()


def test_B08_unknown_sku_lists_candidates_and_creates_nothing(kernel) -> None:
    outcome = plan_change(
        kernel,
        order_id="SO-1001",
        base_revision=1,
        intent_revision=1,
        changes=[
            {"mode": "replace_remaining", "from_sku": "A-BLACK-M", "to_sku": "A-GOLD-M", "qty": 10}
        ],
        client_event_key="b08-sku",
        now=NOW,
    )
    assert outcome.status == "NEEDS_CLARIFICATION"
    assert "A-BLACK-M" in outcome.candidates
    assert "A-WHITE-M" in outcome.candidates
    session = kernel()
    try:
        session.add(Customer(id="CUST-X", code="HX", display_name="华东服饰", active=True))
        session.commit()
    finally:
        session.close()
    named = plan_new_order(
        kernel,
        customer_name="华东服饰",
        lines=[{"sku_id": "A-WHITE-M", "qty": 1}],
        client_event_key="b08-name",
        now=NOW,
    )
    assert "HD" in named.candidates and "HX" in named.candidates
    session = kernel()
    try:
        assert session.scalar(select(func.count()).select_from(Sku)) == 5
        assert session.scalar(select(func.count()).select_from(Customer)) == 3
        assert session.scalar(select(func.count()).select_from(SalesOrder)) == 2
    finally:
        session.close()


def test_B09_same_idempotency_key_returns_the_same_operation(kernel) -> None:
    outcome = _replace(kernel, "b09")
    approval = _approve(kernel, outcome)
    first = commit_plan(
        kernel,
        plan_id=outcome.plan_id,
        approval_id=approval.approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="same-key",
        now=NOW,
    )
    second = commit_plan(
        kernel,
        plan_id=outcome.plan_id,
        approval_id=approval.approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="same-key",
        now=NOW,
    )
    assert first.operation_id == second.operation_id
    assert second.replayed is True
    session = kernel()
    try:
        assert _revision_count(kernel) == 2
        moves = session.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(StockMovement.operation_id == first.operation_id)
        )
        assert moves == 2
    finally:
        session.close()


def test_B10_same_key_different_body_conflicts(kernel) -> None:
    first = _replace(kernel, "b10-a")
    commit_plan(
        kernel,
        plan_id=first.plan_id,
        approval_id=_approve(kernel, first).approval_id,
        plan_hash=first.plan_hash,
        idempotency_key="shared",
        now=NOW,
    )
    second = plan_change(
        kernel,
        order_id="SO-1001",
        base_revision=2,
        intent_revision=2,
        changes=[{"mode": "increment", "sku_id": "A-WHITE-M", "qty": 1}],
        client_event_key="b10-b",
        now=NOW,
    )
    approved = _approve(kernel, second)
    with pytest.raises(AppError) as exc:
        commit_plan(
            kernel,
            plan_id=second.plan_id,
            approval_id=approved.approval_id,
            plan_hash=second.plan_hash,
            idempotency_key="shared",
            now=NOW,
        )
    assert exc.value.http_status == 409
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert _lines(kernel, "SO-1001")["A-WHITE-M"] == 40


def test_B11_old_approval_fails_after_new_intent(kernel) -> None:
    outcome = _replace(kernel, "b11")
    approval = _approve(kernel, outcome)
    note_ambiguous(
        kernel,
        order_id="SO-1001",
        client_event_key="b11-later",
        question="再加 20 指的是哪一款？",
        now=NOW,
    )
    with pytest.raises(AppError) as exc:
        commit_plan(
            kernel,
            plan_id=outcome.plan_id,
            approval_id=approval.approval_id,
            plan_hash=outcome.plan_hash,
            idempotency_key="b11",
            now=NOW,
        )
    assert exc.value.code == "STALE_INTENT"
    assert _lines(kernel, "SO-1001")["A-BLACK-M"] == 100


def test_B12_stock_revision_change_is_stale(kernel) -> None:
    outcome = _replace(kernel, "b12")
    approval = _approve(kernel, outcome)
    record_receipt(kernel, sku_id="A-WHITE-M", qty=1, now=NOW)
    with pytest.raises(AppError) as exc:
        commit_plan(
            kernel,
            plan_id=outcome.plan_id,
            approval_id=approval.approval_id,
            plan_hash=outcome.plan_hash,
            idempotency_key="b12",
            now=NOW,
        )
    assert exc.value.code == "STALE_DEPENDENCY"
    assert _balance(kernel, "A-WHITE-M")[:2] == (121, 0)
    assert _lines(kernel, "SO-1001")["A-BLACK-M"] == 100


def test_B13_tampered_plan_is_rejected(kernel) -> None:
    outcome = _replace(kernel, "b13")
    approval = _approve(kernel, outcome)
    session = kernel()
    try:
        plan = session.get(Plan, outcome.plan_id)
        blob = dict(plan.normalized_changes)
        changed = [dict(item) for item in blob["changes"]]
        changed[0]["qty"] = 10
        blob["changes"] = changed
        plan.normalized_changes = blob
        session.commit()
    finally:
        session.close()
    with pytest.raises(AppError) as exc:
        commit_plan(
            kernel,
            plan_id=outcome.plan_id,
            approval_id=approval.approval_id,
            plan_hash=outcome.plan_hash,
            idempotency_key="b13",
            now=NOW,
        )
    assert exc.value.code == "HASH_MISMATCH"
    assert _lines(kernel, "SO-1001") == {"A-BLACK-M": 100, "C-BLACK-L": 300}


def test_B14_expired_or_wrong_actor(kernel) -> None:
    outcome = _replace(kernel, "b14-actor")
    approval = _approve(kernel, outcome)
    with pytest.raises(AppError) as actor:
        commit_plan(
            kernel,
            plan_id=outcome.plan_id,
            approval_id=approval.approval_id,
            plan_hash=outcome.plan_hash,
            idempotency_key="b14-actor",
            actor_id="someone-else",
            now=NOW,
        )
    assert actor.value.http_status == 403
    session = kernel()
    try:
        current_intent = session.get(SalesOrder, "SO-1001").intent_revision
    finally:
        session.close()
    # _replace always sends intent 1. Build the expiry plan explicitly.
    expiry = plan_change(
        kernel,
        order_id="SO-1001",
        base_revision=1,
        intent_revision=current_intent,
        changes=[
            {"mode": "replace_remaining", "from_sku": "A-BLACK-M", "to_sku": "A-WHITE-M", "qty": 40}
        ],
        client_event_key="b14-expiry",
        now=NOW,
    )
    expiry_approval = _approve(kernel, expiry, when=NOW)
    with pytest.raises(AppError) as exc:
        commit_plan(
            kernel,
            plan_id=expiry.plan_id,
            approval_id=expiry_approval.approval_id,
            plan_hash=expiry.plan_hash,
            idempotency_key="b14-expiry",
            now=NOW + timedelta(seconds=601),
        )
    assert exc.value.code == "EXPIRED"
    assert _lines(kernel, "SO-1001")["A-BLACK-M"] == 100


def test_B15_forged_approved_flag_does_not_create_approval(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'b15.db'}"
    engine = make_engine(url)
    init_db(engine)
    load_seed(make_session_factory(engine), database_url=url)
    engine.dispose()
    app = create_app(Settings(_env_file=None, database_url=url))
    client = TestClient(app)
    response = client.post(
        "/plans",
        json={
            "order_id": "SO-1001",
            "base_revision": 1,
            "intent_revision": 1,
            "client_event_key": "b15",
            "approved": True,
            "changes": [
                {
                    "mode": "replace_remaining",
                    "from_sku": "A-BLACK-M",
                    "to_sku": "A-WHITE-M",
                    "qty": 40,
                }
            ],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    session = app.state.session_factory()
    try:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
    finally:
        session.close()
        app.state.engine.dispose()


def test_B16_two_orders_race_for_the_last_units(kernel) -> None:
    plans = []
    for index, name in enumerate(("华东服饰", "华南制衣")):
        outcome = plan_new_order(
            kernel,
            customer_name=name,
            lines=[{"sku_id": "B-BLUE-L", "qty": 15}],
            client_event_key=f"b16-{index}",
            now=NOW,
        )
        approval = _approve(kernel, outcome)
        plans.append((outcome, approval))
    barrier = threading.Barrier(2)
    results: list[tuple[str, object]] = []

    def run(outcome, approval) -> None:
        session = kernel()
        assert session.get(Plan, outcome.plan_id).base_order_revision == 0
        assert session.get(Approval, approval.approval_id).status == "ACTIVE"
        session.rollback()
        session.close()
        barrier.wait(timeout=5)
        try:
            results.append(
                (
                    "ok",
                    commit_plan(
                        kernel,
                        plan_id=outcome.plan_id,
                        approval_id=approval.approval_id,
                        plan_hash=outcome.plan_hash,
                        idempotency_key=f"b16-{outcome.plan_id}",
                        now=NOW,
                    ),
                )
            )
        except AppError as exc:
            results.append(("err", exc))

    threads = [threading.Thread(target=run, args=item) for item in plans]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(kind for kind, _ in results) == ["err", "ok"]
    error = next(item for kind, item in results if kind == "err")
    assert error.code == "STALE_DEPENDENCY"
    on_hand, reserved, _revision = _balance(kernel, "B-BLUE-L")
    assert on_hand == 30
    assert 0 <= reserved <= on_hand
    assert reserved == 25
    session = kernel()
    try:
        assert session.scalar(select(func.count()).select_from(SalesOrder)) == 3
    finally:
        session.close()


def test_B17_same_order_two_plans_one_commit(kernel) -> None:
    first = _replace(kernel, "b17-a")
    session = kernel()
    try:
        intent = session.get(SalesOrder, "SO-1001").intent_revision
    finally:
        session.close()
    second = plan_change(
        kernel,
        order_id="SO-1001",
        base_revision=1,
        intent_revision=intent,
        changes=[{"mode": "increment", "sku_id": "A-WHITE-M", "qty": 5}],
        client_event_key="b17-b",
        bump_intent=False,
        now=NOW,
    )
    approvals = [_approve(kernel, first), _approve(kernel, second)]
    barrier = threading.Barrier(2)
    results: list[tuple[str, object]] = []

    def run(outcome, approval, key: str) -> None:
        session = kernel()
        assert session.get(Plan, outcome.plan_id) is not None
        session.rollback()
        session.close()
        barrier.wait(timeout=5)
        try:
            results.append(
                (
                    "ok",
                    commit_plan(
                        kernel,
                        plan_id=outcome.plan_id,
                        approval_id=approval.approval_id,
                        plan_hash=outcome.plan_hash,
                        idempotency_key=key,
                        now=NOW,
                    ),
                )
            )
        except AppError as exc:
            results.append(("err", exc))

    pairs = [(first, approvals[0], "b17-a"), (second, approvals[1], "b17-b")]
    threads = [threading.Thread(target=run, args=item) for item in pairs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(kind for kind, _ in results) == ["err", "ok"]
    error = next(item for kind, item in results if kind == "err")
    assert error.http_status == 409
    assert _revision_count(kernel) == 2


def test_B18_failpoint_rolls_back_the_whole_commit(kernel) -> None:
    outcome = _replace(kernel, "b18")
    approval = _approve(kernel, outcome)
    token = commit_failpoint.set("after_revision")
    try:
        with pytest.raises(RuntimeError, match="failpoint"):
            commit_plan(
                kernel,
                plan_id=outcome.plan_id,
                approval_id=approval.approval_id,
                plan_hash=outcome.plan_hash,
                idempotency_key="b18",
                now=NOW,
            )
    finally:
        commit_failpoint.reset(token)
    session = kernel()
    try:
        assert _revision_count(kernel) == 1
        assert session.scalar(select(func.count()).select_from(OutboxDelivery)) == 0
        assert session.get(Approval, approval.approval_id).status == "ACTIVE"
        assert session.scalar(select(func.count()).select_from(StockMovement)) == 6
        assert session.scalar(select(func.count()).select_from(Operation)) == 0
    finally:
        session.close()


def test_B28_create_stock_order(kernel) -> None:
    outcome = plan_new_order(
        kernel,
        customer_name="华东服饰",
        lines=[{"sku_id": "A-WHITE-M", "qty": 30}],
        client_event_key="b28",
        now=NOW,
    )
    committed = commit_plan(
        kernel,
        plan_id=outcome.plan_id,
        approval_id=_approve(kernel, outcome).approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="b28",
        now=NOW,
    )
    assert committed.order_id == "SO-1003"
    assert committed.revision == 1
    assert _lines(kernel, "SO-1003") == {"A-WHITE-M": 30}
    assert _balance(kernel, "A-WHITE-M")[:2] == (120, 30)
    session = kernel()
    try:
        assert (
            session.scalar(
                select(func.count())
                .select_from(StockMovement)
                .where(StockMovement.operation_id == committed.operation_id)
            )
            == 1
        )
    finally:
        session.close()


def test_B29_xlsx_new_order_shortage_keeps_the_cell(kernel) -> None:
    from io import BytesIO

    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "订单"
    sheet.append(["客户", "款", "颜色", "尺码", "数量"])
    sheet.append(["华东服饰", "A", "白", "M", 30])
    sheet.append(["华东服饰", "B", "蓝", "L", 25])
    buffer = BytesIO()
    book.save(buffer)
    parsed = read_order_sheet(buffer.getvalue())
    assert parsed[1]["source_ref"]["cell_range"] == "A3:E3"
    with pytest.raises(AppError) as exc:
        plan_new_order(
            kernel,
            customer_name=str(parsed[0]["customer_name"]),
            lines=[
                {
                    "style": row["style"],
                    "color": row["color"],
                    "size": row["size"],
                    "qty": row["qty"],
                    "source_ref": row["source_ref"],
                }
                for row in parsed
            ],
            client_event_key="b29",
            now=NOW,
        )
    assert exc.value.code == "SHORTAGE"
    assert any(ref.get("cell_range") == "A3:E3" for ref in exc.value.details["source_refs"])
    session = kernel()
    try:
        assert session.scalar(select(func.count()).select_from(SalesOrder)) == 2
        assert session.get(Reservation, ("SO-1002", WAREHOUSE_ID, "B-BLUE-L")).qty == 10
        proposal = session.scalars(select(PurchaseProposal)).one()
        assert proposal.sku_id == "B-BLUE-L" and proposal.shortage_qty == 5
        assert _balance(kernel, "A-WHITE-M")[:2] == (120, 0)
    finally:
        session.close()


def test_B30_new_order_unknown_sku_or_ambiguous_customer(kernel) -> None:
    unknown = plan_new_order(
        kernel,
        customer_name="华东服饰",
        lines=[{"sku_id": "A-GOLD-M", "qty": 2}],
        client_event_key="b30-sku",
        now=NOW,
    )
    assert unknown.status == "NEEDS_CLARIFICATION"
    assert "A-BLACK-M" in unknown.candidates
    session = kernel()
    try:
        session.add(Customer(id="CUST-Y", code="HY", display_name="华东服饰", active=True))
        session.commit()
        before = session.scalar(select(func.count()).select_from(Customer))
    finally:
        session.close()
    named = plan_new_order(
        kernel,
        customer_name="华东服饰",
        lines=[{"sku_id": "A-WHITE-M", "qty": 2}],
        client_event_key="b30-name",
        now=NOW,
    )
    assert "HD" in named.candidates and "HY" in named.candidates
    session = kernel()
    try:
        assert session.scalar(select(func.count()).select_from(Customer)) == before
        assert session.get(Sku, "A-GOLD-M") is None
        assert session.scalar(select(func.count()).select_from(SalesOrder)) == 2
    finally:
        session.close()


def test_B31_idempotent_create_and_suspected_duplicate(kernel) -> None:
    outcome = plan_new_order(
        kernel,
        customer_name="华东服饰",
        lines=[{"sku_id": "A-WHITE-M", "qty": 30}],
        client_event_key="b31-first",
        now=NOW,
    )
    approval = _approve(kernel, outcome)
    first = commit_plan(
        kernel,
        plan_id=outcome.plan_id,
        approval_id=approval.approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="b31-key",
        now=NOW,
    )
    replay = commit_plan(
        kernel,
        plan_id=outcome.plan_id,
        approval_id=approval.approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="b31-key",
        now=NOW,
    )
    assert replay.order_id == first.order_id
    session = kernel()
    try:
        assert session.scalar(select(func.count()).select_from(SalesOrder)) == 3
    finally:
        session.close()
    duplicate = plan_new_order(
        kernel,
        customer_name="华东服饰",
        lines=[{"sku_id": "A-WHITE-M", "qty": 30}],
        client_event_key="b31-second",
        now=NOW + timedelta(hours=1),
    )
    assert duplicate.issues
    with pytest.raises(AppError) as exc:
        _approve(kernel, duplicate)
    assert exc.value.code == "DUPLICATE_UNCONFIRMED"
    confirmed = approve_plan(
        kernel,
        plan_id=duplicate.plan_id,
        plan_hash=duplicate.plan_hash,
        expected_intent_revision=duplicate.intent_revision,
        acknowledge_duplicate=True,
        now=NOW + timedelta(hours=1),
    )
    second = commit_plan(
        kernel,
        plan_id=duplicate.plan_id,
        approval_id=confirmed.approval_id,
        plan_hash=duplicate.plan_hash,
        idempotency_key="b31-other",
        now=NOW + timedelta(hours=1),
    )
    assert second.order_id != first.order_id
    session = kernel()
    try:
        assert session.scalar(select(func.count()).select_from(SalesOrder)) == 4
    finally:
        session.close()


def test_B32_new_order_stock_revision_is_stale(kernel) -> None:
    outcome = plan_new_order(
        kernel,
        customer_name="华东服饰",
        lines=[{"sku_id": "A-WHITE-M", "qty": 30}],
        client_event_key="b32",
        now=NOW,
    )
    approval = _approve(kernel, outcome)
    record_receipt(kernel, sku_id="A-WHITE-M", qty=1, now=NOW)
    with pytest.raises(AppError) as exc:
        commit_plan(
            kernel,
            plan_id=outcome.plan_id,
            approval_id=approval.approval_id,
            plan_hash=outcome.plan_hash,
            idempotency_key="b32",
            now=NOW,
        )
    assert exc.value.code == "STALE_DEPENDENCY"
    session = kernel()
    try:
        assert session.scalar(select(func.count()).select_from(SalesOrder)) == 2
    finally:
        session.close()


def test_post_commit_verification_failure_does_not_rewrite(kernel) -> None:
    outcome = _replace(kernel, "verify")
    approval = _approve(kernel, outcome)
    token = verify_failpoint.set(True)
    try:
        committed = commit_plan(
            kernel,
            plan_id=outcome.plan_id,
            approval_id=approval.approval_id,
            plan_hash=outcome.plan_hash,
            idempotency_key="verify",
            now=NOW,
        )
    finally:
        verify_failpoint.reset(token)
    session = kernel()
    try:
        operation = session.get(Operation, committed.operation_id)
        assert operation.committed_verification_pending is True
        assert session.get(SalesOrder, "SO-1001").current_revision == 2
        assert _revision_count(kernel) == 2
    finally:
        session.close()
