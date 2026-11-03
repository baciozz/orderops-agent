"""Upload limits are frozen. Parsing itself is stage 3."""

import logging
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


def test_upload_limits_match_the_plan() -> None:
    assert MAX_FILE_BYTES == 5 * 1024 * 1024
    assert MAX_FILES == 3
    assert MAX_SHEETS == 5
    assert MAX_NONEMPTY_ROWS == 5000
    assert ALLOWED_SUFFIXES == frozenset({".txt", ".md", ".xlsx"})
    workbook = openpyxl.Workbook()
    workbook.active["A1"] = "客户"
    assert workbook.sheetnames


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
