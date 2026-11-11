"""Persistent live-model budget ledger.

Before a live call, atomically reserve estimated input plus maximum output cost
and a per-task call slot. Settle the same row after the call. A crash leaves the
pessimistic reservation charged. The cap is min(configured, 30.00 USD) and the
ledger holds at most 3000 calls. Each model is billed at its own configured
price; a ledger built with a model price table refuses any model not in it
before a request is sent. Prices are never guessed. Fixture calls do not write
this ledger.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.call_trace import CallTraceDraft, build_call_trace
from app.config import HARD_BUDGET_CEILING_USD, HARD_MAX_TOTAL_CALLS
from app.db.models import ModelUsage
from app.db.session import begin_immediate
from app.errors import BudgetExceeded, CallLimitExceeded, PricesNotConfigured
from app.observability import enrich, get_logger, observed

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

    from app.config import Settings

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


def effective_call_limit(requested: int | None) -> int:
    if requested is not None and requested < 1:
        raise ValueError("总调用上限须为正整数")
    return HARD_MAX_TOTAL_CALLS if requested is None else min(requested, HARD_MAX_TOTAL_CALLS)


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
        price_in_per_mtok: Decimal | None = None,
        price_out_per_mtok: Decimal | None = None,
        max_total_calls: int | None = None,
        model_prices: Mapping[str, tuple[Decimal, Decimal]] | None = None,
    ) -> None:
        """model_prices, when given, is the only price source: unlisted models are refused.

        Without it the legacy single price pair applies to whatever model is
        reserved (direct construction by tests and single-model tools).
        """
        self._sessions = session_factory
        self.cap_usd = effective_cap(cap_usd)
        self.price_in_per_mtok = price_in_per_mtok
        self.price_out_per_mtok = price_out_per_mtok
        self.model_prices = None if model_prices is None else dict(model_prices)
        self.max_total_calls = effective_call_limit(max_total_calls)

    @classmethod
    def from_settings(
        cls,
        session_factory: sessionmaker[Session],
        settings: "Settings",
        *,
        cap_usd: Decimal | None = None,
        max_total_calls: int | None = None,
    ) -> "BudgetLedger":
        """Strict per-model ledger: only models priced in settings may be reserved."""
        return cls(
            session_factory,
            cap_usd=settings.effective_live_budget_usd if cap_usd is None else cap_usd,
            max_total_calls=max_total_calls,
            model_prices=settings.priced_models(),
        )

    def prices_for(self, model: str) -> tuple[Decimal | None, Decimal | None]:
        if self.model_prices is None:
            return self.price_in_per_mtok, self.price_out_per_mtok
        price = self.model_prices.get(model)
        if price is None:
            raise PricesNotConfigured(model=model)
        return price

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
        """Check every limit and persist the reservation under one writer lock.

        The row records the reserved model's own price, so settlement and any
        later audit use the price that was in force for that model.
        """
        price_in, price_out = self.prices_for(model)
        estimate = quote_cost(
            input_tokens=input_tokens,
            output_tokens=max_output_tokens,
            price_in_per_mtok=price_in,
            price_out_per_mtok=price_out,
        )
        enrich(reserved_usd=estimate, actual_usd=None)
        price_version = f"in={price_in};out={price_out}"
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
            price_in_per_mtok=price_in,
            price_out_per_mtok=price_out,
            at=reserved_at,
            status="RESERVED",
            reserved_at=reserved_at,
        )
        session = self._sessions()
        try:
            with begin_immediate(session):
                total_calls = session.scalar(select(func.count()).select_from(ModelUsage))
                assert total_calls is not None
                if total_calls >= self.max_total_calls:
                    raise CallLimitExceeded(
                        "本轮账本已达到模型总调用次数上限",
                        details={"calls": total_calls, "limit": self.max_total_calls},
                    )
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
        trace: CallTraceDraft | None = None,
    ) -> ModelUsage:
        """Replace the estimate with actual usage; retain the row as a call.

        Provider-reported usage is recorded even if it exceeds the reservation.
        Unknown usage should be settled at the reserved token estimates.
        ORPHANED rows accept late actual usage. Identical repeated settlements
        are no-ops; conflicting repeats are rejected so the first result wins.
        When trace metadata is provided, the call_trace row is written in the
        same transaction as this settlement so neither can persist alone.
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
                if trace is not None:
                    session.add(
                        build_call_trace(
                            trace,
                            ledger_row_id=row.id,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cost_usd=cost,
                        )
                    )
                    session.flush()
        finally:
            session.close()
        return row
