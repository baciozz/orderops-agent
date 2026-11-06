"""Stage 1C sub-agent dispatch. Fixture mode only. R18–R21 and the golden chains."""

from datetime import UTC, datetime

import pytest
from app.config import Settings
from app.db.models import (
    AuditRecord,
    DocRevision,
    DownstreamLink,
    JobAttempt,
    ModelUsage,
    Operation,
    OrderRevision,
    Plan,
    ProductionOrder,
    PurchaseOrder,
    SalesOrder,
    StockBalance,
)
from app.db.schema import init_db
from app.db.session import make_engine, make_session_factory
from app.errors import ProposalRejected
from app.main import create_app
from app.orchestration.agents import StepContext
from app.orchestration.defaults import doc_check_spec, parser_spec
from app.orchestration.dispatch import dispatch_text, receive_late_proposal
from app.orchestration.fixtures import (
    TEXT_ALL_NAVY,
    TEXT_BAD_SCHEMA,
    TEXT_BLACK_TO_WHITE,
    TEXT_NEW_ORDER,
    TEXT_ONLY_UNCUT,
)
from app.orchestration.registry import load_registry
from app.orchestration.tools import ToolGateway
from app.services.kernel import approve_plan, commit_plan
from app.services.seed import load_seed
from fastapi.testclient import TestClient
from schemas.common import UsageReport
from schemas.inventory import InventoryCheckOutput, SkuInventoryResult
from schemas.subagent import AgentProposal
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


def _client(tmp_path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'http.db'}"
    engine = make_engine(url)
    init_db(engine)
    load_seed(make_session_factory(engine), database_url=url)
    engine.dispose()
    return TestClient(
        base_url="http://127.0.0.1", app=create_app(Settings(_env_file=None, database_url=url))
    )


def test_R17_missing_capability_fails_the_task(kernel) -> None:
    registry = load_registry([parser_spec()])
    outcome = dispatch_text(
        kernel,
        text=TEXT_BLACK_TO_WHITE,
        client_event_key="r17-dispatch",
        registry=registry,
        now=NOW,
    )
    assert outcome.status == "FAILED"
    assert outcome.error_code == "CAPABILITY_MISSING"
    assert outcome.plan_id is None
    assert "CAPABILITY_MISSING" in " ".join(outcome.issues)
    assert "不能静默跳过" in " ".join(outcome.issues)
    names = [step.agent_id for step in outcome.steps]
    assert names == ["parser"]
    assert outcome.steps[0].status == "SUCCEEDED"
    session = kernel()
    try:
        assert session.get(SalesOrder, "SO-1001").current_revision == 1
        assert session.scalar(select(func.count()).select_from(Plan)) == 0
        assert session.scalar(select(func.count()).select_from(Operation)) == 0
    finally:
        session.close()


def test_R18_forbidden_tool_is_audited_and_writes_nothing(kernel) -> None:
    def probe(ctx: StepContext) -> None:
        ctx.call_tool("commit")

    outcome = dispatch_text(
        kernel,
        text=TEXT_BLACK_TO_WHITE,
        client_event_key="r18",
        now=NOW,
        before_step=probe,
    )
    assert outcome.status == "FAILED"
    assert outcome.error_code == "TOOL_NOT_ALLOWED"
    assert outcome.plan_id is None
    assert outcome.steps[0].status == "FAILED"
    assert outcome.steps[0].agent_id == "parser"
    session = kernel()
    try:
        audit = session.scalars(
            select(AuditRecord).where(AuditRecord.action == "tool.denied")
        ).one()
        assert audit.actor_type == "subagent"
        assert audit.actor_id == "parser"
        assert audit.object_id == outcome.task_id
        assert audit.source_refs == [{"tool": "commit"}]
        assert session.get(SalesOrder, "SO-1001").current_revision == 1
        assert session.scalar(select(func.count()).select_from(Plan)) == 0
        assert session.scalar(select(func.count()).select_from(Operation)) == 0
        assert session.scalar(select(func.count()).select_from(StockBalance)) == 3
    finally:
        session.close()


def test_R19_invalid_output_retries_then_skips_precheck(kernel) -> None:
    outcome = dispatch_text(
        kernel,
        text=TEXT_BAD_SCHEMA,
        client_event_key="r19",
        now=NOW,
    )
    assert outcome.status == "FAILED"
    assert outcome.error_code == "SCHEMA_INVALID"
    assert outcome.plan_id is None
    assert "重试" in " ".join(outcome.issues)
    limit = parser_spec().max_retries + 1
    assert len(outcome.steps) == limit
    assert all(step.agent_id == "parser" for step in outcome.steps)
    assert all(
        step.status == "FAILED" and step.error_code == "SCHEMA_INVALID" for step in outcome.steps
    )
    session = kernel()
    try:
        assert session.scalar(select(func.count()).select_from(Plan)) == 0
        assert session.get(SalesOrder, "SO-1001").current_revision == 1
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0
    finally:
        session.close()


def test_R20_timeout_discards_late_result(kernel) -> None:
    outcome = dispatch_text(
        kernel,
        text=TEXT_BLACK_TO_WHITE,
        client_event_key="r20",
        now=NOW,
        timeout_agent="parser",
    )
    assert outcome.status == "FAILED"
    assert outcome.steps[0].status == "TIMEOUT"
    assert outcome.plan_id is None
    session = kernel()
    try:
        attempt = session.scalar(select(JobAttempt).where(JobAttempt.task_id == outcome.task_id))
        assert attempt.fencing_token == 2
    finally:
        session.close()
    late = AgentProposal(
        agent_id="parser",
        agent_version="0.1.0",
        task_id=outcome.task_id,
        intent_revision=outcome.intent_revision,
        fencing_token=1,
        input_hash="sha256:late",
        payload={"late": True},
        usage=UsageReport(mode="FIXTURE"),
    )
    with pytest.raises(ProposalRejected) as rejected:
        receive_late_proposal(kernel, late, now=NOW)
    assert rejected.value.code == "PROPOSAL_REJECTED"
    session = kernel()
    try:
        assert session.scalar(select(func.count()).select_from(Plan)) == 0
        assert session.get(SalesOrder, "SO-1001").current_revision == 1
        discarded = session.scalars(
            select(AuditRecord).where(AuditRecord.action == "proposal.discarded")
        ).one()
        assert discarded.object_id == outcome.task_id
        assert discarded.before_hash == "1"
        assert discarded.after_hash == "2"
    finally:
        session.close()


def test_R21_change_trace_and_new_order_skips_doc_check(tmp_path) -> None:
    client = _client(tmp_path)
    change = client.post(
        "/dispatch",
        json={"text": TEXT_BLACK_TO_WHITE, "client_event_key": "r21-change"},
    )
    assert change.status_code == 200
    body = change.json()
    assert [step["agent_id"] for step in body["steps"]] == [
        "parser",
        "inventory-check",
        "doc-check",
    ]
    assert [step["status"] for step in body["steps"]] == ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]
    created = client.post(
        "/dispatch",
        json={"text": TEXT_NEW_ORDER, "client_event_key": "r21-new"},
    )
    assert created.status_code == 200
    new_body = created.json()
    assert new_body["requires_human_approval"] is True
    assert [step["agent_id"] for step in new_body["steps"]] == [
        "parser",
        "inventory-check",
        "doc-check",
    ]
    skipped = new_body["steps"][2]
    assert skipped["status"] == "SKIPPED"
    assert skipped["skip_reason"] == "无下游单据"
    assert skipped["capability"] == "check.downstream"
    assert doc_check_spec().agent_id == "doc-check"


def test_golden_stock_change_through_router(tmp_path) -> None:
    client = _client(tmp_path)
    created = client.post(
        "/dispatch",
        json={"text": TEXT_BLACK_TO_WHITE, "client_event_key": "golden-stock"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["requires_human_approval"] is True
    assert body["intent_revision"] == 2
    assert [step["agent_id"] for step in body["steps"]] == [
        "parser",
        "inventory-check",
        "doc-check",
    ]
    approved = client.post(
        f"/plans/{body['plan_id']}/approve",
        json={"plan_hash": body["plan_hash"], "expected_intent_revision": body["intent_revision"]},
    )
    assert approved.status_code == 200
    committed = client.post(
        f"/plans/{body['plan_id']}/commit",
        json={
            "approval_id": approved.json()["approval_id"],
            "plan_hash": body["plan_hash"],
            "idempotency_key": "golden-stock-commit",
        },
    )
    assert committed.status_code == 200
    assert committed.json()["revision"] == 2
    order = client.get("/orders/SO-1001")
    quantities = {line["sku_id"]: line for line in order.json()["lines"]}
    assert quantities["A-BLACK-M"]["ordered_qty"] == 60
    assert quantities["A-WHITE-M"]["ordered_qty"] == 40
    assert quantities["C-BLACK-L"]["ordered_qty"] == 300
    payload = client.get("/stock", params={"sku_ids": ["A-BLACK-M", "A-WHITE-M"]})
    stock = {row["sku_id"]: row for row in payload.json()["rows"]}
    assert (stock["A-BLACK-M"]["on_hand"], stock["A-BLACK-M"]["reserved"]) == (140, 0)
    assert (stock["A-WHITE-M"]["on_hand"], stock["A-WHITE-M"]["reserved"]) == (120, 40)
    factory = client.app.state.session_factory
    session = factory()
    try:
        operation = session.scalars(select(Operation)).one()
        assert operation.committed_verification_pending is False
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0
        plan = session.get(Plan, body["plan_id"])
        assert plan.task_id == body["task_id"]
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
        client.app.state.engine.dispose()


def test_golden_mto_clarify_then_only_uncut(tmp_path) -> None:
    client = _client(tmp_path)
    clarified = client.post(
        "/dispatch",
        json={"text": TEXT_ALL_NAVY, "client_event_key": "golden-navy"},
    )
    assert clarified.status_code == 200
    body = clarified.json()
    assert body["status"] == "NEEDS_CLARIFICATION"
    assert body["requires_human_approval"] is False
    text = " ".join(body["issues"])
    assert "MO-2001 EXECUTED_LOCKED" in text
    assert "PUR-3001 EXECUTED_LOCKED" in text
    assert "MO-2002 CHANGEABLE" in text
    assert "PUR-3002 CHANGEABLE" in text
    assert "180" in text
    assert [step["agent_id"] for step in body["steps"]] == [
        "parser",
        "inventory-check",
        "doc-check",
    ]
    factory = client.app.state.session_factory
    session = factory()
    try:
        assert session.get(SalesOrder, "SO-1001").current_revision == 1
        assert session.scalar(select(func.count()).select_from(Operation)) == 0
        assert session.scalar(select(func.count()).select_from(DocRevision)) == 4
    finally:
        session.close()
    follow = client.post(
        "/dispatch",
        json={"text": TEXT_ONLY_UNCUT, "client_event_key": "golden-uncut"},
    )
    assert follow.status_code == 200
    planned = follow.json()
    assert planned["requires_human_approval"] is True
    approved = client.post(
        f"/plans/{planned['plan_id']}/approve",
        json={
            "plan_hash": planned["plan_hash"],
            "expected_intent_revision": planned["intent_revision"],
        },
    )
    assert approved.status_code == 200
    committed = client.post(
        f"/plans/{planned['plan_id']}/commit",
        json={
            "approval_id": approved.json()["approval_id"],
            "plan_hash": planned["plan_hash"],
            "idempotency_key": "golden-uncut-commit",
        },
    )
    assert committed.status_code == 200
    assert committed.json()["revision"] == 2
    order = client.get("/orders/SO-1001")
    quantities = {line["sku_id"]: line["ordered_qty"] for line in order.json()["lines"]}
    assert quantities == {"A-BLACK-M": 100, "C-BLACK-L": 120, "C-NAVY-L": 180}
    session = factory()
    try:
        changed = session.get(ProductionOrder, "MO-2002")
        fabric = session.get(PurchaseOrder, "PUR-3002")
        assert (changed.sku_id, changed.planned_qty, changed.cut_qty, changed.doc_revision) == (
            "C-NAVY-L",
            180,
            0,
            2,
        )
        assert (
            fabric.material_id,
            fabric.ordered_qty,
            fabric.change_state,
            fabric.doc_revision,
        ) == (
            "FAB-NAVY",
            360,
            "PENDING_SUPPLIER",
            2,
        )
        assert session.get(ProductionOrder, "MO-2001").doc_revision == 1
        assert session.get(PurchaseOrder, "PUR-3001").doc_revision == 1
        links = list(session.scalars(select(DownstreamLink)).all())
        current: dict[str, DownstreamLink] = {}
        for link in links:
            kept = current.get(link.doc_id)
            if kept is None or link.link_seq > kept.link_seq:
                current[link.doc_id] = link
        assert set(current) == {"MO-2001", "MO-2002", "PUR-3001", "PUR-3002"}
        assert all(link.order_revision == 2 for link in current.values())
        black = session.get(StockBalance, (WAREHOUSE_ID, "A-BLACK-M"))
        white = session.get(StockBalance, (WAREHOUSE_ID, "A-WHITE-M"))
        assert (black.on_hand, black.reserved) == (140, 40)
        assert (white.on_hand, white.reserved) == (120, 0)
        operation = session.scalars(select(Operation)).one()
        assert operation.committed_verification_pending is False
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0
        revisions = session.scalar(
            select(func.count())
            .select_from(OrderRevision)
            .where(OrderRevision.order_id == "SO-1001")
        )
        assert revisions == 2
    finally:
        session.close()
        client.app.state.engine.dispose()


def test_kernel_recomputes_stock_when_the_agent_lies(
    kernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lied(self: ToolGateway, _arguments: dict[str, object]) -> dict[str, object]:
        return InventoryCheckOutput(
            lines=[
                SkuInventoryResult(
                    sku_id="A-WHITE-M",
                    on_hand=1,
                    reserved=0,
                    available=1,
                    stock_revision=99,
                    reservation_delta=40,
                    shortage_qty=0,
                    purchase_proposal=False,
                )
            ]
        ).model_dump(mode="json")

    monkeypatch.setattr(ToolGateway, "_inventory", lied)
    outcome = dispatch_text(
        kernel,
        text=TEXT_BLACK_TO_WHITE,
        client_event_key="lie",
        now=NOW,
    )
    assert outcome.requires_human_approval is True
    assert outcome.steps[1].status == "SUCCEEDED"
    session = kernel()
    try:
        plan = session.get(Plan, outcome.plan_id)
        assert 99 not in plan.expected_stock_revisions.values()
        balance = session.get(StockBalance, (WAREHOUSE_ID, "A-WHITE-M"))
        assert plan.expected_stock_revisions["A-WHITE-M"] == balance.revision
    finally:
        session.close()
    approval = approve_plan(
        kernel,
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
        now=NOW,
    )
    commit_plan(
        kernel,
        plan_id=outcome.plan_id,
        approval_id=approval.approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="lie-commit",
        now=NOW,
    )
    session = kernel()
    try:
        white = session.get(StockBalance, (WAREHOUSE_ID, "A-WHITE-M"))
        assert (white.on_hand, white.reserved) == (120, 40)
    finally:
        session.close()
