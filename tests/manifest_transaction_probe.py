"""TEST ONLY: atomic per-operation receipt experiment, NOT an ownership manifest.

No production table/migration/hook uses this module. It deliberately does not claim
base provenance, approval v2, whole-graph completeness, or a deployable verifier.
The tests exercise the real kernel transaction with a small proposed sidecar.
"""

import json
from contextvars import ContextVar
from copy import deepcopy

from app.db.models import Approval, DownstreamLink, Operation, OrderLine, OrderRevision, Plan
from app.db.session import begin_immediate
from app.domain.hashing import canonical_hash
from app.services import kernel
from sqlalchemy import select, text


def install_probe(factory, monkeypatch, *, fail_at=None):
    with factory() as session, begin_immediate(session):
        session.execute(
            text(
                "CREATE TABLE design_receipt (operation_id TEXT PRIMARY KEY "
                "REFERENCES operation(id), body TEXT NOT NULL)"
            )
        )
        session.execute(
            text(
                "CREATE TABLE design_edge (operation_id TEXT NOT NULL "
                "REFERENCES design_receipt(operation_id) ON DELETE CASCADE, "
                "doc_type TEXT NOT NULL, doc_id TEXT NOT NULL, sku TEXT NOT NULL, "
                "PRIMARY KEY(operation_id, doc_type, doc_id))"
            )
        )
    captured = ContextVar("design_commit_payload", default=None)
    original_payload = kernel._payload
    original_commit = kernel._commit_locked

    def payload(**kwargs):
        value = original_payload(**kwargs)
        capture = captured.get()
        if capture is not None:
            capture.append(deepcopy(value))
        return value

    def fault(stage):
        if stage == fail_at:
            raise RuntimeError(f"design fault: {stage}")

    def commit(session, **kwargs):
        capture = []
        token = captured.set(capture)
        try:
            outcome, error = original_commit(session, **kwargs)
        finally:
            captured.reset(token)
        if error is not None or outcome is None:
            return outcome, error
        session.flush()
        if outcome.replayed:
            # Never reconstruct a lost receipt from today's plan or live facts.
            if receipt_problems(session, outcome.operation_id):
                raise RuntimeError("design replay: unavailable or inconsistent receipt")
            return outcome, error
        assert len(capture) == 1
        approved = capture[0]
        approval = session.get(Approval, kwargs["approval_id"])
        plan = session.get(Plan, outcome.plan_id)
        assert canonical_hash(approved) == approval.plan_hash == plan.plan_hash
        operation = session.get(Operation, outcome.operation_id)
        body = {
            "version": 1,
            "scope": "operation_only",
            "origin_completeness": "unknown",
            "operation_id": outcome.operation_id,
            "order_id": outcome.order_id,
            "revision": outcome.revision,
            "plan_id": outcome.plan_id,
            "approval_id": approval.id,
            "request_hash": operation.request_hash,
            "approved_payload": approved,
            "edge_count": len(approved["downstream"]),
        }
        fault("before_header")
        session.execute(
            text("INSERT INTO design_receipt VALUES (:operation_id, :body)"),
            {"operation_id": outcome.operation_id, "body": json.dumps(body)},
        )
        fault("after_header")
        for verdict in approved["downstream"]:
            session.execute(
                text("INSERT INTO design_edge VALUES (:op, :kind, :doc, :sku)"),
                {
                    "op": outcome.operation_id,
                    "kind": verdict["doc_type"],
                    "doc": verdict["doc_id"],
                    "sku": verdict["order_line_sku"],
                },
            )
            fault("during_edges")
        operation.result_json = {
            **operation.result_json,
            "design_receipt_hash": canonical_hash(body),
        }
        session.flush()
        fault("after_receipt_binding")
        return outcome, error

    monkeypatch.setattr(kernel, "_payload", payload)
    monkeypatch.setattr(kernel, "_commit_locked", commit)


def read_receipt(session, operation_id):
    raw = session.scalar(
        text("SELECT body FROM design_receipt WHERE operation_id=:op"), {"op": operation_id}
    )
    return json.loads(raw) if raw else None


def receipt_problems(session, operation_id):
    """Limited proposed receipt checks; NEVER clears pending or certifies ownership."""
    body = read_receipt(session, operation_id)
    if body is None:
        return ["missing_receipt"]
    if body.get("version") != 1:
        return ["unsupported_version"]
    operation = session.get(Operation, operation_id)
    if (operation.result_json or {}).get("design_receipt_hash") != canonical_hash(body):
        return ["receipt_digest"]
    approved = body["approved_payload"]
    approval = session.get(Approval, body["approval_id"])
    plan = session.get(Plan, body["plan_id"])
    if (
        approval is None
        or plan is None
        or approval.status != "CONSUMED"
        or approval.consumed_operation_id != operation_id
        or approval.plan_id != plan.id
        or operation.plan_id != plan.id
        or canonical_hash(approved) != approval.plan_hash
        or approval.plan_hash != plan.plan_hash
        or body["operation_id"] != operation_id
        or body["request_hash"] != operation.request_hash
        or body["order_id"] != operation.result_json["order_id"]
        or body["revision"] != operation.result_json["revision"]
    ):
        return ["approval_binding"]
    expected = sorted(
        (v["doc_type"], v["doc_id"], v["order_line_sku"]) for v in approved["downstream"]
    )
    edges = sorted(
        session.execute(
            text("SELECT doc_type, doc_id, sku FROM design_edge WHERE operation_id=:op"),
            {"op": operation_id},
        )
    )
    if edges != expected or len(edges) != body["edge_count"]:
        return ["receipt_edges"]
    actual = sorted(
        (r.doc_type, r.doc_id, r.order_line_sku, r.order_id, r.order_revision)
        for r in session.scalars(
            select(DownstreamLink).where(DownstreamLink.operation_id == operation_id)
        )
    )
    expected = sorted((*e, body["order_id"], body["revision"]) for e in expected)
    problems = ["business_edges"] if actual != expected else []
    lines = [
        {"sku_id": sku, "ordered_qty": qty, "fulfillment_mode": mode}
        for sku, qty, mode in session.execute(
            select(OrderLine.sku_id, OrderLine.ordered_qty, OrderLine.fulfillment_mode)
            .join(OrderRevision, OrderRevision.id == OrderLine.revision_id)
            .where(
                OrderRevision.order_id == body["order_id"],
                OrderRevision.revision_no == body["revision"],
            )
            .order_by(OrderLine.sku_id)
        )
    ]
    if lines != approved["lines"]:
        problems.append("business_lines")
    return problems
