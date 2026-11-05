"""Bounded allowlist projection: no business services, raw bodies, or writes."""

from pathlib import Path

from schemas.task_audit import (
    AuditApproval,
    AuditInput,
    AuditLimits,
    AuditOperation,
    AuditPlan,
    AuditSemantics,
    AuditSource,
    AuditTask,
    BusinessAudit,
    CommitReceipt,
    CurrentVerification,
    TaskAudit,
)
from sqlalchemy import case, func, inspect, select, true
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.models import (
    Approval,
    AuditRecord,
    Event,
    InputPayload,
    InputSource,
    Operation,
    Plan,
    Task,
    TaskSource,
)
from app.errors import AppError
from app.orchestration.state import aware

MAX_ROWS = 1000
MAX_OUTPUT_BYTES = 4 * 1024 * 1024


def _limit_error() -> AppError:
    return AppError(
        "审计报告超过导出上限，不返回截断报告", code="AUDIT_EXPORT_LIMIT", http_status=413
    )


def _invalid() -> AppError:
    return AppError(
        "审计记录关联或格式不完整，无法导出", code="AUDIT_EXPORT_INVALID", http_status=409
    )


def _rows(session, query):
    rows = session.execute(query.limit(MAX_ROWS + 1)).all()
    if len(rows) > MAX_ROWS:
        raise _limit_error()
    return rows


def _source_links(session, model, ids, task_id):
    """Project source_id inside SQLite; never load source_refs/excerpts into Python.

    Restrict ownership before returning IDs. Invalid shapes fail closed;
    unowned fixture/chat refs are deliberately outside this export scope.
    """
    if not ids:
        return {}
    for row in _rows(
        session,
        select(model.id, func.json_type(model.source_refs).label("container_type"))
        .where(model.id.in_(ids))
        .order_by(model.id),
    ):
        if row.container_type != "array" and not (
            model is AuditRecord and row.container_type in (None, "null")
        ):
            raise _invalid()
    # AuditRecord.source_refs is nullable; absent audit refs are an empty set.
    reference_array = case(
        (func.json_type(model.source_refs) == "array", model.source_refs), else_="[]"
    )
    refs = func.json_each(reference_array).table_valued("value", "type")
    source_type = case(
        (refs.c.type == "object", func.json_type(refs.c.value, "$.source_id")), else_=None
    )
    if (
        session.scalar(
            select(model.id)
            .select_from(model)
            .join(refs, true())
            .where(model.id.in_(ids), source_type.is_distinct_from("text"))
            .limit(1)
        )
        is not None
    ):
        raise _invalid()
    source_id = case(
        (source_type == "text", func.json_extract(refs.c.value, "$.source_id")), else_=None
    )
    rows = _rows(
        session,
        select(model.id, TaskSource.source_id)
        .select_from(model)
        .join(refs, true())
        .join(TaskSource, TaskSource.source_id == source_id)
        .where(model.id.in_(ids), TaskSource.task_id == task_id)
        .distinct()
        .order_by(model.id, TaskSource.source_id),
    )
    result: dict[str, list[str]] = {}
    for owner, source in rows:
        result.setdefault(owner, []).append(source)
    return result


def _project(session: Session, task_id: str) -> TaskAudit:
    # All inspection and SELECTs share this Session's real deferred transaction.
    if not inspect(session.connection()).has_table("task"):
        raise AppError("任务不存在", code="TASK_NOT_FOUND", http_status=404)
    task = session.execute(
        select(
            Task.id, Task.order_id, Task.status, Task.intent_revision, Task.cancel_requested
        ).where(Task.id == task_id)
    ).first()
    if task is None:
        raise AppError("任务不存在", code="TASK_NOT_FOUND", http_status=404)
    inputs = []
    sequences = []
    sequence = func.json_extract(InputPayload.body, "$.input_sequence")
    sequence_type = func.json_type(InputPayload.body, "$.input_sequence")
    for row in _rows(
        session,
        select(
            Event.id,
            sequence,
            sequence_type,
            Event.kind,
            Event.received_at,
            Event.intent_revision,
            func.json_type(InputPayload.body).label("body_type"),
        )
        .join(InputPayload, InputPayload.event_id == Event.id)
        .where(InputPayload.task_id == task_id)
        .order_by(Event.received_at, Event.id),
    ):
        if row.body_type != "object":
            raise _invalid()
        valid = row[2] == "integer" and isinstance(row[1], int) and row[1] > 0
        if row[2] not in (None, "null") and not valid:
            raise _invalid()
        sequences.append(row[1] if valid else None)
        inputs.append(
            AuditInput(
                event_id=row.id,
                input_sequence=row[1] if valid else None,
                kind=row.kind if row.kind in {"input.submit", "clarify_answer"} else "unknown",
                received_at=aware(row.received_at),
                intent_revision=row.intent_revision,
            )
        )
    inputs.sort(
        key=lambda row: (
            row.input_sequence is None,
            row.input_sequence or 0,
            row.received_at,
            row.event_id,
        )
    )
    plan_rows = _rows(
        session,
        select(
            Plan.id,
            Plan.plan_kind,
            Plan.status,
            Plan.base_order_revision,
            Plan.intent_revision,
            Plan.chain_intent_revision,
            Plan.plan_hash,
        )
        .where(Plan.task_id == task_id)
        .order_by(Plan.id),
    )
    plan_refs = {row.id: f"plan-{number}" for number, row in enumerate(plan_rows, 1)}
    plan_sources = _source_links(session, Plan, list(plan_refs), task_id)
    plans = {
        row.id: AuditPlan(
            report_ref=plan_refs[row.id],
            kind=row.plan_kind,
            status=row.status,
            base_order_revision=row.base_order_revision,
            intent_revision=row.intent_revision,
            chain_intent_revision=row.chain_intent_revision,
            plan_hash=row.plan_hash,
            source_ids=plan_sources.get(row.id, []),
        )
        for row in plan_rows
    }
    operation_rows = _rows(
        session,
        select(
            Operation.id,
            Operation.plan_id,
            func.json_extract(Operation.result_json, "$.task_id").label("receipt_task"),
            func.json_type(Operation.result_json, "$.task_id").label("receipt_task_type"),
            func.json_extract(Operation.result_json, "$.plan_id").label("receipt_plan"),
            func.json_type(Operation.result_json, "$.plan_id").label("receipt_plan_type"),
            func.json_extract(Operation.result_json, "$.order_id").label("receipt_order"),
            func.json_type(Operation.result_json, "$.order_id").label("receipt_order_type"),
            func.json_extract(Operation.result_json, "$.revision").label("receipt_revision"),
            func.json_type(Operation.result_json, "$.revision").label("revision_type"),
            func.json_extract(Operation.result_json, "$.audit_id").label("receipt_audit"),
            func.json_type(Operation.result_json, "$.audit_id").label("receipt_audit_type"),
            Operation.committed_verification_pending,
            Operation.verification_retry_attempts,
            Operation.verification_next_attempt_at,
            Operation.verification_manual_required,
        )
        .join(Plan, Plan.id == Operation.plan_id)
        .where(Plan.task_id == task_id, Operation.status == "COMMITTED")
        .order_by(Operation.id),
    )
    operations = {}
    for row in operation_rows:
        if (
            row.revision_type != "integer"
            or any(
                kind != "text"
                for kind in (
                    row.receipt_task_type,
                    row.receipt_plan_type,
                    row.receipt_order_type,
                    row.receipt_audit_type,
                )
            )
            or row.receipt_task != task_id
            or row.receipt_plan != row.plan_id
            or row.receipt_order != task.order_id
        ):
            raise _invalid()
        operations[row.id] = AuditOperation(
            operation_id=row.id,
            plan_ref=plan_refs[row.plan_id],
            receipt=CommitReceipt(
                order_id=row.receipt_order,
                revision=row.receipt_revision,
                audit_id=row.receipt_audit,
            ),
            current_verification=CurrentVerification(
                pending=row.committed_verification_pending,
                retry_attempts=row.verification_retry_attempts,
                next_attempt_at=aware(row.verification_next_attempt_at)
                if row.verification_next_attempt_at
                else None,
                manual_required=row.verification_manual_required,
            ),
        )
    for row in _rows(
        session,
        select(
            Approval.plan_id,
            Approval.status,
            Approval.intent_revision,
            Approval.expires_at,
            Approval.consumed_operation_id,
        )
        .join(Plan, Plan.id == Approval.plan_id)
        .where(Plan.task_id == task_id)
        .order_by(Approval.plan_id, Approval.expires_at, Approval.id),
    ):
        if row.consumed_operation_id and row.consumed_operation_id not in operations:
            raise _invalid()
        plans[row.plan_id].approvals.append(
            AuditApproval(
                status=row.status,
                intent_revision=row.intent_revision,
                expires_at=aware(row.expires_at),
                consumed_operation_id=row.consumed_operation_id,
            )
        )
    audit_rows = _rows(
        session,
        select(
            AuditRecord.id,
            AuditRecord.operation_id,
            AuditRecord.actor_type,
            AuditRecord.object_id,
            AuditRecord.at,
        )
        .join(Operation, Operation.id == AuditRecord.operation_id)
        .join(Plan, Plan.id == Operation.plan_id)
        .where(
            Plan.task_id == task_id, Operation.status == "COMMITTED", AuditRecord.action == "commit"
        )
        .order_by(AuditRecord.at, AuditRecord.id),
    )
    audit_sources = _source_links(session, AuditRecord, [row.id for row in audit_rows], task_id)
    audits = []
    for row in audit_rows:
        operation = operations[row.operation_id]
        if row.object_id != operation.receipt.order_id or row.id != operation.receipt.audit_id:
            raise _invalid()
        audits.append(
            BusinessAudit(
                audit_id=row.id,
                operation_id=row.operation_id,
                plan_ref=operation.plan_ref,
                action="commit",
                actor_type=row.actor_type
                if row.actor_type in {"human", "subagent", "system"}
                else "unknown",
                object_id=row.object_id,
                at=aware(row.at),
                source_ids=audit_sources.get(row.id, []),
            )
        )
    if {row.audit_id for row in audits} != {op.receipt.audit_id for op in operations.values()}:
        raise _invalid()
    sources = [
        AuditSource(
            source_id=row.id,
            sha256=row.sha256,
            media_type=row.media_type,
            size=row.size,
            parser_version=row.parser_version,
        )
        for row in _rows(
            session,
            select(
                InputSource.id,
                InputSource.sha256,
                InputSource.media_type,
                InputSource.size,
                InputSource.parser_version,
            )
            .join(TaskSource, TaskSource.source_id == InputSource.id)
            .where(TaskSource.task_id == task_id)
            .order_by(InputSource.id),
        )
    ]
    return TaskAudit(
        semantics=AuditSemantics(
            causal_sequence_complete=bool(sequences)
            and set(sequences) == set(range(1, len(sequences) + 1))
        ),
        limits=AuditLimits(rows_per_collection=MAX_ROWS, output_bytes=MAX_OUTPUT_BYTES),
        task=AuditTask(
            task_id=task.id,
            order_id=task.order_id,
            status=task.status,
            intent_revision=task.intent_revision,
            cancel_requested=task.cancel_requested,
        ),
        inputs=inputs,
        plans=list(plans.values()),
        committed_operations=list(operations.values()),
        business_audits=audits,
        sources=sources,
    )


def export_task_audit(engine: Engine, task_id: str) -> bytes:
    """A fresh session closes by rollback; make_engine emits explicit BEGIN.

    Check the path before connecting so a missing database is never created.
    This does not run startup reconciliation, verification, or migrations.
    """
    database = engine.url.database
    if task_id == "T-STUB" or (
        database not in (None, "", ":memory:")
        and (not Path(database).is_file() or Path(database).stat().st_size == 0)
    ):
        raise AppError("任务不存在", code="TASK_NOT_FOUND", http_status=404)
    try:
        with Session(engine, autoflush=False) as session:
            report = _project(session, task_id)
            content = report.model_dump_json().encode("utf-8")
            if len(content) > MAX_OUTPUT_BYTES:
                raise _limit_error()
            return content
    except (ValueError, TypeError) as exc:
        # Includes schema validation and SQLite datetime row processing. Do not
        # expose malformed persisted values or produce an incomplete report.
        raise _invalid() from exc
    except OperationalError as exc:
        # SQLite JSON parsing failures are invalid stored evidence, not a reason
        # to return partial provenance or expose a driver message.
        if "malformed JSON" in str(exc.orig):
            raise _invalid() from exc
        raise
