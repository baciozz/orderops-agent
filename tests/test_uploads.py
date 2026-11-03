"""Upload limits are frozen. Parsing itself is stage 3."""

from email.message import Message

import openpyxl
import pytest
from app.adapters.uploads import (
    ALLOWED_SUFFIXES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_NONEMPTY_ROWS,
    MAX_SHEETS,
)
from app.api.sources import _payload_bytes


def test_upload_limits_match_the_plan() -> None:
    assert MAX_FILE_BYTES == 5 * 1024 * 1024
    assert MAX_FILES == 3
    assert MAX_SHEETS == 5
    assert MAX_NONEMPTY_ROWS == 5000
    assert ALLOWED_SUFFIXES == frozenset({".txt", ".md", ".xlsx"})
    workbook = openpyxl.Workbook()
    workbook.active["A1"] = "客户"
    assert workbook.sheetnames


@pytest.mark.parametrize("payload", ["invalid", [], [Message()]])
def test_non_bytes_multipart_payload_rejected(monkeypatch, payload):
    part = Message()
    monkeypatch.setattr(part, "get_payload", lambda decode: payload)
    with pytest.raises(ValueError, match="字段内容格式不正确"):
        _payload_bytes(part)


@pytest.mark.parametrize("payload,expected", [(None, b""), (b"", b""), (b"data", b"data")])
def test_multipart_payload_bytes(monkeypatch, payload, expected):
    part = Message()
    monkeypatch.setattr(part, "get_payload", lambda decode: payload)
    assert _payload_bytes(part) == expected
