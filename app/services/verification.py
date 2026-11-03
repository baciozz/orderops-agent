"""Post-commit checks live in app.services.kernel.

They cover stock balances and downstream link, production, and fabric coverage.
On failure the operation is marked pending and is not rewritten.
"""


def verify_commit(*_args: object, **_kwargs: object) -> None:
    raise NotImplementedError("TODO(stage-1B): 提交后回读核验尚未实现")
