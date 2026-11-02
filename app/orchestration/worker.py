"""Worker loop.

TODO(stage-2): lease, heartbeat, fencing token, and at-least-once inbox
consumption for R01–R09. Do not hold a database write transaction across a
model call.
"""


def run_once(*_args: object, **_kwargs: object) -> None:
    raise NotImplementedError("TODO(stage-2): worker 尚未实现")
