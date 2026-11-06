"""Read-only audit of links promised by one retained, hash-verified commit.

Reconstruct the approval payload from its committed revision and movements,
never from today's quantities, document status, or inferred order ownership.
Missing evidence is uncertain, not an empty manifest or a successful check.
"""

import sqlite3
from pathlib import Path
from typing import Any

from schemas.link_audit import FindingCode, LinkAudit, LinkAuditFinding
from seeds.golden import WAREHOUSE_ID
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import (
    Approval,
    AuditRecord,
    DownstreamLink,
    Operation,
    OrderLine,
    OrderRevision,
    Plan,
    SalesOrder,
    StockMovement,
    Task,
)
from app.domain.changes import ChangeEval, SnapLine
from app.domain.hashing import canonical_hash
from app.domain.plan_snapshot import build_plan_payload

MAX_ROWS = 1000
MAX_OUTPUT_BYTES = 2 * 1024 * 1024


class _Uncertain(Exception):
    pass


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise _Uncertain(code)


def _rows(session: Session, query: Any) -> list[Any]:
    rows = list(session.scalars(query.limit(MAX_ROWS + 1)))
    _require(len(rows) <= MAX_ROWS, "EVIDENCE_LIMIT")
    return rows


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 64


def _manifest(session: Session, operation: Operation) -> tuple[str, int, list[dict[str, Any]]]:
    _require(operation.status == "COMMITTED", "OPERATION_NOT_COMMITTED")
    receipt = operation.result_json
    _require(isinstance(receipt, dict), "RECEIPT_INVALID")
    assert isinstance(receipt, dict)
    _require(
        all(
            _identifier(receipt.get(key))
            for key in ("order_id", "plan_id", "task_id", "chain_id", "audit_id")
        )
        and type(receipt.get("revision")) is int
        and receipt["revision"] > 0,
        "RECEIPT_INVALID",
    )
    plan = session.get(Plan, operation.plan_id) if operation.plan_id else None
    _require(plan is not None, "PLAN_MISSING")
    assert plan is not None
    _require(
        plan.status == "COMMITTED"
        and plan.id == receipt["plan_id"]
        and plan.task_id == receipt["task_id"]
        and plan.plan_kind in {"create", "change"}
        and receipt["revision"] == plan.base_order_revision + 1
        and (plan.base_order_revision == 0) == (plan.plan_kind == "create"),
        "COMMIT_IDENTITY_MISMATCH",
    )
    task = session.get(Task, plan.task_id)
    order = session.get(SalesOrder, receipt["order_id"])
    _require(
        task is not None
        and task.order_id == receipt["order_id"]
        and task.chain_id == receipt["chain_id"]
        and task.result_ref == operation.id
        and order is not None
        and order.current_revision >= receipt["revision"],
        "COMMIT_IDENTITY_MISMATCH",
    )
    revisions = _rows(
        session,
        select(OrderRevision).where(
            OrderRevision.order_id == receipt["order_id"],
            OrderRevision.revision_no == receipt["revision"],
        ),
    )
    _require(len(revisions) == 1, "ORDER_REVISION_MISSING")
    revision = revisions[0]
    _require(revision.base_revision == plan.base_order_revision, "COMMIT_IDENTITY_MISMATCH")
    approvals = _rows(
        session, select(Approval).where(Approval.consumed_operation_id == operation.id)
    )
    _require(len(approvals) == 1, "CONSUMED_APPROVAL_MISSING_OR_AMBIGUOUS")
    approval = approvals[0]
    audit = session.get(AuditRecord, receipt["audit_id"])
    _require(
        approval.status == "CONSUMED"
        and approval.plan_id == plan.id
        and approval.plan_hash == plan.plan_hash
        and approval.intent_revision == plan.intent_revision
        and approval.actor_id == revision.created_by
        and audit is not None
        and audit.action == "commit"
        and audit.object_id == receipt["order_id"]
        and audit.operation_id == operation.id
        and audit.actor_type == "human"
        and audit.actor_id == approval.actor_id
        and audit.before_hash == plan.plan_hash
        and audit.after_hash == plan.plan_hash
        and operation.request_hash
        == canonical_hash(
            {"plan_id": plan.id, "approval_id": approval.id, "plan_hash": plan.plan_hash}
        ),
        "COMMIT_ATTESTATION_MISMATCH",
    )
    normalized = plan.normalized_changes
    _require(isinstance(normalized, dict), "PLAN_MANIFEST_INVALID")
    verdicts = normalized.get("downstream")
    _require(
        isinstance(verdicts, list) and _identifier(normalized.get("customer_id")),
        "PLAN_MANIFEST_INVALID",
    )
    assert isinstance(verdicts, list)
    _require(len(verdicts) <= MAX_ROWS, "EVIDENCE_LIMIT")
    keys = set()
    for item in verdicts:
        _require(
            isinstance(item, dict)
            and item.get("doc_type") in {"production_order", "purchase_order"}
            and _identifier(item.get("doc_id"))
            and _identifier(item.get("order_line_sku"))
            and (item.get("before") is None or isinstance(item.get("before"), dict))
            and isinstance(item.get("after"), dict)
            and item.get("verdict")
            in {"UNAFFECTED_RELINK", "EXECUTED_LOCKED", "CHANGE_DRAFT", "CANCEL_DRAFT"},
            "PLAN_MANIFEST_INVALID",
        )
        key = (item["doc_type"], item["doc_id"])
        _require(key not in keys, "PLAN_MANIFEST_INVALID")
        keys.add(key)
    stocks = plan.expected_stock_revisions
    _require(
        isinstance(stocks, dict)
        and all(_identifier(k) and type(v) is int and v >= 0 for k, v in stocks.items())
        and isinstance(plan.expected_doc_revisions, dict),
        "PLAN_MANIFEST_INVALID",
    )
    movements = _rows(
        session,
        select(StockMovement)
        .where(StockMovement.operation_id == operation.id)
        .order_by(StockMovement.sku_id),
    )
    deltas = {}
    for seq, movement in enumerate(movements, 1):
        _require(
            movement.sku_id not in deltas
            and movement.seq == seq
            and movement.warehouse_id == WAREHOUSE_ID
            and movement.delta_on_hand == 0
            and movement.delta_reserved != 0
            and movement.before_revision == stocks.get(movement.sku_id)
            and movement.after_revision == movement.before_revision + 1
            and movement.movement_type == ("RESERVE" if movement.delta_reserved > 0 else "RELEASE"),
            "COMMIT_MOVEMENTS_INVALID",
        )
        deltas[movement.sku_id] = movement.delta_reserved
    _require(set(deltas) == set(stocks), "COMMIT_MOVEMENTS_INVALID")
    lines = _rows(session, select(OrderLine).where(OrderLine.revision_id == revision.id))
    payload = build_plan_payload(
        plan_kind=plan.plan_kind,
        order_id=receipt["order_id"] if plan.plan_kind == "change" else None,
        customer_id=normalized["customer_id"],
        base_revision=plan.base_order_revision,
        intent_revision=plan.intent_revision,
        evaluation=ChangeEval(
            kind="validated",
            lines={
                row.sku_id: SnapLine(row.sku_id, row.ordered_qty, row.fulfillment_mode)
                for row in lines
            },
            reservation_delta=deltas,
        ),
        stock_revisions=stocks,
        verdicts=verdicts,
        expected_docs=plan.expected_doc_revisions,
        sheet_order_v1=normalized.get("sheet_order_v1"),
    )
    _require(canonical_hash(payload) == plan.plan_hash, "PLAN_HASH_UNVERIFIABLE")
    return receipt["order_id"], receipt["revision"], verdicts


def _compare_links(
    session: Session,
    operation: Operation,
    order_id: str,
    revision: int,
    verdicts: list[dict[str, Any]],
) -> LinkAudit:
    links = _rows(
        session,
        select(DownstreamLink)
        .where(DownstreamLink.operation_id == operation.id)
        .order_by(DownstreamLink.id),
    )
    grouped: dict[tuple[str, str], list[DownstreamLink]] = {}
    for link in links:
        grouped.setdefault((link.doc_type, link.doc_id), []).append(link)
    findings = []
    matched = 0
    for item in sorted(verdicts, key=lambda row: (row["doc_type"], row["doc_id"])):
        rows = grouped.pop((item["doc_type"], item["doc_id"]), [])
        code: FindingCode | None = None
        if not rows:
            code = "EXPECTED_LINK_MISSING"
        elif len(rows) != 1:
            code = "DUPLICATE_LINK"
        elif (rows[0].order_id, rows[0].order_revision, rows[0].order_line_sku) != (
            order_id,
            revision,
            item["order_line_sku"],
        ):
            code = "LINK_IDENTITY_MISMATCH"
        else:
            matched += 1
        if code:
            findings.append(
                LinkAuditFinding(
                    code=code,
                    doc_type=item["doc_type"],
                    doc_id=item["doc_id"],
                    link_ids=[row.id for row in rows],
                )
            )
    for (doc_type, doc_id), rows in sorted(grouped.items()):
        findings.append(
            LinkAuditFinding(
                code="UNEXPECTED_LINK",
                doc_type=doc_type,
                doc_id=doc_id,
                link_ids=[row.id for row in rows],
            )
        )
    return LinkAudit(
        operation_id=operation.id,
        status="findings" if findings else "consistent_for_operation",
        order_id=order_id,
        order_revision=revision,
        expected_link_count=len(verdicts),
        matched_link_count=matched,
        findings=findings,
    )


def _project(session: Session, operation_id: str) -> LinkAudit:
    operation = session.get(Operation, operation_id)
    _require(operation is not None, "OPERATION_MISSING")
    assert operation is not None
    order_id, revision, verdicts = _manifest(session, operation)
    return _compare_links(session, operation, order_id, revision, verdicts)


def _readonly_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    _require(
        url.drivername in {"sqlite", "sqlite+pysqlite"}
        and "?" not in database_url
        and not url.query
        and not url.host
        and url.database not in (None, "", ":memory:"),
        "FILE_SQLITE_REQUIRED",
    )
    path = Path(str(url.database)).resolve()
    _require(path.is_file() and path.stat().st_size > 0, "DATABASE_UNAVAILABLE")

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        connection.execute("PRAGMA query_only=ON")
        return connection

    return create_engine("sqlite://", creator=connect)


def audit_operation_links(database_url: str, operation_id: str) -> LinkAudit:
    """Dedicated mode=ro connection, one explicit deferred snapshot, no initialization.

    Even a consistent report is historical and scoped to this operation. It must
    never clear pending, authorize a commit, or certify today's complete ownership.
    """
    try:
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
    return LinkAudit(operation_id=operation_id, status="uncertain", reason_code=code)
