"""Rule-based watchdog.

TODO(stage-2): requeue expired leases or mark the task failed. Do not rewrite
rules, permissions, or business rows.
"""


def scan(*_args: object, **_kwargs: object) -> None:
    raise NotImplementedError("TODO(stage-2): watchdog 尚未实现")
