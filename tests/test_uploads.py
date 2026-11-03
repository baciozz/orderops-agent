"""Upload limits are frozen. Parsing itself is stage 3."""

import logging
from email.message import Message
from io import BytesIO
from unittest.mock import Mock

import openpyxl
import pytest
from app.adapters.uploads import (
    ALLOWED_SUFFIXES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_NONEMPTY_ROWS,
    MAX_SHEETS,
    static_workbook,
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


class CallerError(Exception):
    pass


@pytest.mark.parametrize("exception_type", [CallerError, ValueError, TypeError])
def test_static_workbook_preserves_caller_exception(exception_type, monkeypatch, caplog):
    stream = BytesIO()
    source = openpyxl.Workbook()
    source.active["A1"] = "valid static value"
    source.save(stream)
    source.close()
    original_load = openpyxl.load_workbook
    close = Mock()

    def load(*args, **kwargs):
        workbook = original_load(*args, **kwargs)
        close.side_effect = workbook.close
        monkeypatch.setattr(workbook, "close", close)
        return workbook

    monkeypatch.setattr(openpyxl, "load_workbook", load)
    error = exception_type("caller error unchanged")
    with caplog.at_level(logging.ERROR):
        with pytest.raises(exception_type) as caught:
            with static_workbook(stream.getvalue()):
                raise error
    assert caught.value is error
    assert type(caught.value) is exception_type
    assert str(caught.value) == "caller error unchanged"
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    close.assert_called_once_with()
