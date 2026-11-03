"""Deterministic precheck.

TODO(stage-1A): read order, shipment, and stock from the database and produce
a normalized plan for B01–B18 and new-order cases B28–B32.
TODO(stage-1B): attach downstream verdicts for B19–B27. Never trust a sub-agent's
stock or version claims.
"""


def precheck(*_args: object, **_kwargs: object) -> None:
    raise NotImplementedError("TODO(stage-1A): 确定性预检尚未实现")
