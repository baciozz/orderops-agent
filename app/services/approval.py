"""Human approval lives in app.services.kernel.

The actor comes from the server demo identity, never from a request body or a
model field named approved.
"""


def approve(*_args: object, **_kwargs: object) -> None:
    raise NotImplementedError("TODO(stage-1A): 人工批准尚未实现")
