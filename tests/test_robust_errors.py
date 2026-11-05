"""Real HTTP error paths must reject input without any persistent side effect."""

import json
import logging
from io import BytesIO
from xml.etree.ElementTree import ParseError
from xml.parsers.expat import ExpatError
from zipfile import ZipFile

import openpyxl
import pytest
from app.adapters.uploads import MAX_FILE_BYTES, MAX_REQUEST_BYTES
from app.config import Settings
from app.db.base import Base
from app.main import create_app
from app.orchestration.inbox import accept_input
from fastapi.testclient import TestClient
from sqlalchemy import func, select


@pytest.fixture()
def error_app(session_factory, database_url, tmp_path):
    app = create_app(Settings(_env_file=None, database_url=database_url))
    storage = tmp_path / "sources"
    storage.mkdir()
    app.state.source_storage_dir = storage
    task_id = accept_input(
        session_factory, thread_id="uploads", client_event_key="initial", text="保存输入"
    )[1]
    with TestClient(base_url="http://127.0.0.1", app=app, raise_server_exceptions=False) as client:
        # Keep existing evidence in the baseline to catch accidental deletion as
        # well as newly orphaned rows/files on a rejected request.
        assert (
            client.post(
                "/sources",
                data={"task_id": task_id},
                files={"file": ("existing.txt", b"existing evidence")},
            ).status_code
            == 200
        )
        yield client, app, session_factory, task_id, storage
    app.state.engine.dispose()


def snapshot(factory, storage):
    # All mapped tables, including tasks/events/sources/spans/deliveries, plans,
    # approvals, operations and inventory. Count existing seed/receipt rows too.
    with factory() as session:
        counts = {
            table.name: session.scalar(select(func.count()).select_from(table))
            for table in Base.metadata.sorted_tables
        }
    return counts, {
        p.relative_to(storage): p.read_bytes() for p in storage.rglob("*") if p.is_file()
    }


def assert_rejected(response, code, before, factory, storage):
    assert 400 <= response.status_code < 500, response.text
    payload = response.json()
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]
    assert payload["error"]["retryable"] is False
    assert payload["schema_version"] == "1.0"
    assert payload["request_id"] and payload["correlation_id"]
    assert snapshot(factory, storage) == before


def workbook_content():
    book = openpyxl.Workbook()
    stream = BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def non_workbook_zip():
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("ordinary.txt", "This is a zip, not a workbook")
    return stream.getvalue()


@pytest.mark.parametrize(
    ("name", "content", "code"),
    [
        ("random.xlsx", b"\x98random\x00bytes", "UPLOAD_CORRUPT"),
        ("truncated.xlsx", workbook_content()[:-40], "UPLOAD_CORRUPT"),
        ("archive.xlsx", non_workbook_zip(), "UPLOAD_CORRUPT"),
        ("large.txt", b"x" * (MAX_FILE_BYTES + 1), "UPLOAD_TOO_LARGE"),
        ("empty.txt", b"", "INPUT_EMPTY"),
        ("empty.md", b" \t\r\n", "INPUT_EMPTY"),
        ("empty.xlsx", b"", "INPUT_EMPTY"),
        ("blank.xlsx", workbook_content(), "INPUT_EMPTY"),
        ("invalid.txt", b"\xff\xfe\x80", "ENCODING_INVALID"),
        ("invalid.md", b"\xff\xfe\x80", "ENCODING_INVALID"),
        ("mixed.txt", "中文".encode("gbk") + b"\xff\x81random", "ENCODING_INVALID"),
        ("mixed.md", "中文".encode("gbk") + b"\xff\x81random", "ENCODING_INVALID"),
        ("nul.txt", b"valid\x00text", "ENCODING_INVALID"),
        ("nul.md", b"\x00", "ENCODING_INVALID"),
    ],
    ids=[
        "random-xlsx",
        "truncated-xlsx",
        "non-workbook-zip",
        "oversized-file",
        "empty-text-file",
        "whitespace-file",
        "empty-xlsx",
        "blank-workbook",
        "invalid-utf8-txt",
        "invalid-utf8-md",
        "mixed-encoding-txt",
        "mixed-encoding-md",
        "nul-txt",
        "nul-md",
    ],
)
def test_upload_rejection_is_atomic(error_app, name, content, code):
    client, _, factory, task_id, storage = error_app
    before = snapshot(factory, storage)
    response = client.post("/sources", data={"task_id": task_id}, files={"file": (name, content)})
    assert_rejected(response, code, before, factory, storage)


@pytest.mark.parametrize(
    "exception_type", [TypeError, IndexError, AttributeError, RuntimeError, NotImplementedError]
)
@pytest.mark.parametrize("phase", ["load", "read"])
def test_xlsx_parser_defect_has_traceable_private_error(
    error_app, monkeypatch, caplog, exception_type, phase
):
    client, _, factory, task_id, storage = error_app
    marker = "PRIVATE-CELL-7f60bc159d"
    book = openpyxl.Workbook()
    book.active["A1"] = marker
    stream = BytesIO()
    book.save(stream)
    book.close()
    content = stream.getvalue()
    original_load = openpyxl.load_workbook

    def fail(*args, **kwargs):
        # Even exception messages/chains containing source values must be redacted.
        try:
            raise ValueError(marker)
        except ValueError as exc:
            raise exception_type(marker) from exc

    def fail_on_read(*args, **kwargs):
        workbook = original_load(*args, **kwargs)
        sheet = workbook.worksheets[0]

        def iter_rows(*args, **kwargs):
            fail()  # Fail during static validation.

        monkeypatch.setattr(sheet, "iter_rows", iter_rows)
        return workbook

    before = snapshot(factory, storage)
    monkeypatch.setattr(openpyxl, "load_workbook", fail if phase == "load" else fail_on_read)
    with caplog.at_level(logging.ERROR, logger="app.adapters.uploads"):
        response = client.post(
            "/sources", data={"task_id": task_id}, files={"file": ("valid.xlsx", content)}
        )
    assert response.status_code == 400
    assert_rejected(response, "UPLOAD_PARSE_FAILED", before, factory, storage)
    error_id = response.json()["error"]["details"]["error_id"]
    assert error_id.startswith("ERR-") and len(error_id) == 36
    records = [record for record in caplog.records if error_id in record.getMessage()]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR
    assert record.exc_info and record.exc_info[2] is not None
    assert "suffix=.xlsx" in record.getMessage()
    assert f"size_bytes={len(content)}" in record.getMessage()
    assert "Traceback (most recent call last)" in caplog.text
    assert exception_type.__name__ in caplog.text
    assert marker not in caplog.text
    assert marker not in response.text


@pytest.mark.parametrize("exception_type", [ValueError, ParseError, ExpatError, EOFError, KeyError])
def test_xlsx_format_errors_remain_corrupt(error_app, monkeypatch, caplog, exception_type):
    client, _, factory, task_id, storage = error_app
    content = workbook_content()

    def fail(*args, **kwargs):
        raise exception_type("invalid workbook format")

    monkeypatch.setattr(openpyxl, "load_workbook", fail)
    before = snapshot(factory, storage)
    response = client.post(
        "/sources", data={"task_id": task_id}, files={"file": ("invalid.xlsx", content)}
    )
    assert response.status_code == 400
    assert_rejected(response, "UPLOAD_CORRUPT", before, factory, storage)
    assert "error_id" not in response.json()["error"]["details"]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.parametrize("endpoint", ["/inputs", "/dispatch", "/tasks/{task_id}/answers"])
@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "INPUT_EMPTY"),
        (" \t\n\u3000", "INPUT_EMPTY"),
        ("hello\x00world", "ENCODING_INVALID"),
        ("\ud800", "ENCODING_INVALID"),
        ("x" * (MAX_FILE_BYTES + 1), "UPLOAD_TOO_LARGE"),
    ],
    ids=["empty", "whitespace", "nul", "surrogate", "oversized"],
)
def test_message_rejection_is_atomic(error_app, endpoint, text, code):
    client, _, factory, task_id, storage = error_app
    body = {"text": text, "client_event_key": "bad"}
    if endpoint != "/dispatch":
        body["thread_id"] = "message"
    before = snapshot(factory, storage)
    response = client.post(
        endpoint.format(task_id=task_id),
        content=json.dumps(body),
        headers={"content-type": "application/json"},
    )
    assert_rejected(response, code, before, factory, storage)


@pytest.mark.parametrize("endpoint", ["/inputs", "/dispatch", "/sources"])
@pytest.mark.parametrize("chunked", [False, True])
def test_request_body_overflow_is_atomic(error_app, endpoint, chunked):
    client, _, factory, _, storage = error_app
    before = snapshot(factory, storage)
    body = b"x" * (MAX_REQUEST_BYTES + 1)
    response = client.post(
        endpoint,
        content=iter([body[:100], body[100:]]) if chunked else body,
        headers={"content-type": "application/json"},
    )
    assert_rejected(response, "UPLOAD_TOO_LARGE", before, factory, storage)


@pytest.mark.parametrize("endpoint", ["/inputs", "/dispatch"])
def test_invalid_utf8_json_has_error_envelope(error_app, endpoint):
    client, _, factory, _, storage = error_app
    before = snapshot(factory, storage)
    response = client.post(
        endpoint, content=b'{"text":"\xff"}', headers={"content-type": "application/json"}
    )
    assert_rejected(response, "VALIDATION_ERROR", before, factory, storage)


def test_upload_database_failure_removes_file_and_partial_rows(error_app, monkeypatch):
    # Inject a failure after file creation and parent INSERT; rollback must cover
    # the source row, and the route must remove its corresponding stored file.
    from app.db.models import InputSource
    from app.errors import AppError
    from sqlalchemy.orm import Session

    client, _, factory, task_id, storage = error_app
    original = Session.flush

    def fail_after_flush(session, *args, **kwargs):
        inserting_source = any(isinstance(row, InputSource) for row in session.new)
        original(session, *args, **kwargs)
        if inserting_source:
            raise AppError("拒绝持久化", code="UPLOAD_REJECTED")

    before = snapshot(factory, storage)
    with monkeypatch.context() as patch:
        patch.setattr(Session, "flush", fail_after_flush)
        response = client.post(
            "/sources", data={"task_id": task_id}, files={"file": ("valid.txt", b"text")}
        )
    assert_rejected(response, "UPLOAD_REJECTED", before, factory, storage)


def test_framework_http_errors_use_specific_codes(error_app):
    client = error_app[0] if isinstance(error_app, tuple) else error_app
    missing = client.get("/no-such-route")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    wrong_method = client.delete("/health")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
