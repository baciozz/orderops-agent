"""Upload limits and a narrow order-sheet reader for stage 1A.

TODO(stage-3): full span extraction, macro rejection, and formula distrust (R11).
"""

from io import BytesIO

import openpyxl

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 3
MAX_SHEETS = 5
MAX_NONEMPTY_ROWS = 5000
ALLOWED_SUFFIXES = frozenset({".txt", ".md", ".xlsx"})


def read_order_sheet(content: bytes) -> list[dict[str, object]]:
    """Read 客户/款/颜色/尺码/数量. Static cells only; this is not the full parser."""
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("文件超过 5 MB")
    workbook = openpyxl.load_workbook(BytesIO(content), data_only=False, read_only=True)
    sheet = workbook.worksheets[0]
    rows: list[dict[str, object]] = []
    for index, row in enumerate(sheet.iter_rows(min_row=2, max_col=5), start=2):
        if all(cell.value is None for cell in row):
            continue
        if len(rows) >= MAX_NONEMPTY_ROWS:
            raise ValueError("非空行超过上限")
        rows.append(
            {
                "customer_name": row[0].value,
                "style": row[1].value,
                "color": row[2].value,
                "size": row[3].value,
                "qty": int(row[4].value),
                "source_ref": {
                    "source_id": "SRC-XLSX",
                    "sheet": sheet.title,
                    "cell_range": f"A{index}:E{index}",
                },
            }
        )
    workbook.close()
    return rows
