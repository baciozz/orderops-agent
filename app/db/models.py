"""Relational schema.

Status values the plans enumerate are CHECK constraints. Mappers do not
declare relationships, so a flush does not order inserts by foreign key;
writers flush parents before children. See docs/ARCHITECTURE.md for columns
the plans left unnamed.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from schemas.enums import (
    ApprovalStatus,
    ChangeState,
    DispatchStatus,
    DocType,
    FulfillmentMode,
    MovementType,
    OrderStatus,
    PlanKind,
    PlanStatus,
    ProductionStatus,
    ProposalStatus,
    PurchaseStatus,
    RuntimeMode,
    TaskStatus,
    Verdict,
)
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import Money


def _in_enum(column: str, enum_cls: type[Enum], name: str) -> CheckConstraint:
    values = ", ".join(f"'{item.value}'" for item in enum_cls)
    return CheckConstraint(f"{column} IN ({values})", name=name)


class Customer(Base):
    __tablename__ = "customer"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Warehouse(Base):
    __tablename__ = "warehouse"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class Sku(Base):
    __tablename__ = "sku"
    __table_args__ = (
        UniqueConstraint("style", "color", "size", name="uq_sku_style_color_size"),
        _in_enum("default_fulfillment_mode", FulfillmentMode, "ck_sku_ff_mode"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    style: Mapped[str] = mapped_column(String(64), nullable=False)
    color: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_fulfillment_mode: Mapped[str] = mapped_column(String(16), nullable=False)


class Supplier(Base):
    __tablename__ = "supplier"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class Material(Base):
    __tablename__ = "material"
    __table_args__ = (CheckConstraint("unit = 'm'", name="ck_material_unit"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    color: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)


class MaterialUsage(Base):
    __tablename__ = "material_usage"
    __table_args__ = (CheckConstraint("qty_per_piece > 0", name="ck_usage_qty"),)

    sku_id: Mapped[str] = mapped_column(ForeignKey("sku.id"), primary_key=True)
    material_id: Mapped[str] = mapped_column(ForeignKey("material.id"), primary_key=True)
    qty_per_piece: Mapped[int] = mapped_column(Integer, nullable=False)


class SalesOrder(Base):
    __tablename__ = "sales_order"
    __table_args__ = (
        CheckConstraint("current_revision >= 0", name="ck_so_current_rev"),
        CheckConstraint("intent_revision >= 0", name="ck_so_intent_rev"),
        _in_enum("status", OrderStatus, "ck_so_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customer.id"), nullable=False, index=True)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class OrderRevision(Base):
    __tablename__ = "order_revision"
    __table_args__ = (
        UniqueConstraint("order_id", "revision_no", name="uq_order_revision_no"),
        CheckConstraint("revision_no >= 1", name="ck_rev_no"),
        CheckConstraint("base_revision >= 0", name="ck_base_rev"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("sales_order.id"), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    base_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrderLine(Base):
    __tablename__ = "order_line"
    __table_args__ = (
        CheckConstraint("ordered_qty >= 0", name="ck_line_qty"),
        _in_enum("fulfillment_mode", FulfillmentMode, "ck_line_ff"),
    )

    revision_id: Mapped[str] = mapped_column(ForeignKey("order_revision.id"), primary_key=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("sku.id"), primary_key=True)
    ordered_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    fulfillment_mode: Mapped[str] = mapped_column(String(16), nullable=False)


class ShipmentRecord(Base):
    __tablename__ = "shipment_record"
    __table_args__ = (CheckConstraint("qty > 0", name="ck_ship_qty"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("sales_order.id"), nullable=False, index=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("sku.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    shipped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class StockBalance(Base):
    __tablename__ = "stock_balance"
    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="ck_on_hand"),
        CheckConstraint("reserved >= 0", name="ck_reserved"),
        CheckConstraint("reserved <= on_hand", name="ck_reserved_le_on_hand"),
        CheckConstraint("revision >= 0", name="ck_stock_rev"),
    )

    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouse.id"), primary_key=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("sku.id"), primary_key=True)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)


class Reservation(Base):
    __tablename__ = "reservation"
    __table_args__ = (CheckConstraint("qty >= 0", name="ck_res_qty"),)

    order_id: Mapped[str] = mapped_column(ForeignKey("sales_order.id"), primary_key=True)
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouse.id"), primary_key=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("sku.id"), primary_key=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)


class StockMovement(Base):
    __tablename__ = "stock_movement"
    __table_args__ = (
        UniqueConstraint("operation_id", "seq", name="uq_movement_op_seq"),
        _in_enum("type", MovementType, "ck_mov_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    movement_type: Mapped[str] = mapped_column("type", String(32), nullable=False)
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouse.id"), nullable=False)
    sku_id: Mapped[str] = mapped_column(ForeignKey("sku.id"), nullable=False, index=True)
    delta_on_hand: Mapped[int] = mapped_column(Integer, nullable=False)
    delta_reserved: Mapped[int] = mapped_column(Integer, nullable=False)
    before_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    after_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProductionOrder(Base):
    __tablename__ = "production_order"
    __table_args__ = (
        CheckConstraint("planned_qty >= 0", name="ck_mo_planned"),
        CheckConstraint("cut_qty >= 0", name="ck_mo_cut"),
        CheckConstraint("cut_qty <= planned_qty", name="ck_mo_cut_le_planned"),
        CheckConstraint("doc_revision >= 1", name="ck_mo_docrev"),
        _in_enum("status", ProductionStatus, "ck_mo_status"),
        CheckConstraint(
            "(status = 'NOT_STARTED' AND cut_qty = 0) "
            "OR (status = 'CUT' AND cut_qty = planned_qty AND planned_qty > 0) "
            "OR (status = 'PARTIALLY_CUT' AND cut_qty > 0 AND cut_qty < planned_qty) "
            "OR (status = 'CANCELLED')",
            name="ck_mo_status_qty",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("sku.id"), nullable=False)
    planned_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    cut_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_revision: Mapped[int] = mapped_column(Integer, nullable=False)


class PurchaseOrder(Base):
    __tablename__ = "purchase_order"
    __table_args__ = (
        CheckConstraint("ordered_qty >= 0", name="ck_po_ordered"),
        CheckConstraint("received_qty >= 0", name="ck_po_received"),
        CheckConstraint("received_qty <= ordered_qty", name="ck_po_recv_le"),
        CheckConstraint("doc_revision >= 1", name="ck_po_docrev"),
        _in_enum("status", PurchaseStatus, "ck_po_status"),
        _in_enum("change_state", ChangeState, "ck_po_change"),
        CheckConstraint(
            "(status = 'PLACED' AND received_qty = 0) "
            "OR (status = 'RECEIVED' AND received_qty = ordered_qty AND ordered_qty > 0) "
            "OR (status = 'PARTIALLY_RECEIVED' AND received_qty > 0 "
            "AND received_qty < ordered_qty) "
            "OR (status = 'CANCELLED')",
            name="ck_po_status_qty",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("supplier.id"), nullable=False)
    material_id: Mapped[str] = mapped_column(ForeignKey("material.id"), nullable=False)
    production_order_id: Mapped[str] = mapped_column(
        ForeignKey("production_order.id"), nullable=False
    )
    ordered_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    received_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    change_state: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_revision: Mapped[int] = mapped_column(Integer, nullable=False)


class DocRevision(Base):
    __tablename__ = "doc_revision"
    __table_args__ = (
        UniqueConstraint("doc_type", "doc_id", "rev_no", name="uq_doc_rev"),
        CheckConstraint("rev_no >= 1", name="ck_doc_rev_no"),
        _in_enum("doc_type", DocType, "ck_doc_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rev_no: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DownstreamLink(Base):
    __tablename__ = "downstream_link"
    __table_args__ = (
        UniqueConstraint("doc_type", "doc_id", "link_seq", name="uq_link_seq"),
        CheckConstraint("link_seq >= 1", name="ck_link_seq"),
        CheckConstraint("order_revision >= 1", name="ck_link_order_rev"),
        _in_enum("doc_type", DocType, "ck_link_doc_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_id: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str] = mapped_column(ForeignKey("sales_order.id"), nullable=False)
    order_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    order_line_sku: Mapped[str] = mapped_column(ForeignKey("sku.id"), nullable=False)
    link_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class InputSource(Base):
    __tablename__ = "input_source"
    __table_args__ = (CheckConstraint("size >= 0", name="ck_source_size"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)


class SourceSpan(Base):
    __tablename__ = "source_span"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("input_source.id"), nullable=False)
    sheet: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cell_range: Mapped[str | None] = mapped_column(String(64), nullable=True)
    line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_excerpt: Mapped[str | None] = mapped_column(String(500), nullable=True)


class Event(Base):
    __tablename__ = "event"
    __table_args__ = (
        UniqueConstraint("thread_id", "source_event_key", name="uq_event_source_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chain_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("sales_order.id"), nullable=True)
    intent_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Task(Base):
    __tablename__ = "task"
    __table_args__ = (
        _in_enum("status", TaskStatus, "ck_task_status"),
        _in_enum("runtime_mode", RuntimeMode, "ck_task_runtime_mode"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chain_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("sales_order.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    intent_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    runtime_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="FIXTURE", server_default="FIXTURE"
    )
    last_event_id: Mapped[str | None] = mapped_column(ForeignKey("event.id"), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TaskSource(Base):
    __tablename__ = "task_source"

    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("input_source.id"), primary_key=True)


class SyncDispatchReceipt(Base):
    """Synchronous retry identity. Never persist the raw dispatch text."""

    __tablename__ = "sync_dispatch_receipt"

    client_event_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), unique=True, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(80), nullable=False)


class SourceUploadReceipt(Base):
    """Optional upload identity, committed with the task-owned evidence."""

    __tablename__ = "source_upload_receipt"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "source_id"],
            ["task_source.task_id", "task_source.source_id"],
            name="fk_upload_receipt_owner",
        ),
    )

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_upload_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class JobAttempt(Base):
    __tablename__ = "job_attempt"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_no", name="uq_attempt_no"),
        CheckConstraint("fencing_token >= 0", name="ck_fence"),
        CheckConstraint("step_count >= 0", name="ck_steps"),
        CheckConstraint("attempt_no >= 1", name="ck_attempt_no"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_progress_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Plan(Base):
    __tablename__ = "plan"
    __table_args__ = (
        _in_enum("plan_kind", PlanKind, "ck_plan_kind"),
        _in_enum("status", PlanStatus, "ck_plan_status"),
        CheckConstraint("base_order_revision >= 0", name="ck_plan_base"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), nullable=False)
    plan_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    base_order_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    chain_intent_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_stock_revisions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expected_doc_revisions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    normalized_changes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class DownstreamChangeDraft(Base):
    __tablename__ = "downstream_change_draft"
    __table_args__ = (
        _in_enum("verdict", Verdict, "ck_verdict"),
        _in_enum("doc_type", DocType, "ck_draft_doc_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plan.id"), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_id: Mapped[str] = mapped_column(String(64), nullable=False)
    base_doc_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)


class Approval(Base):
    __tablename__ = "approval"
    __table_args__ = (_in_enum("status", ApprovalStatus, "ck_appr_status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plan.id"), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    dependencies_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    consumed_operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Operation(Base):
    __tablename__ = "operation"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("plan.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    committed_verification_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    verification_retry_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    verification_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_lease_token: Mapped[str | None] = mapped_column(String(64))
    verification_manual_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    verification_last_error_code: Mapped[str | None] = mapped_column(String(64))
    execution_evidence_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_evidence_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)


class ExecutionEvidence(Base):
    """Retained approved fields for one commit, never a complete ownership graph."""

    __tablename__ = "execution_evidence"

    operation_id: Mapped[str] = mapped_column(ForeignKey("operation.id"), primary_key=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    digest: Mapped[str] = mapped_column(String(80), nullable=False)


class PurchaseProposal(Base):
    __tablename__ = "purchase_proposal"
    __table_args__ = (
        CheckConstraint("shortage_qty > 0", name="ck_shortage"),
        _in_enum("status", ProposalStatus, "ck_prop_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), nullable=False)
    sku_id: Mapped[str] = mapped_column(ForeignKey("sku.id"), nullable=False)
    shortage_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class AuditRecord(Base):
    __tablename__ = "audit_record"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    after_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_refs: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InboxDelivery(Base):
    __tablename__ = "inbox_delivery"
    __table_args__ = (
        UniqueConstraint("event_id", "consumer", name="uq_inbox_consumer"),
        CheckConstraint("attempts >= 0", name="ck_inbox_attempts"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("event.id"), nullable=False)
    consumer: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxDelivery(Base):
    __tablename__ = "outbox_delivery"
    __table_args__ = (
        UniqueConstraint("event_id", "consumer", name="uq_outbox_consumer"),
        CheckConstraint("attempts >= 0", name="ck_outbox_attempts"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("event.id"), nullable=False)
    consumer: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Subscription(Base):
    __tablename__ = "subscription"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), nullable=False)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("sales_order.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class DispatchStep(Base):
    __tablename__ = "dispatch_step"
    __table_args__ = (
        UniqueConstraint("task_id", "step_no", name="uq_dispatch_step"),
        CheckConstraint("step_no >= 1", name="ck_step_no"),
        _in_enum("status", DispatchStatus, "ck_dispatch_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    input_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    output_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ModelUsage(Base):
    __tablename__ = "model_usage"
    __table_args__ = (
        CheckConstraint("input_tokens >= 0", name="ck_in_tok"),
        CheckConstraint("output_tokens >= 0", name="ck_out_tok"),
        CheckConstraint(
            "status IN ('RESERVED', 'SETTLED', 'ORPHANED')", name="ck_model_usage_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task.id"), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    price_version: Mapped[str] = mapped_column(String(128), nullable=False)
    price_in_per_mtok: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    price_out_per_mtok: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SETTLED", server_default="SETTLED"
    )
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InputPayload(Base):
    __tablename__ = "input_payload"

    event_id: Mapped[str] = mapped_column(ForeignKey("event.id"), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ConsumerReceipt(Base):
    __tablename__ = "consumer_receipt"

    event_id: Mapped[str] = mapped_column(ForeignKey("event.id"), primary_key=True)
    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
