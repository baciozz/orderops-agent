"""Read-only tools for the three P0 sub-agents.

The handlers recompute from the database. Their numbers are a proposal. The
kernel runs the same checks again before anything is written.
"""

from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from schemas.downstream import DocCheckOutput, DocVerdict
from schemas.enums import DocType, Verdict
from schemas.inventory import InventoryCheckOutput, SkuInventoryResult
from schemas.subagent import SubAgentSpec
from sqlalchemy.orm import Session

from app.db.models import AuditRecord, SalesOrder
from app.domain.changes import evaluate_changes
from app.domain.downstream import assess_downstream, locked_preview
from app.domain.hashing import canonical_hash
from app.errors import ToolNotAllowed
from app.orchestration.registry import check_tool_whitelist
from app.services.kernel import (
    _available_map,
    _balances_for,
    _candidates,
    _known_skus,
    _load_documents,
    _load_lines,
    _mentioned,
    _resolve_choice,
    _usage_map,
)


class ToolGateway:
    def __init__(
        self,
        session: Session,
        *,
        task_id: str,
        order_id: str | None,
        source_id: str,
        source_text: str,
        changes: list[dict[str, object]],
        new_lines: list[dict[str, object]],
        now: datetime,
    ) -> None:
        self.session = session
        self.task_id = task_id
        self.order_id = order_id
        self.source_id = source_id
        self.source_text = source_text
        self.changes = changes
        self.new_lines = new_lines
        self.now = now

    def execute(
        self,
        spec: SubAgentSpec,
        tool_name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        try:
            check_tool_whitelist(spec, tool_name)
            handler = {
                "source.read": self._source_read,
                "order.get": self._order_get,
                "inventory.check": self._inventory,
                "downstream.get": self._downstream,
            }.get(tool_name)
            if handler is None:
                raise ToolNotAllowed(
                    f"工具 {tool_name} 没有只读实现",
                    details={"tool": tool_name},
                )
            self._check_scope(arguments)
            return handler(arguments)
        except ToolNotAllowed:
            self._audit(spec.agent_id, tool_name)
            raise

    def _audit(self, agent_id: str, tool_name: str) -> None:
        self.session.add(
            AuditRecord(
                id=f"AUD-{uuid4().hex[:12]}",
                actor_type="subagent",
                actor_id=agent_id,
                action="tool.denied",
                object_id=self.task_id,
                operation_id=None,
                before_hash=None,
                after_hash=None,
                source_refs=[{"tool": tool_name}],
                at=self.now,
            )
        )

    def _check_scope(self, arguments: dict[str, object]) -> None:
        requested = arguments.get("order_id")
        if isinstance(requested, str) and self.order_id and requested != self.order_id:
            raise ToolNotAllowed(
                "工具范围不能越过当前订单",
                details={"order_id": requested},
            )
        source_id = arguments.get("source_id")
        if isinstance(source_id, str) and source_id != self.source_id:
            raise ToolNotAllowed(
                "工具范围不能越过当前来源",
                details={"source_id": source_id},
            )

    def _source_read(self, _arguments: dict[str, object]) -> dict[str, object]:
        return {"source_id": self.source_id, "text": self.source_text}

    def _order_get(self, _arguments: dict[str, object]) -> dict[str, object]:
        if not self.order_id:
            return {"order_id": None, "current_revision": 0, "intent_revision": 0, "lines": []}
        order = self.session.get(SalesOrder, self.order_id)
        if order is None:
            raise ToolNotAllowed(
                f"订单 {self.order_id} 不在本任务范围内",
                details={"order_id": self.order_id},
            )
        lines = _load_lines(self.session, self.order_id)
        return {
            "order_id": order.id,
            "current_revision": order.current_revision,
            "intent_revision": order.intent_revision,
            "lines": [
                {
                    "sku_id": line.sku_id,
                    "ordered_qty": line.ordered_qty,
                    "fulfillment_mode": line.fulfillment_mode,
                    "shipped_qty": line.shipped_qty,
                    "cut_qty": line.cut_qty,
                }
                for line in lines.values()
            ],
        }

    def _inventory(self, _arguments: dict[str, object]) -> dict[str, object]:
        changes = self._preview_changes()
        if not changes:
            return InventoryCheckOutput().model_dump(mode="json")
        lines = _load_lines(self.session, self.order_id) if self.order_id else {}
        known = _known_skus(self.session)
        mentioned = _mentioned(changes) | set(lines)
        evaluation = evaluate_changes(
            lines=lines,
            changes=_resolve_choice(changes, lines),
            stock_available=_available_map(self.session, mentioned),
            known_skus=known,
            candidates_for={sku: _candidates(self.session, sku) for sku in mentioned},
        )
        if evaluation.kind != "validated":
            return InventoryCheckOutput(issues=list(evaluation.issues)).model_dump(mode="json")
        balances = _balances_for(self.session, set(evaluation.reservation_delta))
        rows: list[SkuInventoryResult] = []
        for sku, delta in sorted(evaluation.reservation_delta.items()):
            balance = balances.get(sku)
            shortage = evaluation.shortages.get(sku, 0)
            rows.append(
                SkuInventoryResult(
                    sku_id=sku,
                    on_hand=None if balance is None else balance.on_hand,
                    reserved=None if balance is None else balance.reserved,
                    available=None if balance is None else balance.on_hand - balance.reserved,
                    stock_revision=None if balance is None else balance.revision,
                    reservation_delta=delta,
                    shortage_qty=shortage,
                    purchase_proposal=shortage > 0,
                )
            )
        return InventoryCheckOutput(lines=rows).model_dump(mode="json")

    def _downstream(self, _arguments: dict[str, object]) -> dict[str, object]:
        if not self.order_id:
            return DocCheckOutput().model_dump(mode="json")
        documents = _load_documents(self.session, self.order_id)
        lines = _load_lines(self.session, self.order_id)
        changes = _resolve_choice(self._preview_changes(), lines)
        known = _known_skus(self.session)
        mentioned = _mentioned(changes) | set(lines)
        evaluation = evaluate_changes(
            lines=lines,
            changes=changes,
            stock_available=_available_map(self.session, mentioned),
            known_skus=known,
            candidates_for={sku: _candidates(self.session, sku) for sku in mentioned},
        )
        if evaluation.kind == "validated":
            verdicts, error = assess_downstream(
                new_lines=evaluation.lines,
                production=documents["production"],
                purchase=documents["purchase"],
                usage=_usage_map(self.session),
            )
            if not error:
                return self._from_assess(documents, verdicts)
        return self._from_locked(documents)

    def _preview_changes(self) -> list[dict[str, object]]:
        if self.changes:
            return self.changes
        preview: list[dict[str, object]] = []
        for line in self.new_lines:
            preview.append(
                {
                    "mode": "increment",
                    "sku_id": line.get("sku_id"),
                    "qty": line.get("qty"),
                }
            )
        return preview

    def _from_assess(
        self,
        documents: dict[str, list[dict[str, Any]]],
        verdicts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        revisions = _doc_revisions(documents)
        rows = [
            DocVerdict(
                doc_type=cast(DocType, str(item["doc_type"])),
                doc_id=str(item["doc_id"]),
                base_doc_revision=revisions[str(item["doc_id"])],
                verdict=cast(Verdict, str(item["verdict"])),
                before=item.get("before") if isinstance(item.get("before"), dict) else None,
                after=item.get("after") if isinstance(item.get("after"), dict) else None,
                reason=str(item.get("reason") or ""),
            )
            for item in verdicts
        ]
        return DocCheckOutput(verdicts=rows).model_dump(mode="json")

    def _from_locked(self, documents: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        """Map the kernel's locked/changeable preview into the verdict enum.

        CHANGEABLE is not a stored verdict. The proposal uses CHANGE_DRAFT for
        that bucket. The kernel recomputes the clarification and does not read
        this payload.
        """
        revisions = _doc_revisions(documents)
        content = _doc_content(documents)
        rows: list[DocVerdict] = []
        for item in locked_preview(documents["production"], documents["purchase"]):
            label = item["verdict"]
            verdict = "EXECUTED_LOCKED" if label == "EXECUTED_LOCKED" else "CHANGE_DRAFT"
            reason = f"{item['doc_id']} {label}"
            rows.append(
                DocVerdict(
                    doc_type=item["doc_type"],
                    doc_id=item["doc_id"],
                    base_doc_revision=revisions[item["doc_id"]],
                    verdict=cast(Verdict, verdict),
                    before=content.get(item["doc_id"]),
                    after=None,
                    reason=reason,
                )
            )
        return DocCheckOutput(verdicts=rows).model_dump(mode="json")


def _doc_revisions(documents: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    revisions: dict[str, int] = {}
    for rows in documents.values():
        for row in rows:
            revisions[str(row["doc_id"])] = int(row["doc_revision"])
    return revisions


def _doc_content(documents: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    content: dict[str, dict[str, Any]] = {}
    for rows in documents.values():
        for row in rows:
            content[str(row["doc_id"])] = {
                key: value for key, value in row.items() if key != "doc_id"
            }
    return content


def input_hash(payload: dict[str, object]) -> str:
    return canonical_hash(payload)
