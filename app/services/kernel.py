"""Stage 1A business kernel: plan, approve, and commit.

Downstream documents are relinked in the same commit as the order revision.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from schemas.enums import (
    ApprovalStatus,
    FulfillmentMode,
    MovementType,
    PlanKind,
    PlanStatus,
    ProposalStatus,
    TaskStatus,
)
from seeds.golden import WAREHOUSE_ID
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    Approval,
    AuditRecord,
    Customer,
    DocRevision,
    DownstreamLink,
    Event,
    JobAttempt,
    MaterialUsage,
    Operation,
    OrderLine,
    OrderRevision,
    OutboxDelivery,
    Plan,
    ProductionOrder,
    PurchaseOrder,
    PurchaseProposal,
    Reservation,
    SalesOrder,
    ShipmentRecord,
    Sku,
    StockBalance,
    StockMovement,
    Task,
)
from app.db.session import begin_immediate
from app.domain.changes import ChangeEval, SnapLine, business_error, evaluate_changes
from app.domain.downstream import assess_downstream, locked_preview
from app.domain.hashing import canonical_hash
from app.domain.inventory import MovementView, rebuild_balances
from app.domain.orders import derive_order_status
from app.errors import AppError
from app.observability import (
    enrich,
    get_logger,
    observed,
    task_fields_from_factory,
    task_log_fields,
)
from app.orchestration.state import enforce, transition

logger = get_logger(__name__)


DEMO_ACTOR_ID = "demo-user"
_THREAD = "demo"

commit_failpoint: ContextVar[str | None] = ContextVar("commit_failpoint", default=None)
verify_failpoint: ContextVar[bool] = ContextVar("verify_failpoint", default=False)
# Set by the dispatcher so plan/approve reuse the task that already owns DispatchSteps.
bound_task_id: ContextVar[str | None] = ContextVar("bound_task_id", default=None)


@dataclass
class PlanOutcome:
    plan_id: str
    task_id: str
    chain_id: str
    order_id: str | None
    status: str
    base_revision: int
    intent_revision: int
    plan_hash: str
    stock_revisions: dict[str, int]
    requires_human_approval: bool
    issues: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    proposal_ids: list[str] = field(default_factory=list)
    verdicts: list[dict[str, object]] = field(default_factory=list)


@dataclass
class ApprovalOutcome:
    approval_id: str
    plan_id: str
    expires_at: datetime


@dataclass
class CommitOutcome:
    operation_id: str
    order_id: str
    revision: int
    audit_id: str
    task_id: str
    chain_id: str
    plan_id: str
    replayed: bool = False


def plan_change(
    session_factory: sessionmaker[Session],
    *,
    order_id: str,
    base_revision: int,
    intent_revision: int,
    changes: list[dict[str, object]],
    source_refs: list[dict[str, object]] | None = None,
    client_event_key: str,
    bump_intent: bool = True,
    now: datetime | None = None,
) -> PlanOutcome:
    """Precheck a change. A new event bumps intent and invalidates old approvals."""
    moment = _now(now)
    session = session_factory()
    error: AppError | None = None
    outcome: PlanOutcome | None = None
    try:
        with begin_immediate(session):
            enforce(session)
            prior = _existing_outcome(session, client_event_key)
            if prior is not None:
                outcome, error = prior
            else:
                outcome, error = _plan_change_locked(
                    session,
                    order_id=order_id,
                    base_revision=base_revision,
                    intent_revision=intent_revision,
                    changes=[_clean_change(item) for item in changes],
                    source_refs=source_refs or [],
                    client_event_key=client_event_key,
                    bump_intent=bump_intent,
                    now=moment,
                )
    finally:
        session.close()
    if error is not None:
        raise error
    assert outcome is not None
    return outcome


def note_ambiguous(
    session_factory: sessionmaker[Session],
    *,
    order_id: str,
    client_event_key: str,
    question: str,
    now: datetime | None = None,
) -> PlanOutcome:
    """Record an unclear event. No order lines or stock are written."""
    moment = _now(now)
    session = session_factory()
    try:
        with begin_immediate(session):
            enforce(session)
            prior = _existing_outcome(session, client_event_key)
            if prior is not None:
                outcome, error = prior
                if error is not None:
                    raise error
                assert outcome is not None
                return outcome
            order = _require_order(session, order_id)
            invalidate_order(session, order_id)
            order.intent_revision += 1
            return _store_non_validated(
                session,
                order_id=order_id,
                chain_id=f"CH-{order_id}",
                base_revision=order.current_revision,
                intent_revision=order.intent_revision,
                status=TaskStatus.NEEDS_CLARIFICATION,
                questions=[question],
                candidates=[],
                issues=[question],
                source_refs=[],
                client_event_key=client_event_key,
                now=moment,
                kind="clarify",
                code="NEEDS_CLARIFICATION",
                message=question,
            )
    finally:
        session.close()


def plan_new_order(
    session_factory: sessionmaker[Session],
    *,
    lines: list[dict[str, object]],
    client_event_key: str,
    customer_code: str | None = None,
    customer_name: str | None = None,
    source_refs: list[dict[str, object]] | None = None,
    now: datetime | None = None,
) -> PlanOutcome:
    moment = _now(now)
    session = session_factory()
    error: AppError | None = None
    outcome: PlanOutcome | None = None
    try:
        with begin_immediate(session):
            enforce(session)
            prior = _existing_outcome(session, client_event_key)
            if prior is not None:
                outcome, error = prior
            else:
                outcome, error = _plan_new_locked(
                    session,
                    lines=lines,
                    customer_code=customer_code,
                    customer_name=customer_name,
                    source_refs=source_refs or [],
                    client_event_key=client_event_key,
                    now=moment,
                )
    finally:
        session.close()
    if error is not None:
        raise error
    assert outcome is not None
    return outcome


@observed(
    logger,
    "approval.granted",
    failed_event="approval.rejected",
    exclude=("mode",),
    fields=lambda plan_id, actor_id, **_: {"plan_id": plan_id, "actor": actor_id},
)
def approve_plan(
    session_factory: sessionmaker[Session],
    *,
    plan_id: str,
    plan_hash: str,
    expected_intent_revision: int,
    actor_id: str = DEMO_ACTOR_ID,
    acknowledge_duplicate: bool = False,
    now: datetime | None = None,
    ttl_seconds: int = 600,
) -> ApprovalOutcome:
    moment = _now(now)
    session = session_factory()
    try:
        with begin_immediate(session):
            plan = session.get(Plan, plan_id)
            if plan is not None:
                enrich(**task_log_fields(session, plan.task_id))
                enrich(
                    task_id=plan.task_id,
                    base_revision=plan.base_order_revision,
                    intent_revision=plan.intent_revision,
                )
            if plan is None or plan.status not in {PlanStatus.VALIDATED, PlanStatus.APPROVED}:
                raise AppError("计划不可批准", code="NOT_APPROVED", http_status=409)
            if plan.plan_hash != plan_hash:
                raise AppError(
                    "plan_hash 与服务器计划不一致",
                    code="HASH_MISMATCH",
                    http_status=409,
                )
            if plan.intent_revision != expected_intent_revision:
                raise AppError("意图版本已变化", code="STALE_INTENT", http_status=409)
            duplicate_of = (plan.normalized_changes or {}).get("suspected_duplicate_of")
            if duplicate_of and not acknowledge_duplicate:
                raise AppError(
                    f"疑似重复订单 {duplicate_of}，需要批准人逐项确认",
                    code="DUPLICATE_UNCONFIRMED",
                    http_status=409,
                    details={"order_id": duplicate_of},
                )
            active = session.scalars(
                select(Approval).where(
                    Approval.plan_id == plan.id,
                    Approval.status == ApprovalStatus.ACTIVE,
                )
            ).first()
            if active is not None and active.plan_hash == plan_hash:
                enrich(approval_id=active.id)
                return ApprovalOutcome(active.id, plan.id, _aware(active.expires_at))
            approval = Approval(
                id=_id("AP"),
                plan_id=plan.id,
                plan_hash=plan.plan_hash,
                actor_id=actor_id,
                intent_revision=plan.intent_revision,
                dependencies_hash=str((plan.normalized_changes or {}).get("dependencies_hash")),
                expires_at=moment + timedelta(seconds=ttl_seconds),
                status=ApprovalStatus.ACTIVE,
                consumed_operation_id=None,
            )
            plan.status = PlanStatus.APPROVED
            session.add(approval)
            enrich(approval_id=approval.id)
            return ApprovalOutcome(approval.id, plan.id, approval.expires_at)
    finally:
        session.close()


@observed(
    logger,
    "commit.applied",
    failed_event="commit.failed",
    exclude=("mode",),
    fields=lambda plan_id, approval_id, actor_id, **_: {
        "plan_id": plan_id,
        "approval_id": approval_id,
        "actor": actor_id,
    },
)
def commit_plan(
    session_factory: sessionmaker[Session],
    *,
    plan_id: str,
    approval_id: str,
    plan_hash: str,
    idempotency_key: str,
    actor_id: str = DEMO_ACTOR_ID,
    now: datetime | None = None,
    acknowledge: bool = True,
) -> CommitOutcome:
    moment = _now(now)
    request_hash = canonical_hash(
        {"plan_id": plan_id, "approval_id": approval_id, "plan_hash": plan_hash}
    )
    session = session_factory()
    outcome: CommitOutcome | None = None
    error: AppError | None = None
    try:
        with begin_immediate(session):
            outcome, error = _commit_locked(
                session,
                plan_id=plan_id,
                approval_id=approval_id,
                plan_hash=plan_hash,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                actor_id=actor_id,
                now=moment,
            )
    finally:
        session.close()
    if error is not None:
        raise error
    assert outcome is not None
    enrich(**task_fields_from_factory(session_factory, outcome.task_id))
    enrich(
        task_id=outcome.task_id,
        order_id=outcome.order_id,
        revision=outcome.revision,
        replayed=outcome.replayed,
    )
    if not outcome.replayed:
        _post_verify(session_factory, outcome.operation_id)
    if acknowledge:
        acknowledge_commit(session_factory, outcome.task_id)
    return outcome


def acknowledge_commit(session_factory: sessionmaker[Session], task_id: str) -> None:
    """Complete a durable commit without reopening an already acknowledged task."""
    with session_factory() as session, begin_immediate(session):
        task = session.get(Task, task_id)
        if task is None:
            raise AppError("任务不存在", code="NOT_FOUND", http_status=404)
        acknowledged_statuses: set[str] = {TaskStatus.COMMITTING, TaskStatus.RECONCILING}
        if task.status in acknowledged_statuses:
            transition(task, TaskStatus.SUCCEEDED)


def record_receipt(
    session_factory: sessionmaker[Session],
    *,
    sku_id: str,
    qty: int,
    now: datetime | None = None,
) -> None:
    """Sandbox receipt. Bumps the stock revision so a stale approval can be tested."""
    if qty <= 0:
        raise AppError("入库数量必须是正整数", code="BUSINESS_CONFLICT", http_status=422)
    moment = _now(now)
    session = session_factory()
    try:
        with begin_immediate(session):
            balance = session.get(StockBalance, (WAREHOUSE_ID, sku_id))
            if balance is None:
                raise AppError(f"没有 {sku_id} 的库存行", code="NOT_FOUND", http_status=404)
            before = balance.revision
            balance.on_hand += qty
            balance.revision = before + 1
            operation_id = _id("OP")
            session.add(
                StockMovement(
                    id=_id("SM"),
                    operation_id=operation_id,
                    seq=1,
                    movement_type=MovementType.RECEIPT,
                    warehouse_id=WAREHOUSE_ID,
                    sku_id=sku_id,
                    delta_on_hand=qty,
                    delta_reserved=0,
                    before_revision=before,
                    after_revision=balance.revision,
                    at=moment,
                )
            )
    finally:
        session.close()


def record_production_cut(
    session_factory: sessionmaker[Session],
    *,
    doc_id: str,
    cut_qty: int,
    now: datetime | None = None,
) -> None:
    """Sandbox cut. Not available to a model. Changes status and doc_revision."""
    moment = _now(now)
    session = session_factory()
    try:
        with begin_immediate(session):
            order = session.get(ProductionOrder, doc_id)
            if order is None:
                raise AppError(f"没有生产单 {doc_id}", code="NOT_FOUND", http_status=404)
            if cut_qty < 0 or cut_qty > order.planned_qty:
                raise AppError("裁剪数量不合法", code="BUSINESS_CONFLICT", http_status=422)
            order.cut_qty = cut_qty
            if cut_qty == 0:
                order.status = "NOT_STARTED"
            elif cut_qty == order.planned_qty:
                order.status = "CUT"
            else:
                order.status = "PARTIALLY_CUT"
            order.doc_revision += 1
            session.add(
                DocRevision(
                    id=f"DR-{doc_id}-{order.doc_revision}",
                    doc_type="production_order",
                    doc_id=doc_id,
                    rev_no=order.doc_revision,
                    snapshot_json={
                        "sku_id": order.sku_id,
                        "planned_qty": order.planned_qty,
                        "cut_qty": order.cut_qty,
                        "status": order.status,
                    },
                    operation_id=None,
                )
            )
            _ = moment
    finally:
        session.close()


def inspect_downstream(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    """Read-only drift report. Does not write a repair."""
    session = session_factory()
    try:
        return downstream_findings(session)
    finally:
        session.close()


def _plan_change_locked(
    session: Session,
    *,
    order_id: str,
    base_revision: int,
    intent_revision: int,
    changes: list[dict[str, object]],
    source_refs: list[dict[str, object]],
    client_event_key: str,
    bump_intent: bool,
    now: datetime,
) -> tuple[PlanOutcome | None, AppError | None]:
    order = _require_order(session, order_id)
    if order.current_revision != base_revision:
        return None, AppError("订单版本已变化", code="STALE_DEPENDENCY", http_status=409)
    if order.intent_revision != intent_revision:
        return None, AppError("意图版本已变化", code="STALE_INTENT", http_status=409)
    lines = _load_lines(session, order_id)
    changes = _resolve_choice(changes, lines)
    known = _known_skus(session)
    evaluation = evaluate_changes(
        lines=lines,
        changes=changes,
        stock_available=_available_map(session, _mentioned(changes) | set(lines)),
        known_skus=known,
        candidates_for={sku: _candidates(session, sku) for sku in _mentioned(changes)},
    )
    if evaluation.code == "LOCKED_PORTION":
        return _locked_clarification(
            session,
            order=order,
            lines=lines,
            source_refs=source_refs,
            client_event_key=client_event_key,
            now=now,
            unexecuted_note=evaluation.message or "",
        ), None
    if evaluation.code in {"EXECUTED_PORTION_IMMUTABLE", "INVARIANT_6"}:
        rejected = business_error(evaluation)
        assert rejected is not None
        return None, rejected
    if bump_intent:
        invalidate_order(session, order_id)
        order.intent_revision += 1
    intent = order.intent_revision
    documents = _load_documents(session, order_id)
    usage = _usage_map(session)
    verdicts: list[dict[str, object]] = []
    if evaluation.kind == "validated":
        verdicts, downstream_error = assess_downstream(
            new_lines=evaluation.lines,
            production=documents["production"],
            purchase=documents["purchase"],
            usage=usage,
        )
        if downstream_error:
            return _locked_clarification(
                session,
                order=order,
                lines=lines,
                source_refs=source_refs,
                client_event_key=client_event_key,
                now=now,
                unexecuted_note=downstream_error,
            ), None
    if evaluation.kind == "validated":
        return (
            _store_validated(
                session,
                plan_kind=PlanKind.CHANGE,
                order_id=order_id,
                customer_id=order.customer_id,
                chain_id=f"CH-{order_id}",
                base_revision=order.current_revision,
                intent_revision=intent,
                changes=changes,
                evaluation=evaluation,
                source_refs=source_refs,
                client_event_key=client_event_key,
                now=now,
                suspected_duplicate_of=None,
                verdicts=verdicts,
                expected_docs=_doc_fingerprint(documents),
            ),
            None,
        )
    if evaluation.kind == "clarify":
        return (
            _store_non_validated(
                session,
                order_id=order_id,
                chain_id=f"CH-{order_id}",
                base_revision=order.current_revision,
                intent_revision=intent,
                status=TaskStatus.NEEDS_CLARIFICATION,
                questions=evaluation.questions,
                candidates=evaluation.candidates,
                issues=evaluation.issues,
                source_refs=source_refs,
                client_event_key=client_event_key,
                now=now,
                kind="clarify",
                code="NEEDS_CLARIFICATION",
                message=evaluation.message or "需要澄清",
            ),
            None,
        )
    if evaluation.kind == "shortage":
        outcome = _store_shortage(
            session,
            order_id=order_id,
            chain_id=f"CH-{order_id}",
            base_revision=order.current_revision,
            intent_revision=intent,
            evaluation=evaluation,
            source_refs=source_refs,
            client_event_key=client_event_key,
            now=now,
        )
        return outcome, AppError(
            evaluation.message or "缺货，整单拒绝",
            code="SHORTAGE",
            http_status=422,
            details={"proposal_ids": outcome.proposal_ids, "plan_id": outcome.plan_id},
        )
    _store_failed_task(
        session,
        order_id=order_id,
        chain_id=f"CH-{order_id}",
        intent_revision=intent,
        code=evaluation.code or "BUSINESS_CONFLICT",
        message=evaluation.message or "业务冲突",
        client_event_key=client_event_key,
        now=now,
    )
    rejected = business_error(evaluation)
    assert rejected is not None
    return None, rejected


def _plan_new_locked(
    session: Session,
    *,
    lines: list[dict[str, object]],
    customer_code: str | None,
    customer_name: str | None,
    source_refs: list[dict[str, object]],
    client_event_key: str,
    now: datetime,
) -> tuple[PlanOutcome | None, AppError | None]:
    customer, clarify = _resolve_customer(session, customer_code, customer_name)
    chain_id = _id("CH")
    if clarify is not None:
        return (
            _store_non_validated(
                session,
                order_id=None,
                chain_id=chain_id,
                base_revision=0,
                intent_revision=1,
                status=TaskStatus.NEEDS_CLARIFICATION,
                questions=[clarify[0]],
                candidates=clarify[1],
                issues=[clarify[0]],
                source_refs=source_refs,
                client_event_key=client_event_key,
                now=now,
                kind="clarify",
                code="NEEDS_CLARIFICATION",
                message=clarify[0],
            ),
            None,
        )
    assert customer is not None
    changes: list[dict[str, object]] = []
    line_refs: list[dict[str, object]] = list(source_refs)
    for line in lines:
        qty = line.get("qty")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            return _err("新建订单的数量必须是正整数", "BUSINESS_CONFLICT", 422)
        sku_id = line.get("sku_id")
        if not sku_id:
            sku_id = _resolve_sku_id(
                session,
                style=_text(line.get("style")),
                color=_text(line.get("color")),
                size=_text(line.get("size")),
            )
        if sku_id is None:
            token = _text(line.get("style")) or _text(line.get("color")) or ""
            found = _candidates(session, token)
            return (
                _store_non_validated(
                    session,
                    order_id=None,
                    chain_id=chain_id,
                    base_revision=0,
                    intent_revision=1,
                    status=TaskStatus.NEEDS_CLARIFICATION,
                    questions=[f"无法唯一匹配 SKU：{token}"],
                    candidates=found,
                    issues=[f"无法唯一匹配 SKU：{token}"],
                    source_refs=line_refs,
                    client_event_key=client_event_key,
                    now=now,
                    kind="clarify",
                    code="NEEDS_CLARIFICATION",
                    message=f"无法唯一匹配 SKU：{token}",
                ),
                None,
            )
        sku = session.get(Sku, str(sku_id))
        if sku is None:
            found = _candidates(session, str(sku_id))
            return (
                _store_non_validated(
                    session,
                    order_id=None,
                    chain_id=chain_id,
                    base_revision=0,
                    intent_revision=1,
                    status=TaskStatus.NEEDS_CLARIFICATION,
                    questions=[f"未知 SKU {sku_id}"],
                    candidates=found,
                    issues=[f"未知 SKU {sku_id}"],
                    source_refs=line_refs,
                    client_event_key=client_event_key,
                    now=now,
                    kind="clarify",
                    code="NEEDS_CLARIFICATION",
                    message=f"未知 SKU {sku_id}",
                ),
                None,
            )
        if sku.default_fulfillment_mode != FulfillmentMode.STOCK:
            return None, AppError(
                "P0 新建订单只支持现货行",
                code="MTO_CREATE_UNSUPPORTED",
                http_status=422,
            )
        ref = line.get("source_ref")
        if isinstance(ref, dict):
            line_refs.append(ref)
        changes.append({"mode": "increment", "sku_id": sku.id, "qty": qty})
    known = _known_skus(session)
    evaluation = evaluate_changes(
        lines={},
        changes=changes,
        stock_available=_available_map(session, {str(item["sku_id"]) for item in changes}),
        known_skus=known,
        candidates_for={
            str(item["sku_id"]): _candidates(session, str(item["sku_id"])) for item in changes
        },
    )
    if evaluation.kind == "shortage":
        outcome = _store_shortage(
            session,
            order_id=None,
            chain_id=chain_id,
            base_revision=0,
            intent_revision=1,
            evaluation=evaluation,
            source_refs=line_refs,
            client_event_key=client_event_key,
            now=now,
        )
        return outcome, AppError(
            evaluation.message or "缺货，整单拒绝",
            code="SHORTAGE",
            http_status=422,
            details={"proposal_ids": outcome.proposal_ids, "source_refs": line_refs},
        )
    if evaluation.kind != "validated":
        rejected = business_error(evaluation) or AppError(
            evaluation.message or "无法新建订单",
            code=evaluation.code or "BUSINESS_CONFLICT",
            http_status=422,
        )
        return None, rejected
    duplicate = _suspected_duplicate(session, customer.id, evaluation.lines, now)
    return (
        _store_validated(
            session,
            plan_kind=PlanKind.CREATE,
            order_id=None,
            customer_id=customer.id,
            chain_id=chain_id,
            base_revision=0,
            intent_revision=1,
            changes=changes,
            evaluation=evaluation,
            source_refs=line_refs,
            client_event_key=client_event_key,
            now=now,
            suspected_duplicate_of=duplicate,
        ),
        None,
    )


def _commit_locked(
    session: Session,
    *,
    plan_id: str,
    approval_id: str,
    plan_hash: str,
    idempotency_key: str,
    request_hash: str,
    actor_id: str,
    now: datetime,
) -> tuple[CommitOutcome | None, AppError | None]:
    existing = session.scalar(select(Operation).where(Operation.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.request_hash == request_hash:
            body = existing.result_json or {}
            return (
                CommitOutcome(
                    operation_id=existing.id,
                    order_id=str(body["order_id"]),
                    revision=int(body["revision"]),
                    audit_id=str(body["audit_id"]),
                    task_id=str(body["task_id"]),
                    chain_id=str(body["chain_id"]),
                    plan_id=str(body["plan_id"]),
                    replayed=True,
                ),
                None,
            )
        return None, AppError(
            "同一幂等键对应了不同的请求",
            code="IDEMPOTENCY_CONFLICT",
            http_status=409,
        )
    approval = session.get(Approval, approval_id)
    plan = session.get(Plan, plan_id)
    if plan is not None:
        enrich(**task_log_fields(session, plan.task_id))
        enrich(
            task_id=plan.task_id,
            base_revision=plan.base_order_revision,
            intent_revision=plan.intent_revision,
        )
    if approval is None or plan is None or approval.plan_id != plan.id:
        return None, AppError("批准不存在", code="NOT_APPROVED", http_status=404)
    if plan_hash != approval.plan_hash:
        return _err("提交的 plan_hash 与批准不一致", "HASH_MISMATCH", 409)
    if _aware(approval.expires_at) <= now:
        if approval.status == ApprovalStatus.ACTIVE:
            approval.status = ApprovalStatus.EXPIRED
        return None, AppError("批准已过期", code="EXPIRED", http_status=409)
    if approval.actor_id != actor_id:
        return _err("审批人与当前身份不一致", "APPROVAL_ACTOR_MISMATCH", 403)
    task = session.get(Task, plan.task_id)
    if task is None:
        return None, AppError("任务不存在", code="NOT_FOUND", http_status=404)
    if task.cancel_requested or task.status == TaskStatus.CANCELLED:
        return _err("任务已取消", "CANCELLED", 409)
    if plan.plan_kind == PlanKind.CHANGE:
        order = _require_order(session, str(task.order_id))
        if order.intent_revision != approval.intent_revision:
            _revoke_if_active(approval)
            return _err("意图已更新，旧批准不能提交", "STALE_INTENT", 409)
        if order.current_revision != plan.base_order_revision:
            _revoke_if_active(approval)
            return None, AppError("订单版本已变化", code="STALE_DEPENDENCY", http_status=409)
        base_lines = _load_lines(session, order.id)
        customer_id = order.customer_id
        current_docs = _doc_fingerprint_loaded(_load_documents(session, order.id))
        if _stable(current_docs) != _stable(dict(plan.expected_doc_revisions or {})):
            _revoke_if_active(approval)
            return None, AppError(
                "下游单据版本或状态已变化", code="STALE_DEPENDENCY", http_status=409
            )
    else:
        order = None
        base_lines = {}
        customer_id = str((plan.normalized_changes or {})["customer_id"])
    if approval.status != ApprovalStatus.ACTIVE:
        return None, AppError("批准已失效", code="APPROVAL_INVALID", http_status=409)
    stock_ids = set((plan.expected_stock_revisions or {}).keys())
    balances = _balances_for(session, stock_ids)
    for sku, expected in (plan.expected_stock_revisions or {}).items():
        balance = balances.get(sku)
        if balance is None or balance.revision != int(expected):
            _revoke_if_active(approval)
            return None, AppError("库存版本已变化", code="STALE_DEPENDENCY", http_status=409)
    changes = list((plan.normalized_changes or {}).get("changes") or [])
    evaluation = evaluate_changes(
        lines=base_lines,
        changes=changes,
        stock_available=_available_map(session, set(base_lines) | _mentioned(changes)),
        known_skus=_known_skus(session),
        candidates_for={},
    )
    if evaluation.kind != "validated":
        _revoke_if_active(approval)
        return None, AppError(
            evaluation.message or "计划已不能执行",
            code="HASH_MISMATCH",
            http_status=409,
        )
    verdicts = list((plan.normalized_changes or {}).get("downstream") or [])
    expected_docs = dict(plan.expected_doc_revisions or {})
    if plan.plan_kind == PlanKind.CHANGE:
        assert order is not None
        verdicts, downstream_error = assess_downstream(
            new_lines=evaluation.lines,
            production=_load_documents(session, order.id)["production"],
            purchase=_load_documents(session, order.id)["purchase"],
            usage=_usage_map(session),
        )
        if downstream_error:
            _revoke_if_active(approval)
            return None, AppError(downstream_error, code="HASH_MISMATCH", http_status=409)
    payload = _payload(
        plan_kind=plan.plan_kind,
        order_id=order.id if order is not None else None,
        customer_id=customer_id,
        base_revision=plan.base_order_revision,
        intent_revision=plan.intent_revision,
        evaluation=evaluation,
        stock_revisions={sku: balances[sku].revision for sku in balances},
        verdicts=verdicts,
        expected_docs=expected_docs,
    )
    recomputed = canonical_hash(payload)
    if recomputed != approval.plan_hash or recomputed != plan.plan_hash:
        _revoke_if_active(approval)
        return None, AppError("计划内容与批准哈希不一致", code="HASH_MISMATCH", http_status=409)
    transition(task, TaskStatus.COMMITTING)
    _hit_failpoint("before_write")
    operation_id = _id("OP")
    audit_id = _id("AUD")
    if plan.plan_kind == PlanKind.CREATE:
        assert order is None
        order = SalesOrder(
            id=_next_order_id(session),
            customer_id=customer_id,
            current_revision=0,
            intent_revision=plan.intent_revision,
            status="OPEN",
        )
        session.add(order)
        session.flush()
        task.order_id = order.id
    assert order is not None
    revision_no = order.current_revision + 1
    revision = OrderRevision(
        id=f"SOR-{order.id}-{revision_no}",
        order_id=order.id,
        revision_no=revision_no,
        base_revision=order.current_revision,
        reason="approved plan",
        created_by=actor_id,
        source_event_id=None,
        created_at=now,
    )
    session.add(revision)
    session.flush()
    _hit_failpoint("after_revision")
    for line in evaluation.lines.values():
        session.add(
            OrderLine(
                revision_id=revision.id,
                sku_id=line.sku_id,
                ordered_qty=line.ordered_qty,
                fulfillment_mode=line.fulfillment_mode,
            )
        )
    shipped = _shipped_map(session, order.id)
    order.current_revision = revision_no
    order.status = derive_order_status(
        lines=[
            (line.sku_id, line.ordered_qty, line.fulfillment_mode)
            for line in evaluation.lines.values()
        ],
        shipped_by_sku=shipped,
    )
    _apply_reservations(
        session,
        order_id=order.id,
        evaluation=evaluation,
        operation_id=operation_id,
        now=now,
    )
    _hit_failpoint("after_stock")
    _hit_failpoint("before_downstream")
    if verdicts:
        _apply_downstream(
            session,
            order_id=order.id,
            revision_no=revision_no,
            verdicts=verdicts,
            operation_id=operation_id,
        )
    event = Event(
        id=_id("EV"),
        source_event_key=operation_id,
        thread_id="commit",
        chain_id=task.chain_id,
        order_id=order.id,
        intent_revision=plan.intent_revision,
        kind="order.committed",
        payload_ref=operation_id,
        received_at=now,
    )
    session.add(event)
    session.flush()
    session.add(
        OutboxDelivery(
            id=_id("OB"),
            event_id=event.id,
            consumer="ui",
            delivery_state="PENDING",
            attempts=0,
            available_at=now,
        )
    )
    approval.status = ApprovalStatus.CONSUMED
    approval.consumed_operation_id = operation_id
    plan.status = PlanStatus.COMMITTED
    task.result_ref = operation_id
    result = {
        "order_id": order.id,
        "revision": revision_no,
        "audit_id": audit_id,
        "task_id": task.id,
        "chain_id": task.chain_id,
        "plan_id": plan.id,
    }
    session.add(
        Operation(
            id=operation_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            plan_id=plan.id,
            status="COMMITTED",
            result_json=result,
            committed_verification_pending=False,
        )
    )
    session.add(
        AuditRecord(
            id=audit_id,
            actor_type="human",
            actor_id=actor_id,
            action="commit",
            object_id=order.id,
            operation_id=operation_id,
            before_hash=plan.plan_hash,
            after_hash=plan.plan_hash,
            source_refs=plan.source_refs,
            at=now,
        )
    )
    _hit_failpoint("after_audit")
    return (
        CommitOutcome(
            operation_id=operation_id,
            order_id=order.id,
            revision=revision_no,
            audit_id=audit_id,
            task_id=task.id,
            chain_id=task.chain_id,
            plan_id=plan.id,
        ),
        None,
    )


def _apply_reservations(
    session: Session,
    *,
    order_id: str,
    evaluation: ChangeEval,
    operation_id: str,
    now: datetime,
) -> None:
    seq = 0
    for sku in sorted(evaluation.reservation_delta):
        delta = evaluation.reservation_delta[sku]
        balance = session.get(StockBalance, (WAREHOUSE_ID, sku))
        if balance is None:
            raise AppError(f"没有 {sku} 的库存行", code="INVENTORY_INVARIANT", http_status=422)
        before = balance.revision
        new_reserved = balance.reserved + delta
        if new_reserved < 0 or new_reserved > balance.on_hand:
            raise AppError("预占后库存不满足不变量", code="INVENTORY_INVARIANT", http_status=422)
        balance.reserved = new_reserved
        balance.revision = before + 1
        seq += 1
        session.add(
            StockMovement(
                id=f"SM-{operation_id}-{seq}",
                operation_id=operation_id,
                seq=seq,
                movement_type=MovementType.RESERVE if delta > 0 else MovementType.RELEASE,
                warehouse_id=WAREHOUSE_ID,
                sku_id=sku,
                delta_on_hand=0,
                delta_reserved=delta,
                before_revision=before,
                after_revision=balance.revision,
                at=now,
            )
        )
        line = evaluation.lines.get(sku)
        qty = 0
        if line is not None and line.fulfillment_mode == FulfillmentMode.STOCK:
            qty = line.ordered_qty - line.shipped_qty
        current = session.get(Reservation, (order_id, WAREHOUSE_ID, sku))
        if qty == 0:
            if current is not None:
                session.delete(current)
        elif current is None:
            session.add(
                Reservation(
                    order_id=order_id,
                    warehouse_id=WAREHOUSE_ID,
                    sku_id=sku,
                    qty=qty,
                    revision=balance.revision,
                )
            )
        else:
            current.qty = qty
            current.revision = balance.revision


def _store_validated(
    session: Session,
    *,
    plan_kind: str,
    order_id: str | None,
    customer_id: str,
    chain_id: str,
    base_revision: int,
    intent_revision: int,
    changes: list[dict[str, object]],
    evaluation: ChangeEval,
    source_refs: list[dict[str, object]],
    client_event_key: str,
    now: datetime,
    suspected_duplicate_of: str | None,
    verdicts: list[dict[str, object]] | None = None,
    expected_docs: dict[str, dict[str, object]] | None = None,
) -> PlanOutcome:
    stock_revisions = _revisions_for(session, set(evaluation.reservation_delta))
    verdicts = verdicts or []
    expected_docs = expected_docs or {}
    payload = _payload(
        plan_kind=plan_kind,
        order_id=order_id,
        customer_id=customer_id,
        base_revision=base_revision,
        intent_revision=intent_revision,
        evaluation=evaluation,
        stock_revisions=stock_revisions,
        verdicts=verdicts,
        expected_docs=expected_docs,
    )
    plan_hash = canonical_hash(payload)
    dependencies = canonical_hash(
        {
            "base_revision": base_revision,
            "intent_revision": intent_revision,
            "stock_revisions": stock_revisions,
            "expected_doc_revisions": expected_docs,
        }
    )
    task, plan = _new_task_plan(
        session,
        order_id=order_id,
        chain_id=chain_id,
        task_status=TaskStatus.AWAITING_APPROVAL,
        plan_kind=plan_kind,
        base_revision=base_revision,
        intent_revision=intent_revision,
        plan_status=PlanStatus.VALIDATED,
        plan_hash=plan_hash,
        stock_revisions=stock_revisions,
        source_refs=source_refs,
        normalized={
            "customer_id": customer_id,
            "changes": changes,
            "suspected_duplicate_of": suspected_duplicate_of,
            "dependencies_hash": dependencies,
            "downstream": verdicts,
            "outcome": {"kind": "validated"},
        },
        client_event_key=client_event_key,
        now=now,
        error_code=None,
        expected_docs=expected_docs,
    )
    return PlanOutcome(
        plan_id=plan.id,
        task_id=task.id,
        chain_id=chain_id,
        order_id=order_id,
        status=PlanStatus.VALIDATED,
        base_revision=base_revision,
        intent_revision=intent_revision,
        plan_hash=plan_hash,
        stock_revisions=stock_revisions,
        requires_human_approval=True,
        issues=["疑似重复，需要逐项确认"] if suspected_duplicate_of else [],
        verdicts=verdicts,
    )


def _store_shortage(
    session: Session,
    *,
    order_id: str | None,
    chain_id: str,
    base_revision: int,
    intent_revision: int,
    evaluation: ChangeEval,
    source_refs: list[dict[str, object]],
    client_event_key: str,
    now: datetime,
) -> PlanOutcome:
    task, plan = _new_task_plan(
        session,
        order_id=order_id,
        chain_id=chain_id,
        task_status=TaskStatus.FAILED,
        plan_kind=PlanKind.CREATE if order_id is None else PlanKind.CHANGE,
        base_revision=base_revision,
        intent_revision=intent_revision,
        plan_status=PlanStatus.DRAFT,
        plan_hash=canonical_hash({"kind": "shortage", "shortages": evaluation.shortages}),
        stock_revisions={},
        source_refs=source_refs,
        normalized={"outcome": {"kind": "shortage", "code": "SHORTAGE"}},
        client_event_key=client_event_key,
        now=now,
        error_code="SHORTAGE",
    )
    proposal_ids: list[str] = []
    for sku, qty in sorted(evaluation.shortages.items()):
        proposal = PurchaseProposal(
            id=_id("PP"),
            task_id=task.id,
            sku_id=sku,
            shortage_qty=qty,
            reason=evaluation.message or "缺货",
            status=ProposalStatus.DRAFT,
        )
        session.add(proposal)
        proposal_ids.append(proposal.id)
    session.flush()
    return PlanOutcome(
        plan_id=plan.id,
        task_id=task.id,
        chain_id=chain_id,
        order_id=order_id,
        status="REJECTED",
        base_revision=base_revision,
        intent_revision=intent_revision,
        plan_hash=plan.plan_hash,
        stock_revisions={},
        requires_human_approval=False,
        issues=evaluation.issues,
        proposal_ids=proposal_ids,
    )


def _store_non_validated(
    session: Session,
    *,
    order_id: str | None,
    chain_id: str,
    base_revision: int,
    intent_revision: int,
    status: str,
    questions: list[str],
    candidates: list[str],
    issues: list[str],
    source_refs: list[dict[str, object]],
    client_event_key: str,
    now: datetime,
    kind: str,
    code: str,
    message: str,
    verdicts: list[dict[str, object]] | None = None,
) -> PlanOutcome:
    plan_hash = canonical_hash({"kind": kind, "questions": questions, "candidates": candidates})
    task, plan = _new_task_plan(
        session,
        order_id=order_id,
        chain_id=chain_id,
        task_status=status,
        plan_kind=PlanKind.CHANGE if order_id else PlanKind.CREATE,
        base_revision=base_revision,
        intent_revision=intent_revision,
        plan_status=PlanStatus.DRAFT,
        plan_hash=plan_hash,
        stock_revisions={},
        source_refs=source_refs,
        normalized={
            "outcome": {
                "kind": kind,
                "code": code,
                "message": message,
                "questions": questions,
                "candidates": candidates,
                "verdicts": verdicts or [],
            }
        },
        client_event_key=client_event_key,
        now=now,
        error_code=code if status == TaskStatus.FAILED else None,
    )
    return PlanOutcome(
        plan_id=plan.id,
        task_id=task.id,
        chain_id=chain_id,
        order_id=order_id,
        status=status,
        base_revision=base_revision,
        intent_revision=intent_revision,
        plan_hash=plan.plan_hash,
        stock_revisions={},
        requires_human_approval=False,
        issues=issues,
        questions=questions,
        candidates=candidates,
        verdicts=verdicts or [],
    )


def _store_failed_task(
    session: Session,
    *,
    order_id: str,
    chain_id: str,
    intent_revision: int,
    code: str,
    message: str,
    client_event_key: str,
    now: datetime,
) -> None:
    event = _add_event(
        session,
        client_event_key=client_event_key,
        chain_id=chain_id,
        order_id=order_id,
        intent_revision=intent_revision,
        kind="change.rejected",
        payload_ref=None,
        now=now,
    )
    session.flush()
    _attach_task(
        session,
        chain_id=chain_id,
        order_id=order_id,
        status=TaskStatus.FAILED,
        intent_revision=intent_revision,
        event_id=event.id,
        error_code=code,
    )
    event.payload_ref = message[:200]


def _attach_task(
    session: Session,
    *,
    chain_id: str,
    order_id: str | None,
    status: str,
    intent_revision: int,
    event_id: str,
    error_code: str | None,
) -> Task:
    """Reuse the dispatcher's task when one is bound. Otherwise insert a new row."""
    bound = bound_task_id.get()
    if bound:
        task = session.get(Task, bound)
        if task is None:
            raise AppError("调度任务不存在", code="NOT_FOUND", http_status=404)
        task.chain_id = chain_id
        task.order_id = order_id
        transition(task, status)
        for attempt in session.scalars(select(JobAttempt).where(JobAttempt.task_id == task.id)):
            attempt.lease_until = None
            attempt.lease_owner = None
        task.intent_revision = intent_revision
        task.last_event_id = event_id
        task.error_code = error_code
        session.flush()
        return task
    task = Task(
        id=_id("T"),
        chain_id=chain_id,
        order_id=order_id,
        status=status,
        intent_revision=intent_revision,
        last_event_id=event_id,
        cancel_requested=False,
        deadline_at=None,
        result_ref=None,
        error_code=error_code,
    )
    session.add(task)
    session.flush()
    return task


def _new_task_plan(
    session: Session,
    *,
    order_id: str | None,
    chain_id: str,
    task_status: str,
    plan_kind: str,
    base_revision: int,
    intent_revision: int,
    plan_status: str,
    plan_hash: str,
    stock_revisions: dict[str, int],
    source_refs: list[dict[str, object]],
    normalized: dict[str, object],
    client_event_key: str,
    now: datetime,
    error_code: str | None,
    expected_docs: dict[str, dict[str, object]] | None = None,
) -> tuple[Task, Plan]:
    plan_id = _id("P")
    event = _add_event(
        session,
        client_event_key=client_event_key,
        chain_id=chain_id,
        order_id=order_id,
        intent_revision=intent_revision,
        kind="plan",
        payload_ref=plan_id,
        now=now,
    )
    session.flush()
    task = _attach_task(
        session,
        chain_id=chain_id,
        order_id=order_id,
        status=task_status,
        intent_revision=intent_revision,
        event_id=event.id,
        error_code=error_code,
    )
    plan = Plan(
        id=plan_id,
        task_id=task.id,
        plan_kind=plan_kind,
        base_order_revision=base_revision,
        intent_revision=intent_revision,
        chain_intent_revision=intent_revision,
        expected_stock_revisions=stock_revisions,
        expected_doc_revisions=expected_docs or {},
        normalized_changes=normalized,
        source_refs=source_refs,
        plan_hash=plan_hash,
        status=plan_status,
    )
    session.add(plan)
    session.flush()
    return task, plan


def _post_verify(session_factory: sessionmaker[Session], operation_id: str) -> None:
    session = session_factory()
    try:
        problems: list[str] = []
        try:
            if verify_failpoint.get():
                raise RuntimeError("injected verification failure")
            problems = consistency_problems(session)
            problems.extend(downstream_problems(session))
        except Exception as exc:
            problems = [str(exc)]
        session.rollback()
        if not problems:
            return
        with begin_immediate(session):
            operation = session.get(Operation, operation_id)
            if operation is not None:
                operation.committed_verification_pending = True
    finally:
        session.close()


def consistency_problems(session: Session) -> list[str]:
    problems: list[str] = []
    movements = list(session.scalars(select(StockMovement)).all())
    rebuilt = rebuild_balances(
        [
            MovementView(
                sku_id=row.sku_id,
                delta_on_hand=row.delta_on_hand,
                delta_reserved=row.delta_reserved,
                before_revision=row.before_revision,
                after_revision=row.after_revision,
            )
            for row in movements
        ]
    )
    balances = list(session.scalars(select(StockBalance)).all())
    for balance in balances:
        got = rebuilt.get(balance.sku_id)
        if got is None or (got.on_hand, got.reserved, got.revision) != (
            balance.on_hand,
            balance.reserved,
            balance.revision,
        ):
            problems.append(f"{balance.sku_id} 余额与流水不一致")
        if balance.reserved < 0 or balance.on_hand < 0 or balance.reserved > balance.on_hand:
            problems.append(f"{balance.sku_id} 不满足不变量 2")
    sums = {
        sku: int(total or 0)
        for sku, total in session.execute(
            select(Reservation.sku_id, func.sum(Reservation.qty)).group_by(Reservation.sku_id)
        ).all()
    }
    for balance in balances:
        if sums.get(balance.sku_id, 0) != balance.reserved:
            problems.append(f"{balance.sku_id} 预占总和与余额不一致")
    mto = set(
        session.scalars(
            select(Sku.id).where(Sku.default_fulfillment_mode == FulfillmentMode.MTO)
        ).all()
    )
    for reservation in session.scalars(select(Reservation)).all():
        if reservation.sku_id in mto:
            problems.append(f"MTO {reservation.sku_id} 产生了预占")
    return problems


def _payload(
    *,
    plan_kind: str,
    order_id: str | None,
    customer_id: str,
    base_revision: int,
    intent_revision: int,
    evaluation: ChangeEval,
    stock_revisions: dict[str, int],
    verdicts: list[dict[str, object]] | None = None,
    expected_docs: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "plan_kind": plan_kind,
        "order_id": order_id,
        "customer_id": customer_id,
        "base_revision": base_revision,
        "intent_revision": intent_revision,
        "lines": [
            {
                "sku_id": line.sku_id,
                "ordered_qty": line.ordered_qty,
                "fulfillment_mode": line.fulfillment_mode,
            }
            for line in sorted(evaluation.lines.values(), key=lambda item: item.sku_id)
        ],
        "reservation_delta": [
            {"sku_id": sku, "qty": evaluation.reservation_delta[sku]}
            for sku in sorted(evaluation.reservation_delta)
        ],
        "stock_revisions": {sku: stock_revisions[sku] for sku in sorted(stock_revisions)},
        "downstream": sorted(verdicts or [], key=lambda item: str(item["doc_id"])),
        "expected_doc_revisions": expected_docs or {},
    }


def _existing_outcome(
    session: Session, client_event_key: str
) -> tuple[PlanOutcome | None, AppError | None] | None:
    event = session.scalar(
        select(Event).where(
            Event.thread_id == _THREAD,
            Event.source_event_key == client_event_key,
        )
    )
    if event is None or not event.payload_ref:
        return None
    plan = session.get(Plan, event.payload_ref)
    if plan is None:
        return None
    task = session.get(Task, plan.task_id)
    stored = (plan.normalized_changes or {}).get("outcome") or {}
    kind = stored.get("kind")
    if kind in {"shortage", "reject"}:
        return None, AppError(
            str(stored.get("message") or "请求已拒绝"),
            code=str(stored.get("code") or "BUSINESS_CONFLICT"),
            http_status=422,
        )
    questions = list(stored.get("questions") or [])
    candidates = list(stored.get("candidates") or [])
    return (
        PlanOutcome(
            plan_id=plan.id,
            task_id=plan.task_id,
            chain_id=task.chain_id if task else "",
            order_id=task.order_id if task else None,
            status=task.status if task and kind == "clarify" else plan.status,
            base_revision=plan.base_order_revision,
            intent_revision=plan.intent_revision,
            plan_hash=plan.plan_hash,
            stock_revisions=dict(plan.expected_stock_revisions or {}),
            requires_human_approval=plan.status == PlanStatus.VALIDATED,
            issues=questions,
            questions=questions,
            candidates=candidates,
        ),
        None,
    )


def invalidate_order(
    session: Session, order_id: str, *, exclude_task_id: str | None = None
) -> None:
    tasks = list(session.scalars(select(Task).where(Task.order_id == order_id)).all())
    if not tasks:
        return
    plans = list(
        session.scalars(select(Plan).where(Plan.task_id.in_([task.id for task in tasks]))).all()
    )
    for plan in plans:
        if plan.status != PlanStatus.COMMITTED:
            plan.status = PlanStatus.INVALIDATED
        approvals = session.scalars(
            select(Approval).where(
                Approval.plan_id == plan.id,
                Approval.status == ApprovalStatus.ACTIVE,
            )
        ).all()
        for approval in approvals:
            approval.status = ApprovalStatus.REVOKED
    for task in tasks:
        if task.id != exclude_task_id and task.status in {
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.NEEDS_CLARIFICATION,
        }:
            transition(task, TaskStatus.SUPERSEDED)


def _load_lines(session: Session, order_id: str) -> dict[str, SnapLine]:
    order = _require_order(session, order_id)
    revision = session.scalar(
        select(OrderRevision).where(
            OrderRevision.order_id == order_id,
            OrderRevision.revision_no == order.current_revision,
        )
    )
    if revision is None:
        return {}
    shipped = _shipped_map(session, order_id)
    cuts = _cut_by_sku(session, order_id)
    lines: dict[str, SnapLine] = {}
    rows = session.scalars(select(OrderLine).where(OrderLine.revision_id == revision.id)).all()
    for row in rows:
        lines[row.sku_id] = SnapLine(
            row.sku_id,
            row.ordered_qty,
            row.fulfillment_mode,
            int(shipped.get(row.sku_id) or 0),
            cut_qty=int(cuts.get(row.sku_id) or 0),
        )
    return lines


def _shipped_map(session: Session, order_id: str) -> dict[str, int]:
    rows = session.execute(
        select(ShipmentRecord.sku_id, func.sum(ShipmentRecord.qty))
        .where(ShipmentRecord.order_id == order_id)
        .group_by(ShipmentRecord.sku_id)
    ).all()
    return {sku: int(total or 0) for sku, total in rows}


def _known_skus(session: Session) -> dict[str, str]:
    rows = session.scalars(select(Sku).where(Sku.active.is_(True))).all()
    return {row.id: row.default_fulfillment_mode for row in rows}


def _available_map(session: Session, sku_ids: set[str]) -> dict[str, int]:
    balances = _balances_for(session, sku_ids)
    return {sku: (balance.on_hand - balance.reserved) for sku, balance in balances.items()}


def _revisions_for(session: Session, sku_ids: set[str]) -> dict[str, int]:
    return {sku: balance.revision for sku, balance in _balances_for(session, sku_ids).items()}


def _balances_for(session: Session, sku_ids: set[str]) -> dict[str, StockBalance]:
    if not sku_ids:
        return {}
    rows = session.scalars(
        select(StockBalance).where(
            StockBalance.warehouse_id == WAREHOUSE_ID,
            StockBalance.sku_id.in_(sku_ids),
        )
    ).all()
    return {row.sku_id: row for row in rows}


def _candidates(session: Session, token: str) -> list[str]:
    style = token.split("-", 1)[0] if token else ""
    rows = list(session.scalars(select(Sku).where(Sku.active.is_(True))).all())
    matched = [row.id for row in rows if style and row.style == style]
    if matched:
        return sorted(matched)
    return sorted(row.id for row in rows)


def _resolve_customer(
    session: Session,
    code: str | None,
    name: str | None,
) -> tuple[Customer | None, tuple[str, list[str]] | None]:
    if code:
        customer = session.scalar(
            select(Customer).where(Customer.code == code, Customer.active.is_(True))
        )
        if customer is None:
            return None, (f"未知客户 {code}", [])
        return customer, None
    if not name:
        return None, ("缺少客户", [])
    found = list(
        session.scalars(
            select(Customer).where(Customer.display_name == name, Customer.active.is_(True))
        ).all()
    )
    if len(found) == 1:
        return found[0], None
    if len(found) > 1:
        codes = sorted(row.code for row in found)
        return None, (f"客户「{name}」有多个匹配，请选择", codes)
    return None, (f"未知客户 {name}", [])


def _resolve_sku_id(session: Session, *, style: str, color: str, size: str) -> str | None:
    color_code = _COLOR.get(color, color.upper())
    size_code = size.upper().replace("码", "")
    style_code = style.replace("款", "").strip()
    rows = list(
        session.scalars(
            select(Sku).where(
                Sku.style == style_code,
                Sku.color == color_code,
                Sku.size == size_code,
                Sku.active.is_(True),
            )
        ).all()
    )
    if len(rows) == 1:
        return rows[0].id
    return None


def _suspected_duplicate(
    session: Session,
    customer_id: str,
    lines: dict[str, SnapLine],
    now: datetime,
) -> str | None:
    target = tuple(sorted((sku, line.ordered_qty) for sku, line in lines.items()))
    cutoff = now - timedelta(hours=24)
    revisions = session.scalars(select(OrderRevision)).all()
    for revision in revisions:
        if _aware(revision.created_at) < cutoff:
            continue
        order = session.get(SalesOrder, revision.order_id)
        if order is None or order.customer_id != customer_id:
            continue
        if order.current_revision != revision.revision_no:
            continue
        rows = session.scalars(select(OrderLine).where(OrderLine.revision_id == revision.id)).all()
        found = tuple(sorted((row.sku_id, row.ordered_qty) for row in rows))
        if found == target:
            return order.id
    return None


def _add_event(
    session: Session,
    *,
    client_event_key: str,
    chain_id: str,
    order_id: str | None,
    intent_revision: int,
    kind: str,
    payload_ref: str | None,
    now: datetime,
) -> Event:
    event = Event(
        id=_id("EV"),
        source_event_key=client_event_key,
        thread_id=_THREAD,
        chain_id=chain_id,
        order_id=order_id,
        intent_revision=intent_revision,
        kind=kind,
        payload_ref=payload_ref,
        received_at=now,
    )
    session.add(event)
    return event


def _require_order(session: Session, order_id: str) -> SalesOrder:
    order = session.get(SalesOrder, order_id)
    if order is None:
        raise AppError(f"订单 {order_id} 不存在", code="NOT_FOUND", http_status=404)
    return order


def _next_order_id(session: Session) -> str:
    ids = list(session.scalars(select(SalesOrder.id)).all())
    numbers = [int(value[3:]) for value in ids if value.startswith("SO-") and value[3:].isdigit()]
    return f"SO-{max(numbers, default=1000) + 1}"


def _mentioned(changes: list[dict[str, object]]) -> set[str]:
    found: set[str] = set()
    for change in changes:
        for key in ("sku_id", "from_sku", "to_sku"):
            value = change.get(key)
            if value:
                found.add(str(value))
    return found


def _clean_change(change: dict[str, object]) -> dict[str, object]:
    mode = change.get("mode")
    if hasattr(mode, "value"):
        mode = mode.value
    return {
        "mode": mode,
        "sku_id": change.get("sku_id"),
        "from_sku": change.get("from_sku"),
        "to_sku": change.get("to_sku"),
        "qty": change.get("qty"),
        "include_executed": bool(change.get("include_executed")),
        "choice": change.get("choice"),
    }


def _err(message: str, code: str, status: int) -> tuple[None, AppError]:
    return None, AppError(message, code=code, http_status=status)


def _revoke_if_active(approval: Approval) -> None:
    if approval.status == ApprovalStatus.ACTIVE:
        approval.status = ApprovalStatus.REVOKED


def _hit_failpoint(name: str) -> None:
    if commit_failpoint.get() == name:
        raise RuntimeError(f"failpoint {name}")


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return _aware(value)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _resolve_choice(
    changes: list[dict[str, object]],
    lines: dict[str, SnapLine],
) -> list[dict[str, object]]:
    resolved: list[dict[str, object]] = []
    for change in changes:
        item = dict(change)
        if item.get("choice") == "only_uncut":
            source = lines.get(str(item.get("from_sku") or ""))
            if source is not None:
                floor = source.cut_qty if source.fulfillment_mode == "MTO" else source.shipped_qty
                item["qty"] = source.ordered_qty - floor
                item["include_executed"] = False
        resolved.append(item)
    return resolved


def _locked_clarification(
    session: Session,
    *,
    order: SalesOrder,
    lines: dict[str, SnapLine],
    source_refs: list[dict[str, object]],
    client_event_key: str,
    now: datetime,
    unexecuted_note: str,
) -> PlanOutcome:
    documents = _load_documents(session, order.id)
    preview = locked_preview(documents["production"], documents["purchase"])
    cut = sum(
        int(row["cut_qty"]) for row in documents["production"] if row["status"] != "CANCELLED"
    )
    question = (
        f"已裁剪的 {cut} 件不能改色。{unexecuted_note}。"
        "① 只把未开裁的部分改色；② 其他处理（例如追加生产）P0 不支持，请线下决定。"
    )
    labels = [f"{item['doc_id']} {item['verdict']}" for item in preview]
    _ = lines
    return _store_non_validated(
        session,
        order_id=order.id,
        chain_id=f"CH-{order.id}",
        base_revision=order.current_revision,
        intent_revision=order.intent_revision,
        status=TaskStatus.NEEDS_CLARIFICATION,
        questions=[question],
        candidates=[],
        issues=labels,
        source_refs=source_refs,
        client_event_key=client_event_key,
        now=now,
        kind="clarify",
        code="NEEDS_CLARIFICATION",
        message=question,
        verdicts=preview,
    )


def _load_documents(session: Session, order_id: str) -> dict[str, list[dict[str, Any]]]:
    production: list[dict[str, Any]] = []
    purchase: list[dict[str, Any]] = []
    for link in _current_links(session, order_id):
        if link.doc_type == "production_order":
            row = session.get(ProductionOrder, link.doc_id)
            if row is None:
                continue
            production.append(
                {
                    "doc_id": row.id,
                    "sku_id": row.sku_id,
                    "planned_qty": row.planned_qty,
                    "cut_qty": row.cut_qty,
                    "status": row.status,
                    "doc_revision": row.doc_revision,
                    "order_line_sku": link.order_line_sku,
                }
            )
        else:
            purchase_row = session.get(PurchaseOrder, link.doc_id)
            if purchase_row is None:
                continue
            purchase.append(
                {
                    "doc_id": purchase_row.id,
                    "material_id": purchase_row.material_id,
                    "ordered_qty": purchase_row.ordered_qty,
                    "received_qty": purchase_row.received_qty,
                    "status": purchase_row.status,
                    "change_state": purchase_row.change_state,
                    "doc_revision": purchase_row.doc_revision,
                    "production_order_id": purchase_row.production_order_id,
                    "order_line_sku": link.order_line_sku,
                }
            )
    return {"production": production, "purchase": purchase}


def _doc_fingerprint(documents: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for row in documents["production"]:
        found[str(row["doc_id"])] = {
            "doc_type": "production_order",
            "doc_revision": int(row["doc_revision"]),
            "status": row["status"],
            "cut_qty": int(row["cut_qty"]),
            "planned_qty": int(row["planned_qty"]),
            "sku_id": row["sku_id"],
        }
    for row in documents["purchase"]:
        found[str(row["doc_id"])] = {
            "doc_type": "purchase_order",
            "doc_revision": int(row["doc_revision"]),
            "status": row["status"],
            "received_qty": int(row["received_qty"]),
            "ordered_qty": int(row["ordered_qty"]),
            "change_state": row["change_state"],
            "material_id": row["material_id"],
        }
    return found


def _doc_fingerprint_loaded(
    documents: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    return _doc_fingerprint(documents)


def _usage_map(session: Session) -> dict[str, tuple[str, int]]:
    rows = session.scalars(select(MaterialUsage)).all()
    return {row.sku_id: (row.material_id, row.qty_per_piece) for row in rows}


def _current_links(session: Session, order_id: str | None = None) -> list[DownstreamLink]:
    statement = select(DownstreamLink)
    if order_id is not None:
        statement = statement.where(DownstreamLink.order_id == order_id)
    best: dict[tuple[str, str], DownstreamLink] = {}
    for row in session.scalars(statement).all():
        key = (row.doc_type, row.doc_id)
        current = best.get(key)
        if current is None or row.link_seq > current.link_seq:
            best[key] = row
    return list(best.values())


def _cut_by_sku(session: Session, order_id: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in _load_documents(session, order_id)["production"]:
        if row["status"] == "CANCELLED":
            continue
        sku = str(row["sku_id"])
        totals[sku] = totals.get(sku, 0) + int(row["cut_qty"])
    return totals


def _next_link_seq(session: Session, doc_type: str, doc_id: str) -> int:
    current = session.scalar(
        select(func.max(DownstreamLink.link_seq)).where(
            DownstreamLink.doc_type == doc_type,
            DownstreamLink.doc_id == doc_id,
        )
    )
    return int(current or 0) + 1


def _apply_downstream(
    session: Session,
    *,
    order_id: str,
    revision_no: int,
    verdicts: list[dict[str, object]],
    operation_id: str,
) -> None:
    for verdict in sorted(verdicts, key=lambda item: str(item["doc_id"])):
        doc_type = str(verdict["doc_type"])
        doc_id = str(verdict["doc_id"])
        after = verdict.get("after")
        if verdict["verdict"] in {"CHANGE_DRAFT", "CANCEL_DRAFT"} and isinstance(after, dict):
            if doc_type == "production_order":
                row = session.get(ProductionOrder, doc_id)
                if row is None:
                    raise AppError(f"没有生产单 {doc_id}", code="NOT_FOUND", http_status=404)
                row.sku_id = str(after["sku_id"])
                row.planned_qty = int(after["planned_qty"])
                row.cut_qty = int(after["cut_qty"])
                row.status = str(after["status"])
                row.doc_revision += 1
                rev_no = row.doc_revision
            else:
                row_po = session.get(PurchaseOrder, doc_id)
                if row_po is None:
                    raise AppError(f"没有采购单 {doc_id}", code="NOT_FOUND", http_status=404)
                row_po.material_id = str(after["material_id"])
                row_po.ordered_qty = int(after["ordered_qty"])
                row_po.received_qty = int(after["received_qty"])
                row_po.status = str(after["status"])
                row_po.change_state = str(after["change_state"])
                row_po.doc_revision += 1
                rev_no = row_po.doc_revision
            session.add(
                DocRevision(
                    id=f"DR-{doc_id}-{rev_no}",
                    doc_type=doc_type,
                    doc_id=doc_id,
                    rev_no=rev_no,
                    snapshot_json=dict(after),
                    operation_id=operation_id,
                )
            )
        session.add(
            DownstreamLink(
                id=_id("LNK"),
                doc_type=doc_type,
                doc_id=doc_id,
                order_id=order_id,
                order_revision=revision_no,
                order_line_sku=str(verdict["order_line_sku"]),
                link_seq=_next_link_seq(session, doc_type, doc_id),
                operation_id=operation_id,
            )
        )


def downstream_findings(session: Session) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    orders = list(session.scalars(select(SalesOrder)).all())
    usage = _usage_map(session)
    for order in orders:
        links = _current_links(session, order.id)
        for link in links:
            if link.order_revision != order.current_revision:
                findings.append(
                    {
                        "doc_id": link.doc_id,
                        "problem": (
                            f"{link.doc_id} 仍链接到修订 {link.order_revision}，"
                            f"当前修订是 {order.current_revision}"
                        ),
                        "repair": {
                            "action": "relink",
                            "order_id": order.id,
                            "order_revision": order.current_revision,
                        },
                    }
                )
            if link.doc_type == "production_order":
                row = session.get(ProductionOrder, link.doc_id)
                snapshot = session.scalar(
                    select(DocRevision).where(
                        DocRevision.doc_type == "production_order",
                        DocRevision.doc_id == link.doc_id,
                        DocRevision.rev_no == (row.doc_revision if row else -1),
                    )
                )
                if row is not None and snapshot is not None:
                    recorded = snapshot.snapshot_json or {}
                    if (
                        recorded.get("planned_qty") != row.planned_qty
                        or recorded.get("sku_id") != row.sku_id
                    ):
                        findings.append(
                            {
                                "doc_id": row.id,
                                "problem": f"{row.id} 当前内容与单据修订 {row.doc_revision} 不一致",
                                "repair": {
                                    "action": "draft",
                                    "sku_id": recorded.get("sku_id"),
                                    "planned_qty": recorded.get("planned_qty"),
                                },
                            }
                        )
        line_map = _load_lines(session, order.id)
        for sku, line in line_map.items():
            if line.fulfillment_mode != "MTO":
                continue
            planned = 0
            for link in links:
                if link.doc_type != "production_order":
                    continue
                row = session.get(ProductionOrder, link.doc_id)
                if row is None or row.status == "CANCELLED" or row.sku_id != sku:
                    continue
                planned += row.planned_qty
            if planned != line.ordered_qty:
                findings.append(
                    {
                        "doc_id": sku,
                        "problem": f"{sku} 生产计划 {planned} 与订单行 {line.ordered_qty} 不一致",
                        "repair": {"action": "draft", "ordered_qty": line.ordered_qty},
                    }
                )
        expected: dict[str, int] = {}
        actual: dict[str, int] = {}
        for link in links:
            if link.doc_type == "production_order":
                row = session.get(ProductionOrder, link.doc_id)
                if row is None or row.status == "CANCELLED":
                    continue
                material = usage.get(row.sku_id)
                if material is None:
                    continue
                material_id, per_piece = material
                expected[material_id] = expected.get(material_id, 0) + row.planned_qty * per_piece
            else:
                row_po = session.get(PurchaseOrder, link.doc_id)
                if row_po is None or row_po.status == "CANCELLED":
                    continue
                actual[row_po.material_id] = actual.get(row_po.material_id, 0) + row_po.ordered_qty
        if expected != actual:
            findings.append(
                {
                    "doc_id": order.id,
                    "problem": f"{order.id} 面料采购 {actual} 与单耗 {expected} 不一致",
                    "repair": {"action": "draft", "expected": expected},
                }
            )
    return findings


def downstream_problems(session: Session) -> list[str]:
    return [str(item["problem"]) for item in downstream_findings(session)]


def _stable(value: dict[str, dict[str, object]]) -> str:
    return canonical_hash(value)


_COLOR = {
    "黑": "BLACK",
    "黑色": "BLACK",
    "BLACK": "BLACK",
    "白": "WHITE",
    "白色": "WHITE",
    "WHITE": "WHITE",
    "蓝": "BLUE",
    "蓝色": "BLUE",
    "BLUE": "BLUE",
    "藏青": "NAVY",
    "NAVY": "NAVY",
}
