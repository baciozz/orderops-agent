"""Sparse coordinates and tiny lines must not amplify into unbounded parser work."""

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import openpyxl
import pytest
from app.adapters import parser, uploads
from openpyxl.worksheet._read_only import ReadOnlyWorksheet


def workbook_bytes(coordinate="A1"):
    workbook = openpyxl.Workbook()
    workbook.active[coordinate] = "order"
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def rewrite_sheet(content, transform):
    output = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for entry in source.infolist():
            data = source.read(entry)
            if entry.filename == "xl/worksheets/sheet1.xml":
                data = transform(data.decode()).encode()
            target.writestr(entry, data)
    return output.getvalue()


def test_text_span_boundary():
    content = b"x\n" * uploads.MAX_SOURCE_SPANS
    assert len(parser.split_source("SRC", "a.txt", content)) == uploads.MAX_SOURCE_SPANS
    with pytest.raises(uploads.UploadValidationError, match="来源片段超过上限"):
        parser.split_source("SRC", "a.md", content + b"x")


def test_text_streaming_preserves_splitlines_locations():
    text = (
        "\ufefffirst\r\n \nsecond\rthird\vfourth\ffifth\x1csixth"
        "\x1dseventh\x1eeighth\x85ninth\u2028tenth\u2029last"
    )
    spans = parser.split_source("SRC", "a.txt", text.encode())
    expected = [
        (i, line) for i, line in enumerate(text.lstrip("\ufeff").splitlines(), 1) if line.strip()
    ]
    assert [(s["line_start"], s["text_excerpt"]) for s in spans] == expected
    assert all(s["line_start"] == s["line_end"] for s in spans)


def test_xlsx_span_limit_aggregates_sheets(monkeypatch):
    monkeypatch.setattr(parser, "MAX_SOURCE_SPANS", 2)
    workbook = openpyxl.Workbook()
    workbook.active.append(["one", "two"])
    output = BytesIO()
    workbook.save(output)
    assert len(parser.split_source("SRC", "a.xlsx", output.getvalue())) == 2
    workbook.create_sheet()["A1"] = "three"
    output = BytesIO()
    workbook.save(output)
    with pytest.raises(uploads.UploadValidationError, match="来源片段超过上限"):
        parser.split_source("SRC", "a.xlsx", output.getvalue())
    workbook.close()


@pytest.mark.parametrize("coordinate", ["A10001", "IW1", "A1048576"])
def test_sparse_coordinates_rejected_before_row_expansion(coordinate, monkeypatch):
    content = workbook_bytes(coordinate)

    def must_not_expand(*args, **kwargs):
        raise AssertionError("openpyxl iteration must not start")

    monkeypatch.setattr(ReadOnlyWorksheet, "iter_rows", must_not_expand)
    with pytest.raises(uploads.UploadValidationError, match="位置超过上限"):
        parser.split_source("SRC", "a.xlsx", content)


def test_sparse_boundary_preserves_coordinates_and_ignores_false_dimensions():
    content = workbook_bytes("IV10000")
    content = rewrite_sheet(
        content, lambda xml: xml.replace('ref="IV10000:IV10000"', 'ref="A1:XFD1048576"')
    )
    spans = parser.split_source("SRC", "a.xlsx", content)
    assert len(spans) == 1
    assert spans[0]["cell_range"] == "IV10000"
    assert spans[0]["text_excerpt"] == "order"


@pytest.mark.parametrize("replacement", ['r="A10001"', 'r="IW1"'])
def test_cell_coordinate_cannot_bypass_row_coordinate(replacement):
    content = rewrite_sheet(workbook_bytes(), lambda xml: xml.replace('r="A1"', replacement))
    with pytest.raises(uploads.UploadValidationError, match="位置超过上限"):
        parser.split_source("SRC", "a.xlsx", content)


def test_implicit_row_and_cell_coordinates_obey_limits(monkeypatch):
    monkeypatch.setattr(uploads, "MAX_WORKSHEET_ROWS", 1)
    content = rewrite_sheet(
        workbook_bytes(),
        lambda xml: xml.replace(
            "</sheetData>", '<row><c t="inlineStr"><is><t>next</t></is></c></row></sheetData>'
        ),
    )
    with pytest.raises(uploads.UploadValidationError, match="行位置超过上限"):
        parser.split_source("SRC", "a.xlsx", content)
    monkeypatch.setattr(uploads, "MAX_WORKSHEET_COLUMNS", 1)
    content = rewrite_sheet(
        workbook_bytes(),
        lambda xml: xml.replace("</row>", '<c t="inlineStr"><is><t>next</t></is></c></row>'),
    )
    with pytest.raises(uploads.UploadValidationError, match="列位置超过上限"):
        parser.split_source("SRC", "a.xlsx", content)


def test_foreign_row_child_cannot_bypass_coordinate_guard():
    content = rewrite_sheet(
        workbook_bytes(), lambda xml: xml.replace('<c r="A1"', '<c xmlns="urn:foreign" r="IW1"')
    )
    with pytest.raises(uploads.UploadValidationError, match="列位置超过上限"):
        parser.split_source("SRC", "a.xlsx", content)


@pytest.mark.parametrize("filename,content", [("a.txt", b"one\ntwo\nthree"), ("a.xlsx", None)])
def test_resource_rejections_are_atomic_api_errors(
    filename, content, session_factory, database_url, tmp_path, monkeypatch
):
    from app.config import Settings
    from app.db.models import InputSource, SourceSpan, TaskSource
    from app.main import create_app
    from app.orchestration.inbox import accept_input
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    monkeypatch.setattr(parser, "MAX_SOURCE_SPANS", 2)
    task_id = accept_input(
        session_factory, thread_id="limits", client_event_key="limits", text="save evidence"
    )[1]
    app = create_app(Settings(_env_file=None, database_url=database_url))
    app.state.source_storage_dir = tmp_path / "sources"
    try:
        with TestClient(app) as client:
            response = client.post(
                "/sources",
                data={"task_id": task_id},
                files={"file": (filename, content or workbook_bytes("A10001"))},
            )
        assert response.status_code == 400
        assert response.json()["parser_status"] == "rejected"
        assert response.json()["limits"]["max_worksheet_rows"] == uploads.MAX_WORKSHEET_ROWS
        with session_factory() as session:
            for model in (InputSource, SourceSpan, TaskSource):
                assert session.scalar(select(func.count()).select_from(model)) == 0
        assert not app.state.source_storage_dir.exists()
    finally:
        app.state.engine.dispose()


@pytest.mark.parametrize("coordinate", ["A2", "IW2"])
def test_nested_rows_rejected_before_openpyxl_expansion(coordinate, monkeypatch):
    import re

    replacement = (
        '<sheetData><row r="2"><row r="1"/>'
        f'<c r="{coordinate}" t="inlineStr"><is><t>order</t></is></c>'
        "</row></sheetData>"
    )
    content = rewrite_sheet(
        workbook_bytes(), lambda xml: re.sub(r"<sheetData>.*?</sheetData>", replacement, xml)
    )

    def must_not_expand(*args, **kwargs):
        raise AssertionError("Malformed row nesting must be rejected before iteration")

    monkeypatch.setattr(ReadOnlyWorksheet, "iter_rows", must_not_expand)
    with pytest.raises(uploads.UploadValidationError, match="行不能嵌套") as caught:
        parser.split_source("SRC", "a.xlsx", content)
    assert caught.value.code == "UPLOAD_CORRUPT"


@pytest.mark.parametrize(
    "rows",
    [
        '<row r="1"><c r="A1"><f>1+1</f></c><c r="A1" t="inlineStr"><is><t>safe</t></is></c></row>',
        '<row r="1"><c r="A1"><v>40</v></c><c r="A1"><v>4</v></c></row>',
        '<row r="1"><c r="B1"><v>40</v></c><c r="A1"><v>4</v></c></row>',
        '<row r="1"><c r="A1"><v>40</v></c></row><row r="1"><c r="A1"><v>4</v></c></row>',
        '<row r="2"><c r="A2"><v>40</v></c></row><row r="1"><c r="A1"><v>4</v></c></row>',
        '<row r="1"><c r="A2"><v>40</v></c></row>',
    ],
    ids=[
        "hidden_formula",
        "duplicate_cell",
        "reversed_cells",
        "duplicate_row",
        "reversed_rows",
        "row_mismatch",
    ],
)
def test_ambiguous_coordinates_rejected_before_values_can_be_discarded(rows, monkeypatch):
    import re

    content = rewrite_sheet(
        workbook_bytes(),
        lambda xml: re.sub(r"<sheetData>.*?</sheetData>", f"<sheetData>{rows}</sheetData>", xml),
    )

    def must_not_expand(*args, **kwargs):
        raise AssertionError("Ambiguous coordinates must be rejected before iteration")

    monkeypatch.setattr(ReadOnlyWorksheet, "iter_rows", must_not_expand)
    with pytest.raises(uploads.UploadValidationError) as caught:
        parser.split_source("SRC", "a.xlsx", content)
    assert caught.value.code == "UPLOAD_CORRUPT"


@pytest.mark.parametrize("implicit", ["rows", "cells", "both"])
def test_valid_implicit_coordinates_preserve_all_evidence(implicit):
    import re

    workbook = openpyxl.Workbook()
    workbook.active.append(["one", "two"])
    workbook.active.append(["three", "four"])
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    def omit_coordinates(xml):
        if implicit in {"rows", "both"}:
            xml = re.sub(r'(<row) r="\d+"', r"\1", xml)
        if implicit in {"cells", "both"}:
            xml = re.sub(r'(<c) r="[A-Z]+\d+"', r"\1", xml)
        return xml

    content = rewrite_sheet(output.getvalue(), omit_coordinates)
    spans = parser.split_source("SRC", "a.xlsx", content)
    assert [(span["cell_range"], span["text_excerpt"]) for span in spans] == [
        ("A1", "one"),
        ("B1", "two"),
        ("A2", "three"),
        ("B2", "four"),
    ]
