"""Outbox delivery.

TODO(stage-2): write the outbox row in the commit transaction and redeliver
after restart without applying the business write twice (R07).
"""


def publish_pending(*_args: object, **_kwargs: object) -> None:
    raise NotImplementedError("TODO(stage-2): outbox 尚未实现")
