"""Atomic commit lives in app.services.kernel.

TODO(stage-1B): include document revisions and downstream relinks in that same
transaction (B27).
"""


def commit_plan(*_args: object, **_kwargs: object) -> None:
    raise NotImplementedError("TODO(stage-1A): 原子提交尚未实现")
