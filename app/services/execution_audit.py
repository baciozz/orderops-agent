"""Retain the hash-approved payload atomically; compare explicitly limited fields.

This protocol does not bind a parent graph or a complete shared dependency set.
No report from this module can clear pending or authorize a business action.
"""

import json
import sqlite3
from typing import Any

from schemas.execution_audit import (
    ExecutionAudit,
    ExecutionField,
    ExecutionFieldResult,
    ObservedDependency,
    UncoveredScope,
)
from seeds.golden import WAREHOUSE_ID
from sqlalchemy import String, cast, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import (
    Approval,
    AuditRecord,
    DownstreamLink,
    ExecutionEvidence,
    Operation,
    OrderLine,
    OrderRevision,
    Plan,
    Reservation,
    SalesOrder,
    StockMovement,
    Task,
)
from app.domain.hashing import canonical_hash
from app.errors import AppError
from app.services.link_audit import (
    MAX_OUTPUT_BYTES,
    MAX_ROWS,
    _compare_links,
    _identifier,
    _readonly_engine,
    _require,
    _rows,
    _Uncertain,
)

RECEIPT_FIELDS = {"order_id", "revision", "audit_id", "task_id", "chain_id", "plan_id"}
PAYLOAD_FIELDS = {
    "schema_version",
    "plan_kind",
    "order_id",
    "customer_id",
    "base_revision",
    "intent_revision",
    "lines",
    "reservation_delta",
    "stock_revisions",
    "downstream",
    "expected_doc_revisions",
}


def _integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _payload_valid(payload: Any, *, bounded: bool = True) -> None:
    _require(isinstance(payload, dict), "EVIDENCE_PAYLOAD_INVALID")
    _require(
        set(payload) in (PAYLOAD_FIELDS, PAYLOAD_FIELDS | {"sheet_order_v1"})
        and payload["schema_version"] == "1.0"
        and payload["plan_kind"] in {"create", "change"}
        and _identifier(payload["customer_id"])
        and _integer(payload["base_revision"])
        and _integer(payload["intent_revision"])
        and (
            (
                payload["plan_kind"] == "create"
                and payload["order_id"] is None
                and payload["base_revision"] == 0
            )
            or (
                payload["plan_kind"] == "change"
                and _identifier(payload["order_id"])
                and payload["base_revision"] > 0
            )
        ),
        "EVIDENCE_PAYLOAD_INVALID",
    )
    for name in ("lines", "reservation_delta", "downstream"):
        _require(isinstance(payload[name], list), "EVIDENCE_PAYLOAD_INVALID")
        if bounded:
            _require(len(payload[name]) <= MAX_ROWS, "EVIDENCE_LIMIT")
    lines = payload["lines"]
    _require(
        all(
            isinstance(row, dict)
            and set(row) == {"sku_id", "ordered_qty", "fulfillment_mode"}
            and _identifier(row["sku_id"])
            and _integer(row["ordered_qty"])
            and row["fulfillment_mode"] in {"STOCK", "MTO"}
            for row in lines
        ),
        "EVIDENCE_PAYLOAD_INVALID",
    )
    _require(len({row["sku_id"] for row in lines}) == len(lines), "EVIDENCE_PAYLOAD_INVALID")
    deltas = payload["reservation_delta"]
    _require(
        all(
            isinstance(row, dict)
            and set(row) == {"sku_id", "qty"}
            and _identifier(row["sku_id"])
            and type(row["qty"]) is int
            for row in deltas
        ),
        "EVIDENCE_PAYLOAD_INVALID",
    )
    stock = payload["stock_revisions"]
    _require(
        isinstance(stock, dict)
        and (not bounded or len(stock) <= MAX_ROWS)
        and all(_identifier(key) and _integer(value) for key, value in stock.items())
        and len({row["sku_id"] for row in deltas}) == len(deltas)
        and set(stock) == {row["sku_id"] for row in deltas},
        "EVIDENCE_PAYLOAD_INVALID",
    )
    _require(isinstance(payload["expected_doc_revisions"], dict), "EVIDENCE_PAYLOAD_INVALID")
    edges = payload["downstream"]
    _require(
        all(
            isinstance(row, dict)
            and row.get("doc_type") in {"production_order", "purchase_order"}
            and _identifier(row.get("doc_id"))
            and _identifier(row.get("order_line_sku"))
            and row.get("verdict")
            in {"UNAFFECTED_RELINK", "EXECUTED_LOCKED", "CHANGE_DRAFT", "CANCEL_DRAFT"}
            and isinstance(row.get("after"), dict)
            and "before" in row
            and (row["before"] is None or isinstance(row["before"], dict))
            for row in edges
        ),
        "EVIDENCE_PAYLOAD_INVALID",
    )
    _require(
        len({(row["doc_type"], row["doc_id"]) for row in edges}) == len(edges),
        "EVIDENCE_PAYLOAD_INVALID",
    )


def capture_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Called after approval hash comparison, BEFORE any business effects."""
    try:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        snapshot = json.loads(raw)
        # Read-report limits must not become an unrelated business-size limit.
        # Retain the entire already-approved value; an oversized audit is uncertain.
        _payload_valid(snapshot, bounded=False)
        return snapshot
    except (_Uncertain, ValueError, TypeError, KeyError, RecursionError):
        raise AppError(
            "批准执行证据无法完整保存，未执行业务提交",
            code="EXECUTION_EVIDENCE_INVALID",
            http_status=409,
        ) from None


def retain_execution(
    session: Session, *, operation: Operation, approval: Approval, payload: dict[str, Any]
) -> None:
    """INSERT only, in the caller's existing business transaction; never a replay."""
    body = {
        "version": 1,
        "scope": "retained_execution_fields",
        "operation_id": operation.id,
        "approval_id": approval.id,
        "request_hash": operation.request_hash,
        "receipt": dict(operation.result_json or {}),
        "approved_payload": payload,
    }
    digest = canonical_hash(body)
    session.add(ExecutionEvidence(operation_id=operation.id, body=body, digest=digest))
    operation.result_json = {
        **(operation.result_json or {}),
        "execution_evidence": {"version": 1, "digest": digest},
    }


def _bound_payload(
    session: Session, operation: Operation
) -> tuple[dict[str, Any], dict[str, Any], str]:
    _require(operation.status == "COMMITTED", "OPERATION_NOT_COMMITTED")
    receipt = operation.result_json
    _require(isinstance(receipt, dict), "RECEIPT_INVALID")
    assert isinstance(receipt, dict)
    binding = receipt.get("execution_evidence")
    _require(binding is not None, "EXECUTION_EVIDENCE_NOT_RETAINED")
    _require(
        isinstance(binding, dict)
        and set(binding) == {"version", "digest"}
        and type(binding["version"]) is int
        and binding["version"] == 1,
        "EXECUTION_EVIDENCE_VERSION_UNSUPPORTED",
    )
    length = session.scalar(
        select(func.length(cast(ExecutionEvidence.body, String))).where(
            ExecutionEvidence.operation_id == operation.id
        )
    )
    _require(length is not None, "EXECUTION_EVIDENCE_MISSING")
    assert length is not None
    _require(length <= MAX_OUTPUT_BYTES, "EVIDENCE_LIMIT")
    evidence = session.get(ExecutionEvidence, operation.id)
    assert evidence is not None
    assert isinstance(binding, dict)
    body = evidence.body
    _require(
        len(json.dumps(body, ensure_ascii=False).encode()) <= MAX_OUTPUT_BYTES, "EVIDENCE_LIMIT"
    )
    _require(
        isinstance(body, dict)
        and set(body)
        == {
            "version",
            "scope",
            "operation_id",
            "approval_id",
            "request_hash",
            "receipt",
            "approved_payload",
        },
        "EVIDENCE_ENVELOPE_INVALID",
    )
    _require(
        type(body["version"]) is int and body["version"] == 1,
        "EXECUTION_EVIDENCE_VERSION_UNSUPPORTED",
    )
    _require(
        body["scope"] == "retained_execution_fields"
        and body["operation_id"] == operation.id
        and body["request_hash"] == operation.request_hash
        and _identifier(body["approval_id"])
        and evidence.digest == binding["digest"] == canonical_hash(body),
        "EVIDENCE_BINDING_MISMATCH",
    )
    identity = body["receipt"]
    _require(
        isinstance(identity, dict)
        and set(identity) == RECEIPT_FIELDS
        and all(_identifier(identity[key]) for key in RECEIPT_FIELDS - {"revision"})
        and _integer(identity["revision"], 1)
        and all(receipt.get(key) == identity[key] for key in RECEIPT_FIELDS),
        "COMMIT_IDENTITY_MISMATCH",
    )
    payload = body["approved_payload"]
    _payload_valid(payload)
    plan = session.get(Plan, identity["plan_id"])
    approval = session.get(Approval, body["approval_id"])
    consumed = _rows(
        session, select(Approval.id).where(Approval.consumed_operation_id == operation.id)
    )
    _require(consumed == [body["approval_id"]], "CONSUMED_APPROVAL_MISSING_OR_AMBIGUOUS")
    task = session.get(Task, identity["task_id"])
    audit = session.get(AuditRecord, identity["audit_id"])
    _require(
        plan is not None and approval is not None and task is not None and audit is not None,
        "COMMIT_ATTESTATION_MISSING",
    )
    assert plan is not None and approval is not None and task is not None and audit is not None
    _require(
        operation.plan_id == plan.id == approval.plan_id
        and plan.task_id == task.id
        and plan.status == "COMMITTED"
        and approval.status == "CONSUMED"
        and approval.consumed_operation_id == operation.id
        and task.order_id == identity["order_id"]
        and task.chain_id == identity["chain_id"]
        and task.result_ref == operation.id
        and plan.plan_kind == payload["plan_kind"]
        and plan.base_order_revision == payload["base_revision"] == identity["revision"] - 1
        and (payload["plan_kind"] == "create" or payload["order_id"] == identity["order_id"])
        and plan.intent_revision == approval.intent_revision == payload["intent_revision"]
        and plan.plan_hash == approval.plan_hash == canonical_hash(payload)
        and operation.request_hash
        == canonical_hash(
            {"plan_id": plan.id, "approval_id": approval.id, "plan_hash": approval.plan_hash}
        )
        and audit.action == "commit"
        and audit.operation_id == operation.id
        and audit.object_id == identity["order_id"]
        and audit.actor_type == "human"
        and audit.actor_id == approval.actor_id
        and audit.before_hash == audit.after_hash == approval.plan_hash,
        "COMMIT_ATTESTATION_MISMATCH",
    )
    return identity, payload, evidence.digest


def _fields(
    session: Session, operation: Operation, identity: dict, payload: dict
) -> list[ExecutionFieldResult]:
    revisions = _rows(
        session,
        select(OrderRevision).where(
            OrderRevision.order_id == identity["order_id"],
            OrderRevision.revision_no == identity["revision"],
        ),
    )
    lines = (
        _rows(session, select(OrderLine).where(OrderLine.revision_id == revisions[0].id))
        if len(revisions) == 1
        else []
    )
    expected_lines = sorted(
        (row["sku_id"], row["ordered_qty"], row["fulfillment_mode"]) for row in payload["lines"]
    )
    line_reasons = []
    if len(revisions) != 1:
        line_reasons.append("ORDER_REVISION_MISSING")
    elif revisions[0].base_revision != payload["base_revision"]:
        line_reasons.append("REVISION_IDENTITY_MISMATCH")
    if (
        sorted((row.sku_id, row.ordered_qty, row.fulfillment_mode) for row in lines)
        != expected_lines
    ):
        line_reasons.append("REVISION_LINES_MISMATCH")
    links = _compare_links(
        session, operation, identity["order_id"], identity["revision"], payload["downstream"]
    )
    link_count = len(
        _rows(session, select(DownstreamLink).where(DownstreamLink.operation_id == operation.id))
    )
    movements = _rows(
        session, select(StockMovement).where(StockMovement.operation_id == operation.id)
    )
    expected_moves = sorted(
        (
            seq,
            WAREHOUSE_ID,
            row["sku_id"],
            0,
            row["qty"],
            payload["stock_revisions"][row["sku_id"]],
            payload["stock_revisions"][row["sku_id"]] + 1,
            "RESERVE" if row["qty"] > 0 else "RELEASE",
        )
        for seq, row in enumerate(
            sorted(payload["reservation_delta"], key=lambda r: r["sku_id"]), 1
        )
    )
    actual_moves = sorted(
        (
            row.seq,
            row.warehouse_id,
            row.sku_id,
            row.delta_on_hand,
            row.delta_reserved,
            row.before_revision,
            row.after_revision,
            row.movement_type,
        )
        for row in movements
    )
    specs: list[tuple[ExecutionField, int, int, list[str]]] = [
        ("revision_lines", len(expected_lines), len(lines), line_reasons),
        (
            "operation_links",
            len(payload["downstream"]),
            link_count,
            sorted({f.code for f in links.findings}),
        ),
        (
            "reservation_movements",
            len(expected_moves),
            len(movements),
            [] if expected_moves == actual_moves else ["MOVEMENTS_MISMATCH"],
        ),
    ]
    return [
        ExecutionFieldResult(
            field=name,
            status="findings" if reasons else "consistent",
            expected_count=expected,
            observed_count=actual,
            reason_codes=reasons,
        )
        for name, expected, actual, reasons in specs
    ]


def _project(session: Session, operation_id: str) -> ExecutionAudit:
    operation = session.get(Operation, operation_id)
    _require(operation is not None, "OPERATION_MISSING")
    assert operation is not None
    identity, payload, digest = _bound_payload(session, operation)
    fields = _fields(session, operation, identity, payload)
    order_id = identity["order_id"]
    uncovered = [
        UncoveredScope(
            object_type="order", object_id=order_id, reason_code="CURRENT_ORDER_GRAPH_NOT_PROVEN"
        ),
        UncoveredScope(
            object_type="order_revision",
            object_id=f"{order_id}:{payload['base_revision']}",
            reason_code="PARENT_GRAPH_NOT_BOUND_BY_APPROVAL",
        ),
    ]
    dependencies = []
    conflict = False
    for edge in payload["downstream"]:
        owners = sorted(
            set(
                _rows(
                    session,
                    select(DownstreamLink.order_id)
                    .where(
                        DownstreamLink.doc_type == edge["doc_type"],
                        DownstreamLink.doc_id == edge["doc_id"],
                    )
                    .distinct(),
                )
            )
        )
        conflict |= any(owner != order_id for owner in owners)
        dependencies.append(
            ObservedDependency(
                object_type=edge["doc_type"], object_id=edge["doc_id"], observed_order_ids=owners
            )
        )
        uncovered.append(
            UncoveredScope(
                object_type=edge["doc_type"],
                object_id=edge["doc_id"],
                reason_code="CROSS_ORDER_LINK_OBSERVED"
                if any(owner != order_id for owner in owners)
                else "OWNERSHIP_HISTORY_AND_DOCUMENT_FACTS_NOT_PROVEN",
            )
        )
    # Include unchanged STOCK lines too. An absence of a delta does not mean the
    # order is independent of shared stock. Membership observed here is NOT complete.
    stocks = set(payload["stock_revisions"]) | {
        row["sku_id"] for row in payload["lines"] if row["fulfillment_mode"] == "STOCK"
    }
    for sku in sorted(stocks):
        owners = sorted(
            set(
                _rows(
                    session,
                    select(Reservation.order_id)
                    .where(Reservation.warehouse_id == WAREHOUSE_ID, Reservation.sku_id == sku)
                    .distinct(),
                )
            )
        )
        dependencies.append(
            ObservedDependency(
                object_type="warehouse_sku",
                object_id=f"{WAREHOUSE_ID}:{sku}",
                observed_order_ids=owners,
            )
        )
        uncovered.append(
            UncoveredScope(
                object_type="warehouse_sku",
                object_id=f"{WAREHOUSE_ID}:{sku}",
                reason_code="SHARED_STOCK_CLOSURE_NOT_ESTABLISHED",
            )
        )
    inventory = _rows(session, select(SalesOrder.id).order_by(SalesOrder.id))
    return ExecutionAudit(
        operation_id=operation_id,
        status="findings"
        if conflict or any(f.status == "findings" for f in fields)
        else "consistent_for_retained_fields",
        order_id=order_id,
        order_revision=identity["revision"],
        evidence_digest=digest,
        fields=fields,
        uncovered=uncovered,
        observed_dependencies=dependencies,
        observed_unrequested_order_ids=[order for order in inventory if order != order_id],
    )


def audit_execution(database_url: str, operation_id: str) -> ExecutionAudit:
    """Single read snapshot via mode=ro, no migration, repair or result publication."""
    try:
        _require(_identifier(operation_id), "OPERATION_ID_INVALID")
        engine = _readonly_engine(database_url)
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("BEGIN")
                with Session(bind=connection, autoflush=False) as session:
                    report = _project(session, operation_id)
                    _require(
                        len(report.model_dump_json().encode()) <= MAX_OUTPUT_BYTES, "EVIDENCE_LIMIT"
                    )
                    return report
        finally:
            engine.dispose()
    except _Uncertain as exc:
        code = str(exc)
    except (ValueError, TypeError, KeyError, RecursionError):
        code = "EVIDENCE_MALFORMED"
    except (SQLAlchemyError, sqlite3.Error, OSError):
        code = "DATABASE_UNAVAILABLE"
    return ExecutionAudit(operation_id=operation_id, status="uncertain", reason_code=code)
