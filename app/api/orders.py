"""Order, stock, plan, approval, and commit routes for the stage 1A kernel.

The actor is the local demo identity. It is not read from the request body.
"""

from fastapi import APIRouter, Request
from schemas.api import (
    ApproveRequest,
    ApproveResponse,
    CommitRequest,
    CommitSuccess,
    OrderGetResponse,
    OrderLineView,
    PlanChangeRequest,
    PlanPreviewResponse,
    StockResponse,
    StockRow,
)
from sqlalchemy import func, select

from app.db.models import (
    OrderLine,
    OrderRevision,
    SalesOrder,
    ShipmentRecord,
    StockBalance,
)
from app.errors import AppError
from app.orchestration.recovery import durable_commit as commit_plan
from app.services.kernel import (
    DEMO_ACTOR_ID,
    WAREHOUSE_ID,
    approve_plan,
    cut_by_sku,
    inspect_downstream,
    plan_change,
)

router = APIRouter()


def _refs(body: PlanChangeRequest) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in body.source_refs]


def _changes(body: PlanChangeRequest) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in body.changes]


@router.post("/plans", response_model=PlanPreviewResponse)
def create_plan(body: PlanChangeRequest, request: Request) -> PlanPreviewResponse:
    outcome = plan_change(
        request.app.state.session_factory,
        order_id=body.order_id,
        base_revision=body.base_revision,
        intent_revision=body.intent_revision,
        changes=_changes(body),
        source_refs=_refs(body),
        client_event_key=body.client_event_key or f"plan-{body.order_id}-{body.intent_revision}",
    )
    return PlanPreviewResponse(
        request_id=request.state.request_id,
        correlation_id=request.state.correlation_id,
        task_id=outcome.task_id,
        chain_id=outcome.chain_id,
        status=outcome.status,
        plan_id=outcome.plan_id,
        base_revision=outcome.base_revision,
        intent_revision=outcome.intent_revision,
        plan_hash=outcome.plan_hash,
        stock_revisions=outcome.stock_revisions,
        requires_human_approval=outcome.requires_human_approval,
        issues=outcome.issues or outcome.questions,
    )


@router.post("/plans/{plan_id}/approve", response_model=ApproveResponse)
def approve(plan_id: str, body: ApproveRequest, request: Request) -> ApproveResponse:
    outcome = approve_plan(
        request.app.state.session_factory,
        plan_id=plan_id,
        plan_hash=body.plan_hash,
        expected_intent_revision=body.expected_intent_revision,
        actor_id=DEMO_ACTOR_ID,
        acknowledge_duplicate=body.acknowledge_duplicate,
        ttl_seconds=request.app.state.settings.approval_ttl_seconds,
    )
    return ApproveResponse(
        request_id=request.state.request_id,
        correlation_id=request.state.correlation_id,
        approval_id=outcome.approval_id,
        status="ACTIVE",
        expires_at=outcome.expires_at.isoformat(),
    )


@router.post("/plans/{plan_id}/commit", response_model=CommitSuccess)
def commit(plan_id: str, body: CommitRequest, request: Request) -> CommitSuccess:
    outcome = commit_plan(
        request.app.state.session_factory,
        plan_id=plan_id,
        approval_id=body.approval_id,
        plan_hash=body.plan_hash,
        idempotency_key=body.idempotency_key,
        actor_id=DEMO_ACTOR_ID,
    )
    return CommitSuccess(
        request_id=request.state.request_id,
        correlation_id=request.state.correlation_id,
        task_id=outcome.task_id,
        chain_id=outcome.chain_id,
        status="SUCCEEDED",
        operation_id=outcome.operation_id,
        order_id=outcome.order_id,
        revision=outcome.revision,
        audit_id=outcome.audit_id,
    )


@router.get("/orders/{order_id}", response_model=OrderGetResponse)
def get_order(order_id: str, request: Request) -> OrderGetResponse:
    session = request.app.state.session_factory()
    try:
        order = session.get(SalesOrder, order_id)
        if order is None:
            raise AppError(f"订单 {order_id} 不存在", code="NOT_FOUND", http_status=404)
        revision = session.scalar(
            select(OrderRevision).where(
                OrderRevision.order_id == order_id,
                OrderRevision.revision_no == order.current_revision,
            )
        )
        shipped = {
            sku: int(total or 0)
            for sku, total in session.execute(
                select(ShipmentRecord.sku_id, func.sum(ShipmentRecord.qty))
                .where(ShipmentRecord.order_id == order_id)
                .group_by(ShipmentRecord.sku_id)
            ).all()
        }
        lines: list[OrderLineView] = []
        if revision is not None:
            cuts = cut_by_sku(session, order_id)
            rows = session.scalars(
                select(OrderLine).where(OrderLine.revision_id == revision.id)
            ).all()
            for row in rows:
                cut = cuts.get(row.sku_id, 0) if row.fulfillment_mode == "MTO" else 0
                lines.append(
                    OrderLineView(
                        sku_id=row.sku_id,
                        ordered_qty=row.ordered_qty,
                        fulfillment_mode=row.fulfillment_mode,
                        shipped_qty=shipped.get(row.sku_id, 0),
                        cut_qty=cut,
                    )
                )
        return OrderGetResponse(
            request_id=request.state.request_id,
            correlation_id=request.state.correlation_id,
            order_id=order.id,
            current_revision=order.current_revision,
            intent_revision=order.intent_revision,
            status=order.status,
            lines=lines,
        )
    finally:
        session.close()


@router.get("/stock", response_model=StockResponse)
def get_stock(
    request: Request,
    sku_ids: list[str] | None = None,
    warehouse_id: str = WAREHOUSE_ID,
) -> StockResponse:
    session = request.app.state.session_factory()
    try:
        stmt = select(StockBalance).where(StockBalance.warehouse_id == warehouse_id)
        if sku_ids:
            stmt = stmt.where(StockBalance.sku_id.in_(sku_ids))
        rows = [
            StockRow(
                sku_id=row.sku_id,
                warehouse_id=row.warehouse_id,
                on_hand=row.on_hand,
                reserved=row.reserved,
                available=row.on_hand - row.reserved,
                revision=row.revision,
            )
            for row in session.scalars(stmt).all()
        ]
        return StockResponse(
            request_id=request.state.request_id,
            correlation_id=request.state.correlation_id,
            rows=rows,
        )
    finally:
        session.close()


@router.get("/inspections/downstream")
def downstream_inspection(request: Request) -> dict[str, object]:
    return {
        "request_id": request.state.request_id,
        "correlation_id": request.state.correlation_id,
        "schema_version": "1.0",
        "drifts": inspect_downstream(request.app.state.session_factory),
    }
