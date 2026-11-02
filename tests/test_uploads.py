"""Upload limits are frozen. Parsing itself is stage 3."""

import openpyxl
from app.adapters.uploads import (
    ALLOWED_SUFFIXES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_NONEMPTY_ROWS,
    MAX_SHEETS,
)


def test_upload_limits_match_the_plan() -> None:
    assert MAX_FILE_BYTES == 5 * 1024 * 1024
    assert MAX_FILES == 3
    assert MAX_SHEETS == 5
    assert MAX_NONEMPTY_ROWS == 5000
    assert ALLOWED_SUFFIXES == frozenset({".txt", ".md", ".xlsx"})
    workbook = openpyxl.Workbook()
    workbook.active["A1"] = "客户"
    assert workbook.sheetnames
