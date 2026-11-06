"""Run Router → sub-agents → the kernel, and persist every DispatchStep.

The kernel is the only writer of orders, stock, and downstream documents.
Agent payloads are checked, recorded, and then ignored by the precheck.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from schemas.business import BusinessEvent, ExtractionResult
from schemas.enums import BusinessEventType, DispatchStatus, TaskStatus
from schemas.subagent import AgentProposal, SubAgentSpec
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.budget import BudgetLedger
from app.adapters.fixture_model import FixtureLLMClient
from app.adapters.live_model import LiveLLMClient
from app.adapters.llm import LLMClient, resolve_mode
from app.adapters.uploads import validate_text
from app.config import Settings, get_settings
from app.db.models import (
    AuditRecord,
    DispatchStep,
    Event,
    InputPayload,
    JobAttempt,
    ModelUsage,
    SalesOrder,
    Task,
)
from app.db.session import begin_immediate
from app.domain.hashing import canonical_hash
from app.errors import AppError, ProposalRejected, SchemaInvalid, ToolNotAllowed
from app.observability import enrich, get_logger, log_event, observed, task_fields_from_factory
from app.orchestration.agents import RUNNERS, AgentRun, StepContext
from app.orchestration.defaults import build_default_registry
from app.orchestration.fixtures import golden_fixtures
from app.orchestration.intent_receipts import check_acquisition_order
from app.orchestration.order_scope import conflicting_order
from app.orchestration.proposal import accept_proposal
from app.orchestration.registry import Registry, tool_executor
from app.orchestration.router import PlannedStep, Router
from app.orchestration.state import enforce, sync_guard, transition, utcnow
from app.orchestration.state import guard as durable_guard
from app.orchestration.tools import ToolGateway
from app.services.kernel import _current_links as current_links
from app.services.kernel import (
    bound_task_id,
    invalidate_order,
    plan_change,
    plan_new_order,
)

logger = get_logger(__name__)


class AgentTimeout(Exception):
    """Raised inside a step when the caller injects a timeout. Tests do not sleep."""


@dataclass
class StepView:
    step_no: int
    agent_id: str
    agent_version: str
    capability: str
    status: str
    error_code: str | None = None
    skip_reason: str | None = None


@dataclass
class DispatchOutcome:
    task_id: str
    chain_id: str
    status: str
    plan_id: str | None
    order_id: str | None
    intent_revision: int
    plan_hash: str | None
    requires_human_approval: bool
    issues: list[str]
    error_code: str | None
    steps: list[StepView] = field(default_factory=list)


@dataclass
class _Work:
    changes: list[dict[str, object]] = field(default_factory=list)
    new_lines: list[dict[str, object]] = field(default_factory=list)
    model_calls: int = 0
    source_ids: list[str] = field(default_factory=list)


def _dispatch_text(
    session_factory: sessionmaker[Session],
    *,
    text: str,
    input_mode: str = "text",
    client_event_key: str,
    registry: Registry | None = None,
    now: datetime | None = None,
    fixtures: dict[str, dict[str, object]] | None = None,
    before_step: Callable[[StepContext], None] | None = None,
    timeout_agent: str | None = None,
    durable_task_id: str | None = None,
    source_ids: list[str] | None = None,
    settings: Settings | None = None,
    client_factory: Callable[[Settings, BudgetLedger], LLMClient] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> DispatchOutcome:
    """Parse one utterance, run the routed sub-agents, then precheck."""
    validate_text(text, allow_empty=bool(source_ids))
    moment = _moment(now)
    configured = settings or get_settings()
    if durable_task_id is None:
        from app.orchestration.sync_dispatch import execute

        return execute(
            session_factory,
            key=client_event_key,
            request_hash=canonical_hash(
                {"text": text, "input_mode": input_mode, "source_ids": source_ids or []}
            ),
            clock=clock or ((lambda: moment) if now is not None else utcnow),
            runtime_mode=resolve_mode(configured).mode.value,
            run=lambda task_id: _dispatch_text(
                session_factory,
                text=text,
                input_mode=input_mode,
                client_event_key=f"sync:{task_id}",
                registry=registry,
                now=moment,
                fixtures=fixtures,
                before_step=before_step,
                timeout_agent=timeout_agent,
                durable_task_id=task_id,
                source_ids=source_ids,
                settings=configured,
                client_factory=client_factory,
            ),
        )
    registered = registry or build_default_registry()
    router = Router(registered)
    task_id = durable_task_id
    from app.services.sheet_order import is_sheet_task

    with session_factory() as read_session:
        task = read_session.get(Task, task_id)
        if task is None or task.runtime_mode not in {"FIXTURE", "LIVE"}:
            raise AppError("任务运行模式不可验证", code="TASK_MODE_INVALID", http_status=409)
        task_mode = task.runtime_mode
        bounded = (
            durable_guard.get() is not None
            and task is not None
            and is_sheet_task(read_session, task)
        )
    use_live = not bounded and task_mode == "LIVE"
    if use_live and resolve_mode(configured).mode != "LIVE":
        return _fail(
            session_factory, task_id, "TASK_MODE_UNAVAILABLE", "任务需要已配置的 LIVE 消费者"
        )
    enrich(mode="LIVE" if use_live else "FIXTURE")
    client: LLMClient | None = None
    if not bounded:
        library = golden_fixtures()
        if fixtures:
            library.update(fixtures)
        client = FixtureLLMClient(library)
    if use_live:
        ledger = BudgetLedger(
            session_factory,
            cap_usd=configured.effective_live_budget_usd,
            price_in_per_mtok=configured.price_in_per_mtok,
            price_out_per_mtok=configured.price_out_per_mtok,
        )
        # LiveLLMClient selects/validates llm_provider; all providers share this ledger.
        client = (client_factory or LiveLLMClient)(configured, ledger)
    enrich(**task_fields_from_factory(session_factory, task_id))
    log_event(logger, "dispatch.start", outcome="started", duration_ms=0.0)
    routed = router.plan("unstructured", input_mode=input_mode)
    if not routed.ok:
        return _fail(session_factory, task_id, routed.code, routed.message)
    work = _Work(source_ids=source_ids or [])
    extraction: ExtractionResult | None = None
    for planned in routed.steps:
        outcome = _run_planned(
            session_factory,
            task_id=task_id,
            planned=planned,
            registry=registered,
            text=text,
            input_mode=input_mode,
            client=client,
            work=work,
            moment=moment,
            before_step=before_step,
            timeout_agent=timeout_agent,
        )
        if isinstance(outcome, DispatchOutcome):
            return outcome
        if outcome.extraction is not None:
            extraction = outcome.extraction
    if (
        extraction is None
        or extraction.business_event is None
        or extraction.conflicts
        or extraction.questions
    ):
        questions = extraction.questions if extraction else []
        message = questions[0] if questions else "解析结果没有业务事件"
        needs_answer = bool(questions or (extraction and extraction.conflicts))
        code = "NEEDS_CLARIFICATION" if needs_answer else "SCHEMA_INVALID"
        status = TaskStatus.NEEDS_CLARIFICATION if needs_answer else TaskStatus.FAILED
        return _fail(session_factory, task_id, code, message, status=status)
    event = extraction.business_event
    work.changes = _change_rows(event)
    work.new_lines = [
        {
            "sku_id": line.sku_id,
            "qty": line.qty,
            "style": line.style,
            "color": line.color,
            "size": line.size,
        }
        for line in event.new_lines
    ]
    follow = _route_event(router, session_factory, event)
    if not follow.ok:
        return _fail(session_factory, task_id, follow.code, follow.message)
    saw_downstream = False
    for planned in follow.steps:
        if planned.capability == "check.downstream":
            saw_downstream = True
        outcome = _run_planned(
            session_factory,
            task_id=task_id,
            planned=planned,
            registry=registered,
            text=text,
            input_mode=input_mode,
            client=client,
            work=work,
            moment=moment,
            before_step=before_step,
            timeout_agent=timeout_agent,
        )
        if isinstance(outcome, DispatchOutcome):
            return outcome
    if event.event_type == BusinessEventType.NEW_ORDER and not saw_downstream:
        spec = registered.find_by_capability("check.downstream")
        _record_skip(
            session_factory,
            task_id,
            spec=spec,
            capability="check.downstream",
            reason="无下游单据",
            now=moment,
        )
    return _precheck(
        session_factory,
        task_id=task_id,
        event=event,
        extraction=extraction,
        client_event_key=client_event_key,
        customer_name=extraction.customer_candidate,
        now=moment,
    )


dispatch_text = observed(
    logger,
    "dispatch.finish",
    fields=lambda durable_task_id, **_: {"task_id": durable_task_id},
)(_dispatch_text)


def receive_late_proposal(
    session_factory: sessionmaker[Session],
    proposal: AgentProposal,
    *,
    now: datetime | None = None,
) -> None:
    """Drop a proposal whose fencing token or intent no longer matches."""
    moment = _moment(now)
    session = session_factory()
    rejected: ProposalRejected | None = None
    try:
        with begin_immediate(session):
            attempt = session.scalar(
                select(JobAttempt)
                .where(JobAttempt.task_id == proposal.task_id)
                .order_by(JobAttempt.attempt_no.desc())
            )
            task = session.get(Task, proposal.task_id)
            if attempt is None or task is None:
                raise AppError("调度任务不存在", code="NOT_FOUND", http_status=404)
            try:
                accept_proposal(
                    proposal,
                    expected_fencing_token=attempt.fencing_token,
                    expected_intent_revision=task.intent_revision,
                )
            except ProposalRejected as exc:
                rejected = exc
                session.add(
                    AuditRecord(
                        id=_new_id("AUD"),
                        actor_type="subagent",
                        actor_id=proposal.agent_id,
                        action="proposal.discarded",
                        object_id=proposal.task_id,
                        operation_id=None,
                        before_hash=str(proposal.fencing_token),
                        after_hash=str(attempt.fencing_token),
                        source_refs=None,
                        at=moment,
                    )
                )
            else:
                raise AppError(
                    "晚到提案仍与 fencing 匹配，调度器不会把它写进业务表",
                    code="LATE_PROPOSAL",
                    http_status=409,
                )
    finally:
        session.close()
    if rejected is not None:
        raise rejected


def _run_planned(
    session_factory: sessionmaker[Session],
    *,
    task_id: str,
    planned: PlannedStep,
    registry: Registry,
    text: str,
    input_mode: str,
    client: LLMClient | None,
    work: _Work,
    moment: datetime,
    before_step: Callable[[StepContext], None] | None,
    timeout_agent: str | None,
) -> DispatchOutcome | AgentRun:
    if planned.skipped:
        spec = registry.find_by_capability(planned.capability)
        _record_skip(
            session_factory,
            task_id,
            spec=spec,
            capability=planned.capability,
            reason=planned.skip_reason or "跳过",
            now=moment,
        )
        return AgentRun(payload={})
    spec = registry.find_by_capability(planned.capability)
    if spec is None:
        return _fail(
            session_factory,
            task_id,
            "CAPABILITY_MISSING",
            f"没有子 Agent 提供能力 {planned.capability}",
        )
    runner = RUNNERS.get(spec.agent_id)
    if runner is None:
        return _fail(
            session_factory,
            task_id,
            "CAPABILITY_MISSING",
            f"子 Agent {spec.agent_id} 没有可执行实现",
        )
    probe_done = False
    last_message = "输出不合 schema"
    for _attempt in range(spec.max_retries + 1):
        result, retry, abort = _one_attempt(
            session_factory,
            task_id=task_id,
            spec=spec,
            capability=planned.capability,
            text=text,
            input_mode=input_mode,
            client=client,
            work=work,
            moment=moment,
            runner=runner,
            before_step=before_step if durable_guard.get() else None if probe_done else before_step,
            timeout_agent=timeout_agent,
        )
        probe_done = True
        if abort is not None:
            return abort
        if result is not None:
            return result
        if not retry:
            break
        last_message = (
            f"输出不符合 {spec.output_schema.model_id}，已重试 {spec.max_retries} 次后失败"
        )
    return _fail(session_factory, task_id, "SCHEMA_INVALID", last_message)


def _one_attempt(
    session_factory: sessionmaker[Session],
    *,
    task_id: str,
    spec: SubAgentSpec,
    capability: str,
    text: str,
    input_mode: str,
    client: LLMClient | None,
    work: _Work,
    moment: datetime,
    runner: Callable[[StepContext], AgentRun],
    before_step: Callable[[StepContext], None] | None,
    timeout_agent: str | None,
) -> tuple[AgentRun | None, bool, DispatchOutcome | None]:
    """Return (run, should_retry, abort outcome)."""
    session = session_factory()
    with session_factory() as usage_session:
        previous_usage = set(
            usage_session.scalars(select(ModelUsage.id).where(ModelUsage.task_id == task_id))
        )
    try:
        with session.no_autoflush:
            task, attempt = _lock(session, task_id)
            input_event_id = task.last_event_id
            gateway = ToolGateway(
                session,
                task_id=task.id,
                order_id=task.order_id,
                source_id="SRC-INPUT",
                source_text=text,
                changes=work.changes,
                new_lines=work.new_lines,
                now=moment,
            )
            ctx = StepContext(
                session=session,
                task=task,
                spec=spec,
                text=text,
                input_mode=input_mode,
                source_id="SRC-INPUT",
                llm=client,
                intent=task.intent_revision,
                fence=attempt.fencing_token,
                model_calls=work.model_calls,
                source_ids=work.source_ids,
            )
            token = tool_executor.set(gateway.execute)
            run: AgentRun | None = None
            retry = False
            code: str | None = None
            message = ""
            try:
                if before_step is not None:
                    before_step(ctx)
                if timeout_agent == spec.agent_id:
                    raise AgentTimeout
                run = runner(ctx)
            except AgentTimeout:
                code = "TIMEOUT"
                message = f"{spec.agent_id} 超时，本步结果作废"
            except SchemaInvalid as exc:
                code = "SCHEMA_INVALID"
                message = str(exc)
                retry = True
            except ToolNotAllowed as exc:
                code = "TOOL_NOT_ALLOWED"
                message = exc.message
            except AppError as exc:
                code = exc.code
                message = exc.message
            finally:
                work.model_calls = ctx.model_calls
                tool_executor.reset(token)
            # The runner's session only reads, with autoflush disabled. Persist its
            # changes through a fresh writer so the fence sees current committed state.
            with session_factory() as writer, begin_immediate(writer):
                task, attempt = _lock(writer, task_id)
                usage_rows = writer.scalars(
                    select(ModelUsage).where(
                        ModelUsage.task_id == task_id,
                        ModelUsage.id.not_in(previous_usage),
                    )
                ).all()
                live_tokens = sum(row.input_tokens + row.output_tokens for row in usage_rows)
                live_cost = sum((row.cost_usd for row in usage_rows), Decimal("0"))
                acquired_intent = None
                if code is None and run is not None and run.extraction is not None:
                    try:
                        acquired_intent = _acquire_late_intent(writer, task, input_event_id, run)
                    except AppError as exc:
                        if exc.code not in {
                            "STALE_INPUT_ORDER",
                            "INTENT_ORDER_UNPROVEN",
                            "ORDER_SCOPE",
                        }:
                            raise
                        # Chronology rejection occurs before any acquisition mutation.
                        # Finish visibly; do not copy this rejected parse's binding.
                        code, message = exc.code, exc.message
                if code not in {"STALE_INPUT_ORDER", "INTENT_ORDER_UNPROVEN", "ORDER_SCOPE"}:
                    _copy_step_writes(session, writer)
                if acquired_intent is not None:
                    # Do not copy the runner's pre-lock revision over the acquired one.
                    task.intent_revision = acquired_intent
                if code == "TIMEOUT":
                    attempt.fencing_token += 1
                if code is None and run is not None:
                    if run.extraction is not None and task.last_event_id:
                        receipt = writer.get(InputPayload, task.last_event_id)
                        assert receipt is not None  # last_event_id refers to persisted input.
                        if run.sheet_order is not None:
                            from app.services.sheet_order import validate_provenance

                            provenance = validate_provenance(writer, task, run.sheet_order)
                            receipt.body = {**receipt.body, "sheet_order_v1": provenance}
                        receipt.body = {
                            **receipt.body,
                            "extraction": run.extraction.model_dump(mode="json"),
                            "questions": run.extraction.questions,
                        }
                    _add_step(
                        writer,
                        task=task,
                        attempt=attempt,
                        spec=spec,
                        capability=capability,
                        status=DispatchStatus.SUCCEEDED,
                        now=moment,
                        output_ref=canonical_hash(run.payload),
                        tokens=live_tokens if usage_rows else run.tokens,
                        cost=live_cost,
                    )
                    return run, False, None
                status = DispatchStatus.TIMEOUT if code == "TIMEOUT" else DispatchStatus.FAILED
                _add_step(
                    writer,
                    task=task,
                    attempt=attempt,
                    spec=spec,
                    capability=capability,
                    status=status,
                    now=moment,
                    error_code=code,
                    tokens=live_tokens if usage_rows else (0 if code == "SCHEMA_INVALID" else None),
                    cost=live_cost
                    if usage_rows
                    else (Decimal("0") if code == "SCHEMA_INVALID" else None),
                )
                if retry:
                    return None, True, None
                transition(task, TaskStatus.FAILED)
                attempt.lease_until = None
                attempt.lease_owner = None
                task.error_code = code
                task.result_ref = message[:255]
                failed = _view(
                    writer,
                    task,
                    status=TaskStatus.FAILED,
                    error_code=code,
                    issues=[message],
                )
                return None, False, failed
    finally:
        session.close()


def _acquire_late_intent(
    writer: Session, task: Task, input_event_id: str | None, run: AgentRun
) -> int | None:
    """Acquire an unbound durable change's intent in the fenced acceptance transaction.

    The server-owned input JSON marker commits with the binding and survives retries.
    It is never read from provider payloads or user-submitted fields.
    Neither the model's revision nor its read snapshot authorizes acquisition.
    """
    if durable_guard.get() is None:
        return None
    if task.last_event_id != input_event_id:
        raise AppError("输入已更新", code="STALE_INTENT", http_status=409)
    receipt = writer.get(InputPayload, task.last_event_id) if task.last_event_id else None
    if receipt is None or receipt.task_id != task.id:
        raise AppError("输入已更新", code="STALE_INTENT", http_status=409)
    extraction = run.extraction
    assert extraction is not None
    event = extraction.business_event
    if conflicting_order(writer, task, event):
        raise AppError("输入订单与任务订单范围不一致", code="ORDER_SCOPE", http_status=409)
    origin = writer.get(Event, receipt.event_id)
    binding = receipt.body.get("late_bound_intent")
    if "late_bound_intent" in receipt.body:
        if (
            origin is None
            or origin.order_id is not None
            or not isinstance(binding, dict)
            or not isinstance(binding.get("order_id"), str)
            or not binding["order_id"]
            or type(binding.get("intent_revision")) is not int
            or binding["intent_revision"] <= 0
        ):
            raise AppError(
                "无法可靠确定订单意图的先后，请核对当前订单并选择订单重新提交",
                code="INTENT_ORDER_UNPROVEN",
                http_status=409,
            )
        order = writer.get(SalesOrder, binding["order_id"])
        if (
            order is None
            or task.order_id != order.id
            or task.intent_revision != binding["intent_revision"]
            or order.intent_revision != binding["intent_revision"]
        ):
            raise AppError("意图已更新", code="STALE_INTENT", http_status=409)
        if event is not None and event.order_id and event.order_id != order.id:
            raise AppError("输入订单与绑定订单不一致", code="ORDER_SCOPE", http_status=409)
        intent = order.intent_revision
    elif (
        origin is not None
        and origin.order_id is None
        and event is not None
        and event.event_type == BusinessEventType.CHANGE_ORDER
        and event.order_id
        and task.order_id in (None, event.order_id)
        and not extraction.questions
        and not extraction.conflicts
    ):
        order = writer.get(SalesOrder, event.order_id)
        if order is None:
            raise AppError("订单不存在", code="NOT_FOUND", http_status=404)
        check_acquisition_order(writer, receipt, order)
        invalidate_order(writer, order.id, exclude_task_id=task.id)
        order.intent_revision += 1
        intent = order.intent_revision
        receipt.body = {
            **receipt.body,
            "late_bound_intent": {"order_id": order.id, "intent_revision": intent},
        }
    else:
        return None
    run.extraction = extraction.model_copy(update={"intent_revision": intent})
    run.payload = run.extraction.model_dump(mode="json")
    return intent


def _copy_step_writes(source: Session, writer: Session) -> None:
    """Copy changed columns only; never merge a stale snapshot over current rows."""
    for obj in source.dirty:
        state = inspect(obj)
        current = writer.get(type(obj), state.identity)
        for column in state.mapper.column_attrs:
            if state.attrs[column.key].history.has_changes():
                setattr(current, column.key, getattr(obj, column.key))
    for obj in source.new:
        writer.add(
            type(obj)(
                **{
                    column.key: getattr(obj, column.key)
                    for column in inspect(obj).mapper.column_attrs
                }
            )
        )


def _precheck(
    session_factory: sessionmaker[Session],
    *,
    task_id: str,
    event: BusinessEvent,
    extraction: ExtractionResult,
    client_event_key: str,
    customer_name: str | None,
    now: datetime,
) -> DispatchOutcome:
    refs = [item.model_dump(mode="json") for item in extraction.source_refs]
    token = bound_task_id.set(task_id)
    try:
        try:
            if event.event_type == BusinessEventType.NEW_ORDER:
                outcome = plan_new_order(
                    session_factory,
                    lines=_new_line_rows(event),
                    customer_code=event.customer_code,
                    customer_name=customer_name,
                    source_refs=refs,
                    client_event_key=client_event_key,
                    now=now,
                )
            elif event.event_type in (
                BusinessEventType.CHANGE_ORDER,
                BusinessEventType.CANCEL_REMAINING,
            ):
                if not event.order_id:
                    return _fail(session_factory, task_id, "NEEDS_CLARIFICATION", "改单缺少订单号")
                base_revision, intent_revision = _versions(session_factory, event.order_id)
                bump_intent = True
                if durable_guard.get():
                    with session_factory() as read_session:
                        durable_task = read_session.get(Task, task_id)
                        if durable_task is None:
                            raise AppError("调度任务不存在", code="NOT_FOUND", http_status=404)
                        if durable_task.order_id:
                            if durable_task.order_id != event.order_id:
                                raise AppError(
                                    "输入订单与选择订单不一致", code="ORDER_SCOPE", http_status=409
                                )
                            if durable_task.intent_revision != intent_revision:
                                raise AppError("意图已更新", code="STALE_INTENT", http_status=409)
                            bump_intent = False
                outcome = plan_change(
                    session_factory,
                    order_id=event.order_id,
                    base_revision=base_revision,
                    intent_revision=intent_revision,
                    changes=_change_rows(event),
                    bump_intent=bump_intent,
                    source_refs=refs,
                    client_event_key=client_event_key,
                    now=now,
                )
            else:
                return _fail(
                    session_factory,
                    task_id,
                    "UNSUPPORTED_EVENT",
                    f"事件 {event.event_type} 在本阶段不进入预检",
                )
        except AppError as exc:
            _fail_if_running(session_factory, task_id, exc.code, exc.message)
            raise
    finally:
        bound_task_id.reset(token)
    issues = list(outcome.issues)
    if outcome.questions:
        issues.extend(question for question in outcome.questions if question not in issues)
    enrich(status=outcome.status, outcome="success")
    return _snapshot(
        session_factory,
        task_id,
        status=outcome.status,
        plan_id=outcome.plan_id,
        order_id=outcome.order_id,
        intent_revision=outcome.intent_revision,
        plan_hash=outcome.plan_hash,
        requires_human_approval=outcome.requires_human_approval,
        issues=issues,
        error_code=None,
    )


def _route_event(router: Router, session_factory: sessionmaker[Session], event: BusinessEvent):
    if event.event_type == BusinessEventType.NEW_ORDER:
        return router.plan(event.event_type)
    if event.event_type in (BusinessEventType.CHANGE_ORDER, BusinessEventType.CANCEL_REMAINING):
        has_downstream = bool(event.order_id) and _has_downstream(session_factory, event.order_id)
        return router.plan(event.event_type, has_downstream=has_downstream)
    if event.event_type == BusinessEventType.QUERY:
        return router.plan(event.event_type)
    return router.plan(event.event_type)


def _record_skip(
    session_factory: sessionmaker[Session],
    task_id: str,
    *,
    spec: SubAgentSpec | None,
    capability: str,
    reason: str,
    now: datetime,
) -> None:
    session = session_factory()
    try:
        with begin_immediate(session):
            task, attempt = _lock(session, task_id)
            _add_step(
                session,
                task=task,
                attempt=attempt,
                spec=spec,
                capability=capability,
                status=DispatchStatus.SKIPPED,
                now=now,
                skip_reason=reason,
                agent_id=None if spec is None else spec.agent_id,
                agent_version=None if spec is None else spec.version,
            )
    finally:
        session.close()


def _add_step(
    session: Session,
    *,
    task: Task,
    attempt: JobAttempt,
    spec: SubAgentSpec | None,
    capability: str,
    status: str,
    now: datetime,
    output_ref: str | None = None,
    error_code: str | None = None,
    skip_reason: str | None = None,
    tokens: int | None = None,
    cost: Decimal | None = None,
    agent_id: str | None = None,
    agent_version: str | None = None,
) -> None:
    guard = durable_guard.get() or sync_guard.get()
    if guard is not None:
        now = guard[2]()
        attempt.heartbeat_at = now
        attempt.lease_until = now + timedelta(seconds=guard[3])
        attempt.last_progress_at = now
    attempt.step_count += 1
    session.add(
        DispatchStep(
            id=_new_id("DS"),
            task_id=task.id,
            step_no=(
                session.scalar(
                    select(func.max(DispatchStep.step_no)).where(DispatchStep.task_id == task.id)
                )
                or 0
            )
            + 1,
            agent_id=agent_id or (spec.agent_id if spec else "doc-check"),
            agent_version=agent_version or (spec.version if spec else "0.1.0"),
            capability=capability,
            input_ref=None,
            output_ref=output_ref,
            status=status,
            started_at=now,
            ended_at=now,
            tokens=tokens,
            cost_usd=cost,
            error_code=error_code,
            skip_reason=skip_reason,
        )
    )
    session.flush()


def _fail(
    session_factory: sessionmaker[Session],
    task_id: str,
    code: str,
    message: str,
    *,
    status: str = TaskStatus.FAILED,
) -> DispatchOutcome:
    enrich(outcome="failed", status=status, error_code=code)
    session = session_factory()
    try:
        with begin_immediate(session):
            enforce(session)
            task = session.get(Task, task_id)
            if task is None:
                raise AppError("调度任务不存在", code="NOT_FOUND", http_status=404)
            if task.status == TaskStatus.RUNNING:
                transition(task, status)
                task.error_code = code
                task.result_ref = message[:255]
                payload = (
                    session.get(InputPayload, task.last_event_id) if task.last_event_id else None
                )
                if payload:
                    payload.body = {
                        **payload.body,
                        "questions": payload.body.get("questions") or [message],
                    }
                attempt = session.scalar(
                    select(JobAttempt)
                    .where(JobAttempt.task_id == task.id)
                    .order_by(JobAttempt.attempt_no.desc())
                )
                if attempt:
                    attempt.lease_until = None
                    attempt.lease_owner = None
            return _view(
                session,
                task,
                status=task.status,
                error_code=task.error_code,
                issues=[message],
            )
    finally:
        session.close()


def _fail_if_running(
    session_factory: sessionmaker[Session], task_id: str, code: str, message: str
) -> None:
    session = session_factory()
    try:
        with begin_immediate(session):
            task = session.get(Task, task_id)
            if task is not None and task.status == TaskStatus.RUNNING:
                # The kernel may already have persisted FAILED and released the
                # lease before raising its business error. A no-op must not mask
                # that error with STALE_ATTEMPT. Every mutation still requires
                # the current running attempt's fence.
                enforce(session)
                transition(task, TaskStatus.FAILED)
                task.error_code = code
                task.result_ref = message[:255]
    finally:
        session.close()


def _snapshot(
    session_factory: sessionmaker[Session],
    task_id: str,
    *,
    status: str,
    plan_id: str | None,
    order_id: str | None,
    intent_revision: int,
    plan_hash: str | None,
    requires_human_approval: bool,
    issues: list[str],
    error_code: str | None,
) -> DispatchOutcome:
    session = session_factory()
    try:
        task = session.get(Task, task_id)
        if task is None:
            raise AppError("调度任务不存在", code="NOT_FOUND", http_status=404)
        enrich(status=status, outcome="failed" if error_code else "success", error_code=error_code)
        return DispatchOutcome(
            task_id=task.id,
            chain_id=task.chain_id,
            status=status,
            plan_id=plan_id,
            order_id=order_id,
            intent_revision=intent_revision,
            plan_hash=plan_hash,
            requires_human_approval=requires_human_approval,
            issues=issues,
            error_code=error_code,
            steps=_step_views(session, task.id),
        )
    finally:
        session.close()


def _view(
    session: Session,
    task: Task,
    *,
    status: str,
    error_code: str | None,
    issues: list[str],
) -> DispatchOutcome:
    enrich(status=status, outcome="failed" if error_code else "success", error_code=error_code)
    return DispatchOutcome(
        task_id=task.id,
        chain_id=task.chain_id,
        status=status,
        plan_id=None,
        order_id=task.order_id,
        intent_revision=task.intent_revision,
        plan_hash=None,
        requires_human_approval=False,
        issues=issues,
        error_code=error_code,
        steps=_step_views(session, task.id),
    )


def _step_views(session: Session, task_id: str) -> list[StepView]:
    rows = session.scalars(
        select(DispatchStep).where(DispatchStep.task_id == task_id).order_by(DispatchStep.step_no)
    ).all()
    return [
        StepView(
            step_no=row.step_no,
            agent_id=row.agent_id,
            agent_version=row.agent_version,
            capability=row.capability,
            status=row.status,
            error_code=row.error_code,
            skip_reason=row.skip_reason,
        )
        for row in rows
    ]


def _lock(session: Session, task_id: str) -> tuple[Task, JobAttempt]:
    enforce(session)
    task = session.get(Task, task_id)
    attempt = session.scalar(
        select(JobAttempt)
        .where(JobAttempt.task_id == task_id)
        .order_by(JobAttempt.attempt_no.desc())
    )
    if task is None or attempt is None:
        raise AppError("调度任务不存在", code="NOT_FOUND", http_status=404)
    enrich(attempt_id=attempt.id)
    return task, attempt


def _versions(session_factory: sessionmaker[Session], order_id: str) -> tuple[int, int]:
    session = session_factory()
    try:
        order = session.get(SalesOrder, order_id)
        if order is None:
            raise AppError(f"订单 {order_id} 不存在", code="NOT_FOUND", http_status=404)
        return order.current_revision, order.intent_revision
    finally:
        session.close()


def _has_downstream(session_factory: sessionmaker[Session], order_id: str | None) -> bool:
    if not order_id:
        return False
    session = session_factory()
    try:
        return bool(current_links(session, order_id))
    finally:
        session.close()


def _change_rows(event: BusinessEvent) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in event.line_changes:
        item: dict[str, object] = {
            "mode": line.mode.value,
            "sku_id": line.sku_id,
            "from_sku": line.from_sku,
            "to_sku": line.to_sku,
            "qty": line.qty,
            "include_executed": False,
        }
        if event.clarification_choice:
            item["choice"] = event.clarification_choice
        rows.append(item)
    return rows


def _new_line_rows(event: BusinessEvent) -> list[dict[str, object]]:
    return [
        {
            "sku_id": line.sku_id,
            "qty": line.qty,
            "style": line.style,
            "color": line.color,
            "size": line.size,
        }
        for line in event.new_lines
    ]


def _moment(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"
