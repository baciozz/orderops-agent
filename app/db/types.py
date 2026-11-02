"""Exact decimal storage for SQLite.

SQLite NUMERIC affinity can turn a decimal into a float. Money is stored as a
quantized decimal string so the budget ledger survives a restart unchanged.
"""

from decimal import Decimal

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

_QUANTUM = Decimal("0.000001")


class Money(TypeDecorator[Decimal]):
    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: Decimal | str | int | None, dialect: object) -> str | None:
        if value is None:
            return None
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
        if amount < 0:
            raise ValueError("金额不能为负")
        return format(amount.quantize(_QUANTUM), "f")

    def process_result_value(self, value: str | None, dialect: object) -> Decimal | None:
        if value is None:
            return None
        return Decimal(value)
