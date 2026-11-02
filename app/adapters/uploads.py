"""Upload limits frozen from plan section 9.

TODO(stage-3): enforce size, sheet, and row caps, reject XLSM and macros,
and refuse formula cells as business facts (R11). Parsing is not an agent.
"""

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 3
MAX_SHEETS = 5
MAX_NONEMPTY_ROWS = 5000
ALLOWED_SUFFIXES = frozenset({".txt", ".md", ".xlsx"})


def parse_xlsx(_content: bytes, _original_name: str) -> None:
    raise NotImplementedError("TODO(stage-3): XLSX 切分为 SourceSpan，并拒绝宏与超限文件")
