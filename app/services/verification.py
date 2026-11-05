"""Durable, bounded readback retries. Never commit or repair business data."""

from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import uuid4

from sqlalchemy import or_, select

from app.config import Settings, get_settings
from app.db.models import Operation, Plan
from app.db.session import begin_immediate
from app.errors import AppError
from app.observability import (
    enrich,
    exception_code,
    get_logger,
    log_context,
    log_event,
    task_log_fields,
)
from app.orchestration.state import aware

logger = get_logger(__name__)


def _escalate(operation):
    newly_required = not operation.verification_manual_required
    operation.verification_manual_required = True
    operation.verification_next_attempt_at = None
    operation.verification_lease_until = None
    operation.verification_lease_token = None
    if newly_required:
        log_event(
            logger, "verification.manual_required", operation_id=operation.id, outcome="required"
        )


def _claim(factory, operation_id, moment, settings, *, manual=False, clock=None):
    with factory() as session, begin_immediate(session):
        if clock is not None:
            moment = aware(clock())  # Lock waits must not consume a newly issued lease.
        operation = session.get(Operation, operation_id)
        if (
            operation is None
            or operation.status != "COMMITTED"
            or not operation.committed_verification_pending
        ):
            return None
        if (
            operation.verification_lease_until
            and aware(operation.verification_lease_until) > moment
        ):
            return None
        if manual:
            if not operation.verification_manual_required:
                return None
        else:
            if operation.verification_manual_required:
                return None
            if operation.verification_retry_attempts >= settings.verification_max_retries:
                _escalate(operation)
                return None
            if operation.verification_next_attempt_at and (
                aware(operation.verification_next_attempt_at) > moment
            ):
                return None
            operation.verification_retry_attempts += 1
        token = str(uuid4())
        operation.verification_lease_token = token
        operation.verification_lease_until = moment + timedelta(
            seconds=settings.verification_lease_seconds
        )
        return token


def _attempt(factory, operation_id, token, moment, settings, *, manual=False, clock=None):
    from app.services.kernel import _post_verify

    started = perf_counter()
    outcome, error_code = "error", None
    with log_context(exclude=("mode", "task_id", "attempt_id", "order_id")):
        try:
            with factory() as session:
                operation = session.get(Operation, operation_id)
                plan = (
                    session.get(Plan, operation.plan_id)
                    if operation and operation.plan_id
                    else None
                )
                if plan:
                    enrich(**task_log_fields(session, plan.task_id))
        except Exception:
            pass  # Optional log fields never block readback.
        try:
            _post_verify(factory, operation_id)
            with factory() as session:
                operation = session.get(Operation, operation_id)
                outcome = (
                    "cleared"
                    if operation and not operation.committed_verification_pending
                    else "still_pending"
                )
        except Exception as exc:
            error_code = exception_code(exc)
        with factory() as session, begin_immediate(session):
            operation = session.get(Operation, operation_id)
            if operation and not operation.committed_verification_pending:
                outcome = "cleared"  # A concurrent pass is authoritative, even after our exception.
            # An expired worker must not overwrite a newer attempt's schedule or lease.
            if operation and operation.verification_lease_token == token:
                operation.verification_lease_until = None
                operation.verification_lease_token = None
                if not operation.committed_verification_pending:
                    operation.verification_manual_required = False
                    operation.verification_next_attempt_at = None
                    operation.verification_last_error_code = None
                else:
                    operation.verification_last_error_code = error_code or "READBACK_PENDING"
                    if (
                        manual
                        or operation.verification_retry_attempts
                        >= settings.verification_max_retries
                    ):
                        _escalate(operation)
                    else:
                        delay = min(
                            settings.verification_max_delay_seconds,
                            settings.verification_base_delay_seconds
                            * 2 ** (operation.verification_retry_attempts - 1),
                        )
                        completed_at = aware(clock()) if clock else moment
                        operation.verification_next_attempt_at = completed_at + timedelta(
                            seconds=delay
                        )
        log_event(
            logger,
            "verification.manual" if manual else "verification.retry",
            operation_id=operation_id,
            outcome=outcome,
            error_code=error_code,
            duration_ms=round((perf_counter() - started) * 1000, 3),
        )
    return outcome == "cleared"


def retry_pending(factory, operation_id=None, *, now=None, settings=None, clock=None) -> int:
    """Claim at most one bounded batch; initial commit readback is separate."""
    settings = settings or get_settings()
    clock = clock or (lambda: now if now is not None else datetime.now(UTC))
    moment = aware(clock())
    with factory() as session:
        query = select(Operation.id).where(
            Operation.status == "COMMITTED",
            Operation.committed_verification_pending.is_(True),
            Operation.verification_manual_required.is_(False),
            or_(
                Operation.verification_next_attempt_at.is_(None),
                Operation.verification_next_attempt_at <= moment,
            ),
            or_(
                Operation.verification_lease_until.is_(None),
                Operation.verification_lease_until <= moment,
            ),
        )
        if operation_id is not None:
            query = query.where(Operation.id == operation_id)
        pending = list(
            session.scalars(
                query.order_by(Operation.verification_next_attempt_at, Operation.id).limit(
                    settings.verification_batch_size
                )
            )
        )
    attempts = 0
    for pending_id in pending:
        moment = aware(clock())
        token = _claim(factory, pending_id, moment, settings, clock=clock)
        if token is None:
            continue
        attempts += 1
        _attempt(factory, pending_id, token, moment, settings, clock=clock)
    return attempts


def verify_manually(
    factory, operation_id: str, *, now=None, settings: Settings | None = None, clock=None
) -> bool:
    """An operator requests one read-only check, without resetting the automatic cap."""
    settings = settings or get_settings()
    clock = clock or (lambda: now if now is not None else datetime.now(UTC))
    moment = aware(clock())
    with factory() as session:
        operation = session.get(Operation, operation_id)
        if operation is None or operation.status != "COMMITTED":
            raise AppError("已提交操作不存在", code="NOT_FOUND", http_status=404)
        if not operation.committed_verification_pending:
            return True
    token = _claim(factory, operation_id, moment, settings, manual=True, clock=clock)
    if token is None:
        raise AppError(
            "核验尚未转人工或已有核验正在进行", code="VERIFICATION_NOT_READY", http_status=409
        )
    return _attempt(factory, operation_id, token, moment, settings, manual=True, clock=clock)
