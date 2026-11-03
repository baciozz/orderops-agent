"""Persistent live-model budget ledger.

Before a live call, atomically reserve estimated input plus maximum output cost
and a per-task call slot. Settle the same row after the call. A crash leaves the
pessimistic reservation charged. The cap is min(configured, 5.00 USD). Prices
are never guessed. Fixture calls do not write this ledger.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import HARD_BUDGET_CEILING_USD
from app.db.models import ModelUsage
from app.db.session import begin_immediate
from app.errors import BudgetExceeded, CallLimitExceeded, PricesNotConfigured
from app.observability import enrich, get_logger, observed

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

logger = get_logger(__name__)


_MILLION = Decimal(1_000_000)
_QUANTUM = Decimal("0.000001")


def reconcile_reservations(
    session_factory: sessionmaker[Session], *, now: datetime, ttl: timedelta
) -> int:
    """Mark expired reservations without releasing money or call slots.

    Only age strictly greater than ttl qualifies. Late settlement is allowed;
    this transition never claims that the provider did not receive the call.
    """
    if ttl <= timedelta(0):
        raise ValueError("预留 TTL 必须大于零")
    with session_factory() as session, begin_immediate(session):
        result = cast(
            "CursorResult[Any]",
            session.execute(
                update(ModelUsage)
                .where(ModelUsage.status == "RESERVED", ModelUsage.reserved_at < now - ttl)
                .values(status="ORPHANED")
            ),
        )
        return result.rowcount


def effective_cap(configured_usd: Decimal) -> Decimal:
    if configured_usd < 0:
        raise ValueError("预算不能为负")
    return min(configured_usd, HARD_BUDGET_CEILING_USD)


def quote_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    price_in_per_mtok: Decimal | None,
    price_out_per_mtok: Decimal | None,
) -> Decimal:
    if price_in_per_mtok is None or price_out_per_mtok is None:
        raise PricesNotConfigured()
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token 数不能为负")
    raw = (
        Decimal(input_tokens) * price_in_per_mtok + Decimal(output_tokens) * price_out_per_mtok
    ) / _MILLION
    return raw.quantize(_QUANTUM)


class BudgetLedger:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        cap_usd: Decimal,
        price_in_per_mtok: Decimal | None,
        price_out_per_mtok: Decimal | None,
    ) -> None:
        self._sessions = session_factory
        self.cap_usd = effective_cap(cap_usd)
        self.price_in_per_mtok = price_in_per_mtok
        self.price_out_per_mtok = price_out_per_mtok

    def spent(self) -> Decimal:
        session = self._sessions()
        try:
            amounts = session.scalars(select(ModelUsage.cost_usd)).all()
        finally:
            session.close()
        total = Decimal("0")
        for amount in amounts:
            total += amount if isinstance(amount, Decimal) else Decimal(str(amount))
        return total.quantize(_QUANTUM)

    def calls_for_task(self, task_id: str) -> int:
        session = self._sessions()
        try:
            rows = session.scalars(select(ModelUsage.id).where(ModelUsage.task_id == task_id)).all()
        finally:
            session.close()
        return len(rows)

    @observed(
        logger,
        "budget.reserve",
        failed_event="budget.refused",
        fields=lambda self, task_id, input_tokens, max_output_tokens, **_: {
            "task_id": task_id,
            "cap_usd": self.cap_usd,
            "input_tokens": input_tokens,
            "output_tokens": max_output_tokens,
        },
    )
    def reserve(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        max_output_tokens: int,
        task_id: str | None = None,
        agent_id: str | None = None,
        max_calls_per_task: int | None = None,
        at: datetime | None = None,
    ) -> str:
        """Check both limits and persist the reservation under one writer lock."""
        estimate = quote_cost(
            input_tokens=input_tokens,
            output_tokens=max_output_tokens,
            price_in_per_mtok=self.price_in_per_mtok,
            price_out_per_mtok=self.price_out_per_mtok,
        )
        enrich(reserved_usd=estimate, actual_usd=None)
        price_version = f"in={self.price_in_per_mtok};out={self.price_out_per_mtok}"
        reserved_at = at or datetime.now(UTC)
        row = ModelUsage(
            id=f"MU-{uuid4().hex[:16]}",
            task_id=task_id,
            agent_id=agent_id,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=max_output_tokens,
            cost_usd=estimate,
            price_version=price_version,
            price_in_per_mtok=self.price_in_per_mtok,
            price_out_per_mtok=self.price_out_per_mtok,
            at=reserved_at,
            status="RESERVED",
            reserved_at=reserved_at,
        )
        session = self._sessions()
        try:
            with begin_immediate(session):
                if task_id is not None and max_calls_per_task is not None:
                    used = session.scalar(
                        select(func.count())
                        .select_from(ModelUsage)
                        .where(ModelUsage.task_id == task_id)
                    )
                    assert used is not None  # COUNT always returns a row.
                    if used >= max_calls_per_task:
                        raise CallLimitExceeded(
                            f"任务 {task_id} 已达到模型调用次数上限 {max_calls_per_task}",
                            details={"task_id": task_id, "calls": used},
                        )
                # Sum Decimals in Python: SQLite's SUM would use floating point.
                spent = sum(session.scalars(select(ModelUsage.cost_usd)), Decimal("0"))
                if spent + estimate > self.cap_usd:
                    raise BudgetExceeded(
                        details={
                            "spent_usd": format(spent, "f"),
                            "estimate_usd": format(estimate, "f"),
                            "cap_usd": format(self.cap_usd, "f"),
                        }
                    )
                session.add(row)
        finally:
            session.close()
        enrich(reservation_id=row.id)
        return row.id

    @observed(
        logger,
        "budget.settle",
        fields=lambda self, reservation_id, input_tokens, output_tokens, **_: {
            "cap_usd": self.cap_usd,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reservation_id": reservation_id,
        },
    )
    def settle(
        self,
        reservation_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        at: datetime | None = None,
    ) -> ModelUsage:
        """Replace the estimate with actual usage; retain the row as a call.

        Provider-reported usage is recorded even if it exceeds the reservation.
        Unknown usage should be settled at the reserved token estimates.
        ORPHANED rows accept late actual usage. Identical repeated settlements
        are no-ops; conflicting repeats are rejected so the first result wins.
        """
        session = self._sessions()
        try:
            with begin_immediate(session):
                row = session.get(ModelUsage, reservation_id)
                if row is None:
                    raise ValueError("模型用量预留不存在")
                if row.status == "SETTLED":
                    if (row.input_tokens, row.output_tokens) != (input_tokens, output_tokens):
                        raise ValueError("模型用量已经结算，不能覆盖不同用量")
                    return row
                cost = quote_cost(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    price_in_per_mtok=(
                        row.price_in_per_mtok
                        if row.price_in_per_mtok is not None
                        else self.price_in_per_mtok
                    ),
                    price_out_per_mtok=(
                        row.price_out_per_mtok
                        if row.price_out_per_mtok is not None
                        else self.price_out_per_mtok
                    ),
                )
                enrich(task_id=row.task_id, reserved_usd=row.cost_usd, actual_usd=cost)
                row.input_tokens = input_tokens
                row.output_tokens = output_tokens
                row.cost_usd = cost
                row.status = "SETTLED"
                row.settled_at = at or datetime.now(UTC)
        finally:
            session.close()
        return row
