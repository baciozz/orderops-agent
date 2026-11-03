"""Persistent live-model budget ledger.

Before a live call, estimate input tokens × input price plus max output tokens
× output price. Refuse when spent + estimate is greater than the cap. The cap
is min(configured, 5.00 USD). Prices are never guessed. Fixture calls do not
write this ledger.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import HARD_BUDGET_CEILING_USD
from app.db.models import ModelUsage
from app.db.session import begin_immediate
from app.errors import BudgetExceeded, CallLimitExceeded, PricesNotConfigured

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

    def authorize(
        self,
        *,
        input_tokens: int,
        max_output_tokens: int,
        task_id: str | None = None,
        max_calls_per_task: int | None = None,
    ) -> Decimal:
        """Return the estimate, or refuse before any provider call."""
        estimate = quote_cost(
            input_tokens=input_tokens,
            output_tokens=max_output_tokens,
            price_in_per_mtok=self.price_in_per_mtok,
            price_out_per_mtok=self.price_out_per_mtok,
        )
        if task_id is not None and max_calls_per_task is not None:
            used = self.calls_for_task(task_id)
            if used >= max_calls_per_task:
                raise CallLimitExceeded(
                    f"任务 {task_id} 已达到模型调用次数上限 {max_calls_per_task}",
                    details={"task_id": task_id, "calls": used},
                )
        spent = self.spent()
        if spent + estimate > self.cap_usd:
            raise BudgetExceeded(
                details={
                    "spent_usd": format(spent, "f"),
                    "estimate_usd": format(estimate, "f"),
                    "cap_usd": format(self.cap_usd, "f"),
                }
            )
        return estimate

    def record(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        task_id: str | None = None,
        agent_id: str | None = None,
        at: datetime | None = None,
    ) -> ModelUsage:
        cost = quote_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            price_in_per_mtok=self.price_in_per_mtok,
            price_out_per_mtok=self.price_out_per_mtok,
        )
        price_version = f"in={self.price_in_per_mtok};out={self.price_out_per_mtok}"
        row = ModelUsage(
            id=f"MU-{uuid4().hex[:16]}",
            task_id=task_id,
            agent_id=agent_id,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            price_version=price_version,
            at=at or datetime.now(UTC),
        )
        session = self._sessions()
        try:
            with begin_immediate(session):
                session.add(row)
        finally:
            session.close()
        return row
