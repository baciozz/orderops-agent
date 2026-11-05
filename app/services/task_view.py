"""Read-only task projection. Business authorization remains in the kernel."""

from typing import cast

from schemas.common import SourceRef
from schemas.enums import DisabledReason, LockedKind, RuntimeMode, VerificationState, WaitingOn
from schemas.task_view import (
    ApprovalBlock,
    DispatchStepView,
    DownstreamImpact,
    LineDiff,
    TaskCard,
    TaskResultBlock,
    TaskView,
)
from sqlalchemy import select

from app.db.models import (
    Approval,
    DispatchStep,
    DownstreamLink,
    Event,
    ModelUsage,
    Operation,
    OrderLine,
    OrderRevision,
    Plan,
    ProductionOrder,
    PurchaseOrder,
    SalesOrder,
    Task,
)
from app.domain.changes import SnapLine, evaluate_changes
from app.errors import AppError
from app.orchestration.inbox import latest_input
from app.orchestration.state import aware
from app.services.kernel import (
    _available_map,
    _balances_for,
    _known_skus,
    _load_lines,
    _mentioned,
    _stable,
    current_doc_fingerprint,
)
from app.services.source_spans import task_spans


def _revision_lines(session, order_id, revision_no):
    revision = session.scalar(
        select(OrderRevision).where(
            OrderRevision.order_id == order_id, OrderRevision.revision_no == revision_no
        )
    )
    if revision is None:
        return {}
    return {
        row.sku_id: row
        for row in session.scalars(select(OrderLine).where(OrderLine.revision_id == revision.id))
    }


def _line_diffs(session, task, plan, operation):
    current = _load_lines(session, task.order_id) if task.order_id else {}
    before = _revision_lines(session, task.order_id, plan.base_order_revision)
    base = {
        sku: SnapLine(
            sku,
            row.ordered_qty,
            row.fulfillment_mode,
            current[sku].shipped_qty if sku in current else 0,
            current[sku].cut_qty if sku in current else 0,
        )
        for sku, row in before.items()
    }
    changes = plan.normalized_changes.get("changes", [])
    skus = set(base) | _mentioned(changes)
    available = _available_map(session, skus)
    if operation and operation.result_json:
        # Committed results are projected from their recorded revision, never
        # by reapplying a change against today's order quantities.
        after = _revision_lines(session, task.order_id, operation.result_json["revision"])
    else:
        evaluation = evaluate_changes(
            lines=base,
            changes=changes,
            stock_available=available,
            known_skus=_known_skus(session),
            candidates_for={},
        )
        if evaluation.kind != "validated":
            return []
        after = evaluation.lines
    rows = []
    for sku in sorted(set(before) | set(after)):
        old = before[sku].ordered_qty if sku in before else 0
        new = after[sku].ordered_qty if sku in after else 0
        line = base.get(sku) or current.get(sku)
        mode = (after.get(sku) or before.get(sku)).fulfillment_mode
        locked = (line.cut_qty if mode == "MTO" else line.shipped_qty) if line else 0
        rows.append(
            LineDiff(
                sku_id=sku,
                old_qty=old,
                new_qty=new,
                delta=new - old,
                locked_qty=locked,
                locked_kind=cast(
                    LockedKind, ("cut" if mode == "MTO" else "shipped") if locked else "none"
                ),
                unexecuted_qty=max(new - locked, 0),
                reservation_delta=new - old if mode == "STOCK" else 0,
                available_qty=available.get(sku),
            )
        )
    return rows


def _approval_block(session, task, plan, now, generated_at):
    approval = session.scalar(
        select(Approval)
        .where(Approval.plan_id == plan.id)
        .order_by(Approval.expires_at.desc(), Approval.id.desc())
    )
    order = session.get(SalesOrder, task.order_id) if task.order_id else None
    reason = None
    expired = bool(approval and (aware(approval.expires_at) <= now or approval.status == "EXPIRED"))
    if plan.status == "EXPIRED":
        reason = "EXPIRED"
    elif plan.status == "COMMITTED" or (approval and approval.status == "CONSUMED"):
        reason = "CONSUMED"
    elif (
        plan.status not in {"VALIDATED", "APPROVED"}
        or task.status != "AWAITING_APPROVAL"
        or task.cancel_requested
        or plan.intent_revision != task.intent_revision
        or (
            approval
            and (
                approval.status not in {"ACTIVE", "EXPIRED"} or approval.plan_hash != plan.plan_hash
            )
        )
        or (
            order
            and plan.plan_kind != "create"
            and (
                order.intent_revision != plan.intent_revision
                or order.current_revision != plan.base_order_revision
            )
        )
    ):
        reason = "STALE"
    if reason is None:
        balances = _balances_for(session, set(plan.expected_stock_revisions))
        if any(
            sku not in balances or balances[sku].revision != expected
            for sku, expected in plan.expected_stock_revisions.items()
        ):
            reason = "STALE"
        if (
            order
            and plan.plan_kind != "create"
            and _stable(current_doc_fingerprint(session, order.id, plan.expected_doc_revisions))
            != _stable(plan.expected_doc_revisions)
        ):
            reason = "STALE"
    renewable = reason is None and expired
    if renewable:
        reason = "EXPIRED"
    return ApprovalBlock(
        plan_hash=plan.plan_hash,
        generated_at=generated_at,
        expires_at=aware(approval.expires_at) if approval else None,
        approvable=reason is None,
        disabled_reason=cast(DisabledReason | None, reason),
    )


def build_task_view(session, task_id, *, request_id, correlation_id, now):
    task = session.get(Task, task_id)
    if task is None:
        raise AppError("任务不存在", code="TASK_NOT_FOUND", http_status=404)
    # Each kernel plan has its own event. Use its timestamp, not a random plan ID.
    plan = session.scalar(
        select(Plan)
        .join(Event, Event.payload_ref == Plan.id)
        .where(Plan.task_id == task.id)
        .order_by(Event.received_at.desc(), Event.id.desc())
    )
    steps = session.scalars(
        select(DispatchStep).where(DispatchStep.task_id == task.id).order_by(DispatchStep.step_no)
    )
    dispatch = [
        DispatchStepView(
            step_no=s.step_no,
            agent_id=s.agent_id,
            agent_version=s.agent_version,
            capability=s.capability,
            status=s.status,
            duration_ms=int((aware(s.ended_at) - aware(s.started_at)).total_seconds() * 1000)
            if s.ended_at and s.started_at
            else None,
            tokens=s.tokens,
            cost_usd=str(s.cost_usd) if s.cost_usd is not None else None,
            error_code=s.error_code,
            skip_reason=s.skip_reason,
        )
        for s in steps
    ]
    operation = session.get(Operation, task.result_ref) if task.result_ref else None
    if operation and operation.status != "COMMITTED":
        operation = None
    sources = task_spans(session, task.id)
    receipt = latest_input(session, task)
    extraction = receipt.body.get("extraction", {}) if receipt else {}
    sources.extend(SourceRef.model_validate(ref) for ref in extraction.get("source_refs", []))
    approval = ApprovalBlock(
        plan_hash=None,
        generated_at=None,
        expires_at=None,
        approvable=False,
        disabled_reason=None,
    )
    diffs = []
    downstream = []
    if plan:
        event = session.scalar(select(Event).where(Event.payload_ref == plan.id))
        approval = _approval_block(
            session, task, plan, now, aware(event.received_at) if event else None
        )
        diffs = _line_diffs(session, task, plan, operation)
        sources.extend(SourceRef.model_validate(ref) for ref in plan.source_refs)
        for item in plan.normalized_changes.get("downstream", []):
            model = ProductionOrder if item["doc_type"] == "production_order" else PurchaseOrder
            doc = session.get(model, item["doc_id"])
            link = session.scalar(
                select(DownstreamLink)
                .where(
                    DownstreamLink.order_id == task.order_id,
                    DownstreamLink.doc_id == item["doc_id"],
                    DownstreamLink.doc_type == item["doc_type"],
                )
                .order_by(DownstreamLink.link_seq.desc())
            )
            downstream.append(
                DownstreamImpact(
                    doc_id=item["doc_id"],
                    doc_type=item["doc_type"],
                    linked_revision=link.order_revision if link else None,
                    status=doc.status if doc else "UNKNOWN",
                    verdict=item["verdict"],
                    before=item.get("before"),
                    after=item.get("after"),
                    executable=approval.approvable and item["verdict"] != "EXECUTED_LOCKED",
                    reason=item.get("reason"),
                )
            )
    waiting = "none"
    if task.status in {"NEEDS_CLARIFICATION", "AWAITING_APPROVAL"}:
        waiting = "human"
    elif task.status in {"QUEUED", "RUNNING"}:
        waiting = "subagent"
    result = operation.result_json or {} if operation else {}
    return TaskView(
        request_id=request_id,
        correlation_id=correlation_id,
        card=TaskCard(
            task_id=task.id,
            order_id=task.order_id,
            status=task.status,
            intent_revision=task.intent_revision,
            waiting_on=cast(WaitingOn, waiting),
            mode=RuntimeMode.LIVE
            if session.scalar(select(ModelUsage.id).where(ModelUsage.task_id == task.id).limit(1))
            else RuntimeMode.FIXTURE,
        ),
        dispatch=dispatch,
        line_diffs=diffs,
        downstream=downstream,
        sources=list({ref.model_dump_json(): ref for ref in sources}.values()),
        conflicts=extraction.get("conflicts", []),
        questions=receipt.body.get("questions", []) if receipt else [],
        approval=approval,
        result=TaskResultBlock(
            operation_id=operation.id if operation else None,
            revision=result.get("revision"),
            verification=cast(
                VerificationState | None,
                ("pending" if operation.committed_verification_pending else "passed")
                if operation
                else None,
            ),
            download_url=f"/tasks/{task.id}/download" if diffs else None,
        ),
    )
