"""Enumerations frozen from the v1.0 plan and the v1.1 revision.

Values the plans list explicitly are checked in the database. Status
vocabularies the plans describe only in prose (operation status, delivery
state) stay unconstrained strings until the stage that writes them.
"""

from enum import StrEnum


class FulfillmentMode(StrEnum):
    STOCK = "STOCK"
    MTO = "MTO"


class OrderStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_SHIPPED = "PARTIALLY_SHIPPED"
    FULFILLED = "FULFILLED"
    CANCELLED_UNSHIPPED = "CANCELLED_UNSHIPPED"


class TaskStatus(StrEnum):
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMMITTING = "COMMITTING"
    SUCCEEDED = "SUCCEEDED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"
    RECONCILING = "RECONCILING"


class PlanStatus(StrEnum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    COMMITTED = "COMMITTED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class PlanKind(StrEnum):
    CREATE = "create"
    CHANGE = "change"
    CANCEL = "cancel"


class ApprovalStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class ProposalStatus(StrEnum):
    DRAFT = "DRAFT"
    REVIEWED = "REVIEWED"
    DISMISSED = "DISMISSED"


class ProductionStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    CUT = "CUT"
    PARTIALLY_CUT = "PARTIALLY_CUT"
    CANCELLED = "CANCELLED"


class PurchaseStatus(StrEnum):
    PLACED = "PLACED"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


class ChangeState(StrEnum):
    NONE = "NONE"
    PENDING_SUPPLIER = "PENDING_SUPPLIER"


class DocType(StrEnum):
    PRODUCTION_ORDER = "production_order"
    PURCHASE_ORDER = "purchase_order"


class Verdict(StrEnum):
    UNAFFECTED_RELINK = "UNAFFECTED_RELINK"
    CHANGE_DRAFT = "CHANGE_DRAFT"
    EXECUTED_LOCKED = "EXECUTED_LOCKED"
    CANCEL_DRAFT = "CANCEL_DRAFT"


class MovementType(StrEnum):
    RECEIPT = "RECEIPT"
    RESERVE = "RESERVE"
    SHIP = "SHIP"
    RELEASE = "RELEASE"
    ADJUST = "ADJUST"


class LineMode(StrEnum):
    REPLACE_REMAINING = "replace_remaining"
    SET_TOTAL = "set_total"
    INCREMENT = "increment"
    CANCEL_REMAINING = "cancel_remaining"


class BusinessEventType(StrEnum):
    NEW_ORDER = "new_order"
    CHANGE_ORDER = "change_order"
    CANCEL_REMAINING = "cancel_remaining"
    CLARIFY_ANSWER = "clarify_answer"
    QUERY = "query"


class InputMode(StrEnum):
    TEXT = "text"
    CHAT = "chat"
    MD = "md"
    XLSX = "xlsx"


class AgentKind(StrEnum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"
    EXTERNAL = "external"


class DispatchStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"


class WaitingOn(StrEnum):
    HUMAN = "human"
    SUBAGENT = "subagent"
    NONE = "none"


class RuntimeMode(StrEnum):
    FIXTURE = "FIXTURE"
    LIVE = "LIVE"


class DisabledReason(StrEnum):
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class LockedKind(StrEnum):
    SHIPPED = "shipped"
    CUT = "cut"
    NONE = "none"


class VerificationState(StrEnum):
    PASSED = "passed"
    PENDING = "pending"
