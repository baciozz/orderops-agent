"""Atomic commit.

TODO(stage-1A): one BEGIN IMMEDIATE transaction for the order revision, stock
movements, approval consumption, operation, audit, and outbox.
TODO(stage-1B): include document revisions and downstream relinks in that same
transaction (B27).
"""


def commit_plan(*_args: object, **_kwargs: object) -> None:
    raise NotImplementedError("TODO(stage-1A): 原子提交尚未实现")
