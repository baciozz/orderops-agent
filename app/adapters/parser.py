"""Deterministic file-to-span splitting.

TODO(stage-3): turn TXT, MD, and XLSX into SourceSpan rows. This module is not
a sub-agent. The parser sub-agent reads spans and proposes a BusinessEvent.
"""


def split_source(*_args: object, **_kwargs: object) -> None:
    raise NotImplementedError("TODO(stage-3): 文件切分尚未实现")
