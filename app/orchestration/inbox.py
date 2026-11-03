"""Inbox dedup.

TODO(stage-2): persist the input event and the task in one transaction, and
ignore a duplicate source_event_key (R02).
"""


def accept_input(*_args: object, **_kwargs: object) -> None:
    raise NotImplementedError("TODO(stage-2): inbox 尚未实现")
