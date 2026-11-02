"""Stage 1A business kernel: plan, approve, and commit.

Downstream document relink is stage 1B. This module does not call a model.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
    Event,
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
    Task,
)
from app.db.session import begin_immediate
from app.domain.changes import ChangeEval, SnapLine, business_error, evaluate_changes
from app.domain.hashing import canonical_hash
from app.domain.inventory import MovementView, rebuild_balances
from app.domain.orders import derive_order_status
from app.errors import AppError

DEMO_ACTOR_ID = "demo-user"
_THREAD = "demo"

commit_failpoint: ContextVar[str | None] = ContextVar("commit_failpoint", default=None)
verify_failpoint: ContextVar[bool] = ContextVar("verify_failpoint", default=False)


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
            prior = _existing_outcome(session, client_event_key)
            if prior is not None:
                outcome, error = prior
                if error is not None:
                    raise error
                assert outcome is not None
                return outcome
            order = _require_order(session, order_id)
            _invalidate_order(session, order_id)
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
            return ApprovalOutcome(approval.id, plan.id, approval.expires_at)
    finally:
        session.close()


def commit_plan(
    session_factory: sessionmaker[Session],
    *,
    plan_id: str,
    approval_id: str,
    plan_hash: str,
    idempotency_key: str,
    actor_id: str = DEMO_ACTOR_ID,
    now: datetime | None = None,
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
    if not outcome.replayed:
        _post_verify(session_factory, outcome.operation_id)
    return outcome


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
    if bump_intent:
        _invalidate_order(session, order_id)
        order.intent_revision += 1
    lines = _load_lines(session, order_id)
    known = _known_skus(session)
    evaluation = evaluate_changes(
        lines=lines,
        changes=changes,
        stock_available=_available_map(session, _mentioned(changes) | set(lines)),
        known_skus=known,
        candidates_for={sku: _candidates(session, sku) for sku in _mentioned(changes)},
    )
    intent = order.intent_revision
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
    payload = _payload(
        plan_kind=plan.plan_kind,
        order_id=order.id if order is not None else None,
        base_revision=plan.base_order_revision,
        intent_revision=plan.intent_revision,
        evaluation=evaluation,
        stock_revisions={sku: balances[sku].revision for sku in balances},
    )
    recomputed = canonical_hash(payload)
    if recomputed != approval.plan_hash or recomputed != plan.plan_hash:
        _revoke_if_active(approval)
        return None, AppError("计划内容与批准哈希不一致", code="HASH_MISMATCH", http_status=409)
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
    task.status = TaskStatus.SUCCEEDED
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
) -> PlanOutcome:
    stock_revisions = _revisions_for(session, set(evaluation.reservation_delta))
    payload = _payload(
        plan_kind=plan_kind,
        order_id=order_id,
        base_revision=base_revision,
        intent_revision=intent_revision,
        evaluation=evaluation,
        stock_revisions=stock_revisions,
    )
    plan_hash = canonical_hash(payload)
    dependencies = canonical_hash(
        {
            "base_revision": base_revision,
            "intent_revision": intent_revision,
            "stock_revisions": stock_revisions,
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
            "outcome": {"kind": "validated"},
        },
        client_event_key=client_event_key,
        now=now,
        error_code=None,
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
    session.add(
        Task(
            id=_id("T"),
            chain_id=chain_id,
            order_id=order_id,
            status=TaskStatus.FAILED,
            intent_revision=intent_revision,
            last_event_id=event.id,
            cancel_requested=False,
            deadline_at=None,
            result_ref=None,
            error_code=code,
        )
    )
    session.flush()
    event.payload_ref = message[:200]


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
) -> tuple[Task, Plan]:
    task_id = _id("T")
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
    task = Task(
        id=task_id,
        chain_id=chain_id,
        order_id=order_id,
        status=task_status,
        intent_revision=intent_revision,
        last_event_id=event.id,
        cancel_requested=False,
        deadline_at=None,
        result_ref=None,
        error_code=error_code,
    )
    session.add(task)
    session.flush()
    plan = Plan(
        id=plan_id,
        task_id=task_id,
        plan_kind=plan_kind,
        base_order_revision=base_revision,
        intent_revision=intent_revision,
        chain_intent_revision=intent_revision,
        expected_stock_revisions=stock_revisions,
        expected_doc_revisions={},
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
    base_revision: int,
    intent_revision: int,
    evaluation: ChangeEval,
    stock_revisions: dict[str, int],
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "plan_kind": plan_kind,
        "order_id": order_id,
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
        "expected_doc_revisions": {},
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


def _invalidate_order(session: Session, order_id: str) -> None:
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
        if task.status in {TaskStatus.AWAITING_APPROVAL, TaskStatus.NEEDS_CLARIFICATION}:
            task.status = TaskStatus.SUPERSEDED


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
    lines: dict[str, SnapLine] = {}
    rows = session.scalars(select(OrderLine).where(OrderLine.revision_id == revision.id)).all()
    for row in rows:
        lines[row.sku_id] = SnapLine(
            row.sku_id,
            row.ordered_qty,
            row.fulfillment_mode,
            int(shipped.get(row.sku_id) or 0),
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
