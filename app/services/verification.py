"""Post-commit read-back check.

TODO(stage-1B): verify downstream links, MTO coverage, and fabric coverage.
On failure set committed_verification_pending and do not write again (B21).
"""


def verify_commit(*_args: object, **_kwargs: object) -> None:
    raise NotImplementedError("TODO(stage-1B): 提交后回读核验尚未实现")
