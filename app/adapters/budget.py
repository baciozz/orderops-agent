"""Persistent live-model budget ledger.

Before a live call, atomically reserve estimated input plus maximum output cost
and a per-task call slot. Settle the same row after the call. A crash leaves the
pessimistic reservation charged. The cap is min(configured, 5.00 USD). Prices
are never guessed. Fixture calls do not write this ledger.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import HARD_BUDGET_CEILING_USD
from app.db.models import ModelUsage
from app.db.session import begin_immediate
from app.errors import BudgetExceeded, CallLimitExceeded, PricesNotConfigured
from app.observability import get_logger, observe

logger = get_logger(__name__)


_MILLION = Decimal(1_000_000)
_QUANTUM = Decimal("0.000001")


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
        with observe(
            logger,
            "budget.reserve",
            failed_event="budget.refused",
            mode="LIVE",
            task_id=task_id,
            cap_usd=self.cap_usd,
            input_tokens=input_tokens,
            output_tokens=max_output_tokens,
        ) as fields:
            estimate = quote_cost(
                input_tokens=input_tokens,
                output_tokens=max_output_tokens,
                price_in_per_mtok=self.price_in_per_mtok,
                price_out_per_mtok=self.price_out_per_mtok,
            )
            fields.update(reserved_usd=estimate, actual_usd=None)
            price_version = f"in={self.price_in_per_mtok};out={self.price_out_per_mtok}"
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
                at=at or datetime.now(UTC),
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
            fields.update(reservation_id=row.id)
            return row.id

    def settle(self, reservation_id: str, *, input_tokens: int, output_tokens: int) -> ModelUsage:
        """Replace the estimate with actual usage; retain the row as a call.

        Provider-reported usage is recorded even if it exceeds the reservation.
        Unknown usage should be settled at the reserved token estimates.
        """
        with observe(
            logger,
            "budget.settle",
            mode="LIVE",
            cap_usd=self.cap_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reservation_id=reservation_id,
        ) as fields:
            cost = quote_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                price_in_per_mtok=self.price_in_per_mtok,
                price_out_per_mtok=self.price_out_per_mtok,
            )
            session = self._sessions()
            try:
                with begin_immediate(session):
                    row = session.get(ModelUsage, reservation_id)
                    if row is None:
                        raise ValueError("模型用量预留不存在")
                    fields.update(task_id=row.task_id, reserved_usd=row.cost_usd, actual_usd=cost)
                    row.input_tokens = input_tokens
                    row.output_tokens = output_tokens
                    row.cost_usd = cost
            finally:
                session.close()
            return row
