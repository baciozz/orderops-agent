"""Reject value evidence that openpyxl would silently discard in malformed XML."""

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import openpyxl
import pytest
from app.adapters import parser, uploads
from openpyxl.worksheet._read_only import ReadOnlyWorksheet

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def cell(payload, kind="inlineStr"):
    return f'<c r="A1" t="{kind}">{payload}</c>'


def workbook_bytes(
    cells,
    shared=None,
    *,
    shared_path="xl/sharedStrings.xml",
    later_sheet=False,
    worksheet_body=None,
):
    workbook = openpyxl.Workbook()
    workbook.active["A1"] = "safe"
    if later_sheet:
        workbook.create_sheet()["A1"] = "safe"
    original = BytesIO()
    workbook.save(original)
    workbook.close()
    output = BytesIO()
    sheet_path = f"xl/worksheets/sheet{2 if later_sheet else 1}.xml"
    if worksheet_body is None:
        worksheet_body = f'<sheetData><row r="1">{cells}</row></sheetData>'
    xml = f'<worksheet xmlns="{NS}">{worksheet_body}</worksheet>'
    with ZipFile(original) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for entry in source.infolist():
            data = source.read(entry)
            if entry.filename == sheet_path:
                data = xml.encode()
            elif shared is not None and entry.filename == "[Content_Types].xml":
                data = data.replace(
                    b"</Types>",
                    (
                        f'<Override PartName="/{shared_path}" ContentType="application/'
                        'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                        "</Types>"
                    ).encode(),
                )
            target.writestr(entry, data)
        if shared is not None:
            target.writestr(shared_path, f'<sst xmlns="{NS}">{shared}</sst>')
    return output.getvalue()


# The expected value documents the evidence loss in the pinned reader. Each
# payload is also accepted by the pre-fix static reader in the baseline run.
AMBIGUOUS_CELLS = [
    ('<c r="A1">qty 4<v>40</v></c>', 40),
    ('<c r="A1"><v>40</v>qty 4</c>', 40),
    (cell("<is>qty 4<t>qty 40</t></is>"), "qty 40"),
    (cell("<is><r><t>qty 40</t>qty 4</r></is>"), "qty 40"),
    (cell("<v>40</v><v>4</v>", "n"), 40),
    (cell("<is><t>qty 40</t></is><is><t>qty 4</t></is>"), "qty 40"),
    (cell("<is><t>qty 40</t><t>qty 4</t></is>"), "qty 4"),
    (cell("<is><r><t>qty 40</t><t>qty 4</t></r></is>"), "qty 4"),
    (cell('<v>40</v><c r="B1"><f>1+1</f></c>', "n"), 40),
    (cell("<is><r><t>40</t><f>1+1</f></r></is>"), "40"),
    (cell('<is><r><t>40</t><c r="B1"><v>4</v></c></r></is>'), "40"),
    (cell("<v>4</v><is><t>40</t></is>"), "40"),
    (cell("<v>4</v><is><t>40</t></is>", "n"), 4),
    (cell("<v>4</v><is><t>40</t></is>", "str"), "4"),
    ('<c r="A1"><v>4</v><is><t>40</t></is></c>', 4),
    (cell("<v>40</v>"), None),
    (cell("<is><t>40</t></is>", "n"), None),
    (cell("<v><t>40</t></v>", "n"), None),
    (cell("<is><r><rPr><t>hidden</t></rPr><t>40</t></r></is>"), "40"),
]


@pytest.mark.parametrize("cells,expected", AMBIGUOUS_CELLS)
def test_reader_reproduces_ambiguous_cell_evidence_loss(cells, expected):
    workbook = openpyxl.load_workbook(
        BytesIO(workbook_bytes(cells)), read_only=True, data_only=False
    )
    try:
        assert workbook.active["A1"].value == expected
    finally:
        workbook.close()


@pytest.mark.parametrize("cells,expected", AMBIGUOUS_CELLS)
def test_ambiguous_cells_rejected_before_row_iteration(cells, expected, monkeypatch):
    def must_not_iterate(*args, **kwargs):
        raise AssertionError("Malformed cell values must be rejected before iter_rows")

    monkeypatch.setattr(ReadOnlyWorksheet, "iter_rows", must_not_iterate)
    with pytest.raises(uploads.UploadValidationError) as caught:
        parser.split_source("SRC", "a.xlsx", workbook_bytes(cells))
    assert caught.value.code == "UPLOAD_CORRUPT"


AMBIGUOUS_SHARED = [
    '<si><t>40</t></si><x:si xmlns:x="urn:foreign"><t>4</t></x:si>',
    "<si><t>40</t></si><t>4</t>",
    "<si><t>40</t></si><wrapper><t>4</t></wrapper>",
    "<si><t>40</t></si><wrapper><si><t>4</t></si></wrapper>",
    "<si><t>qty 40</t>qty 4</si>",
    "qty 4<si><t>40</t></si>",
    "<si><t>40</t></si>qty 4",
    "<si><t>qty 40</t><t>qty 4</t></si>",
    "<si><r><t>qty 40</t><t>qty 4</t></r></si>",
    '<si><t>qty 40</t><c r="B1"><v>4</v></c></si>',
    "<si><t>qty 40</t><f>1+1</f></si>",
    "<si><t>qty 40</t><si><t>qty 4</t></si></si>",
]


@pytest.mark.parametrize("shared", AMBIGUOUS_SHARED)
@pytest.mark.parametrize("shared_path", ["xl/sharedStrings.xml", "xl/text/value-table.xml"])
def test_ambiguous_shared_strings_rejected_before_loading(shared, shared_path, monkeypatch):
    content = workbook_bytes(cell("<v>0</v>", "s"), shared, shared_path=shared_path)

    def must_not_load(*args, **kwargs):
        raise AssertionError("Malformed shared strings must be rejected before load_workbook")

    monkeypatch.setattr(openpyxl, "load_workbook", must_not_load)
    with pytest.raises(uploads.UploadValidationError) as caught:
        parser.split_source("SRC", "a.xlsx", content)
    assert caught.value.code == "UPLOAD_CORRUPT"


@pytest.mark.parametrize("shared", AMBIGUOUS_SHARED[7:9])
def test_reader_reproduces_shared_string_evidence_loss(shared):
    content = workbook_bytes(cell("<v>0</v>", "s"), shared)
    workbook = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=False)
    try:
        assert workbook.active["A1"].value == "qty 4"
    finally:
        workbook.close()


LEGAL_RICH_TEXT = [
    ("<t>qty 40</t>", "qty 40"),
    ("<r><t>qty </t></r><r><t>40</t></r>", "qty 40"),
    ("<t>qty </t><r><t>40</t></r>", "qty 40"),
    (
        '<r><rPr><b/></rPr><t xml:space="preserve">qty </t></r>'
        '<r><rPr><i/><color rgb="FF0000FF"/></rPr><t>40</t></r>',
        "qty 40",
    ),
    ('<t>上海</t><rPh sb="0" eb="2"><t>shanghai</t></rPh><phoneticPr fontId="0"/>', "上海"),
    (
        '<r><t>上海</t></r><rPh sb="0" eb="1"><t>shang</t></rPh>'
        '<rPh sb="1" eb="2"><t>hai</t></rPh>',
        "上海",
    ),
]


@pytest.mark.parametrize("payload,expected", LEGAL_RICH_TEXT)
@pytest.mark.parametrize("shared", [False, True])
def test_legal_rich_text_preserves_values(payload, expected, shared):
    content = (
        workbook_bytes(cell("<v>0</v>", "s"), f"<si>{payload}</si>")
        if shared
        else workbook_bytes(cell(f"<is>{payload}</is>"))
    )
    spans = parser.split_source("SRC", "a.xlsx", content)
    assert [(span["cell_range"], span["text_excerpt"]) for span in spans] == [("A1", expected)]


@pytest.mark.parametrize(
    "cells,expected",
    [
        (cell("<v>40</v>", "n"), "40"),
        (cell("<v>1</v>", "b"), "True"),
        (cell("<v>#N/A</v>", "e"), "#N/A"),
        (cell("<v>order</v>", "str"), "order"),
        (cell("<v/><is><t>40</t></is>"), "40"),
        (cell("<v>40</v><is><t/></is>", "n"), "40"),
        (cell('<v>40</v><is><rPh sb="0" eb="0"><t>annotation</t></rPh></is>', "n"), "40"),
        (cell("<v/><is><t/></is>"), None),
        (cell(""), None),
    ],
)
def test_static_types_and_empty_placeholders_remain_supported(cells, expected):
    content = workbook_bytes(cells)
    if expected is None:
        with uploads.static_workbook(content):
            pass
        with pytest.raises(uploads.UploadValidationError) as caught:
            parser.split_source("SRC", "a.xlsx", content)
        assert caught.value.code == "INPUT_EMPTY"
    else:
        spans = parser.split_source("SRC", "a.xlsx", content)
        assert [span["text_excerpt"] for span in spans] == [expected]


def test_malformed_later_sheet_rejected_before_any_row_iteration(monkeypatch):
    content = workbook_bytes(AMBIGUOUS_CELLS[0][0], later_sheet=True)

    def must_not_iterate(*args, **kwargs):
        raise AssertionError("All sheets must pass structural preflight before iter_rows")

    monkeypatch.setattr(ReadOnlyWorksheet, "iter_rows", must_not_iterate)
    with pytest.raises(uploads.UploadValidationError) as caught:
        parser.split_source("SRC", "a.xlsx", content)
    assert caught.value.code == "UPLOAD_CORRUPT"


@pytest.mark.parametrize("shared", [False, True])
def test_malformed_values_leave_no_api_evidence(
    shared, session_factory, database_url, tmp_path, monkeypatch
):
    from app.config import Settings
    from app.db.models import InputSource, SourceSpan, TaskSource
    from app.main import create_app
    from app.orchestration.inbox import accept_input
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    content = (
        workbook_bytes(cell("<v>0</v>", "s"), AMBIGUOUS_SHARED[0])
        if shared
        else workbook_bytes(AMBIGUOUS_CELLS[0][0])
    )
    task_id = accept_input(
        session_factory, thread_id="cell-integrity", client_event_key="cell-integrity", text="order"
    )[1]
    app = create_app(Settings(_env_file=None, database_url=database_url))
    app.state.source_storage_dir = tmp_path / "sources"

    def must_not_iterate(*args, **kwargs):
        raise AssertionError("Malformed values must not reach row iteration")

    monkeypatch.setattr(ReadOnlyWorksheet, "iter_rows", must_not_iterate)
    try:
        with TestClient(base_url="http://127.0.0.1", app=app) as client:
            response = client.post(
                "/sources", data={"task_id": task_id}, files={"file": ("malformed.xlsx", content)}
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UPLOAD_CORRUPT"
        assert response.json()["parser_status"] == "rejected"
        with session_factory() as session:
            for model in (InputSource, SourceSpan, TaskSource):
                assert session.scalar(select(func.count()).select_from(model)) == 0
        assert not app.state.source_storage_dir.exists()
    finally:
        app.state.engine.dispose()


def test_legal_row_and_cell_extensions_preserve_values():
    extension = '<extLst><ext uri="test"><note xmlns="urn:test">metadata</note></ext></extLst>'
    cells = cell(f"<is><t>40</t></is>{extension}") + extension
    spans = parser.split_source("SRC", "a.xlsx", workbook_bytes(cells))
    assert [(span["cell_range"], span["text_excerpt"]) for span in spans] == [("A1", "40")]


def test_valid_shared_strings_use_manifest_selected_part():
    content = workbook_bytes(
        cell("<v>0</v>", "s"), "<si><t>40</t></si>", shared_path="xl/text/value-table.xml"
    )
    assert parser.split_source("SRC", "a.xlsx", content)[0]["text_excerpt"] == "40"


def test_direct_formula_rejected_before_iteration(monkeypatch):
    content = workbook_bytes(cell("<f>1+1</f><v>2</v>", "n"))

    def must_not_iterate(*args, **kwargs):
        raise AssertionError("Formula XML must be rejected before row iteration")

    monkeypatch.setattr(ReadOnlyWorksheet, "iter_rows", must_not_iterate)
    with pytest.raises(uploads.UploadValidationError, match="含公式") as caught:
        parser.split_source("SRC", "a.xlsx", content)
    assert caught.value.code == "UPLOAD_REJECTED"


@pytest.mark.parametrize(
    "extensions,expected_code",
    [
        ('<extLst r="XFD1"/>', "UPLOAD_REJECTED"),
        ('<extLst r="IW1"/>', "UPLOAD_REJECTED"),
        ('<extLst r="B1"/>', "UPLOAD_CORRUPT"),
        ("<extLst/><extLst/>", "UPLOAD_CORRUPT"),
        ("<extLst/>" * 257, "UPLOAD_CORRUPT"),
        ('<extLst/><c r="B1"><v>4</v></c>', "UPLOAD_CORRUPT"),
    ],
)
def test_row_extensions_cannot_amplify_coordinates_or_hide_cells(
    extensions, expected_code, monkeypatch
):
    content = workbook_bytes(cell("<is><t>40</t></is>") + extensions)

    def must_not_iterate(*args, **kwargs):
        raise AssertionError("Invalid row extensions must be rejected before iter_rows")

    monkeypatch.setattr(ReadOnlyWorksheet, "iter_rows", must_not_iterate)
    with pytest.raises(uploads.UploadValidationError) as caught:
        parser.split_source("SRC", "a.xlsx", content)
    assert caught.value.code == expected_code


@pytest.mark.parametrize("shared", [False, True])
def test_xml_formatting_whitespace_is_not_conflicting_evidence(shared):
    text = '\n <t xml:space="preserve">qty </t>\n <r>\n <t>40</t>\n </r>\n'
    content = (
        workbook_bytes(cell("\n<v>0</v>\n", "s"), f"\n<si>{text}</si>\n")
        if shared
        else workbook_bytes(cell(f"\n<is>{text}</is>\n") + "\n\t ")
    )
    assert parser.split_source("SRC", "a.xlsx", content)[0]["text_excerpt"] == "qty 40"


@pytest.mark.parametrize("tail", ["qty 4", "\u00a0", " " * 32768 + "qty 4"])
@pytest.mark.parametrize("shared", [False, True])
def test_container_tails_rejected_even_across_parser_chunks(tail, shared, monkeypatch):
    content = (
        workbook_bytes(cell("<v>0</v>", "s"), f"<si><t>40</t></si>{tail}")
        if shared
        else workbook_bytes(cell("<v>40</v>", "n") + tail)
    )

    def must_not_iterate(*args, **kwargs):
        raise AssertionError("Conflicting tails must not reach iter_rows")

    monkeypatch.setattr(ReadOnlyWorksheet, "iter_rows", must_not_iterate)
    with pytest.raises(uploads.UploadValidationError) as caught:
        parser.split_source("SRC", "a.xlsx", content)
    assert caught.value.code == "UPLOAD_CORRUPT"


def test_shared_string_extension_metadata_remains_supported():
    shared = (
        '<si><t>40</t></si><extLst><ext uri="test">'
        '<note xmlns="urn:test">metadata</note></ext></extLst>'
    )
    content = workbook_bytes(cell("<v>0</v>", "s"), shared)
    assert parser.split_source("SRC", "a.xlsx", content)[0]["text_excerpt"] == "40"


@pytest.mark.parametrize(
    "body",
    [
        '<sheetData><row r="1"><c r="A1"><v>40</v></c></row>'
        '<row xmlns="urn:foreign" r="2"><c r="A2"><v>4</v></c></row></sheetData>',
        '<sheetData><row r="1"><c r="A1"><v>40</v></c></row></sheetData>'
        '<row r="2"><c r="A2"><v>4</v></c></row>',
        '<sheetData><row r="1"><c r="A1"><v>40</v></c></row></sheetData>'
        '<sheetData><row r="2"><c r="A2"><v>4</v></c></row></sheetData>',
        f'<sheetData xmlns="urn:foreign"><row xmlns="{NS}" r="1">'
        '<c r="A1"><v>40</v></c></row></sheetData>',
        '<sheetData><wrapper><row r="1"><c r="A1"><v>40</v></c></row></wrapper></sheetData>',
        '<sheetData><row r="1"><c r="A1"><v>40</v></c></row>qty 4</sheetData>',
    ],
    ids=[
        "foreign_row",
        "misplaced_row",
        "duplicate_data",
        "foreign_data",
        "wrapped_row",
        "data_tail",
    ],
)
def test_worksheet_value_ancestry_is_unambiguous(body, monkeypatch):
    content = workbook_bytes("", worksheet_body=body)

    def must_not_iterate(*args, **kwargs):
        raise AssertionError("Malformed data ancestry must be rejected before iter_rows")

    monkeypatch.setattr(ReadOnlyWorksheet, "iter_rows", must_not_iterate)
    with pytest.raises(uploads.UploadValidationError) as caught:
        parser.split_source("SRC", "a.xlsx", content)
    assert caught.value.code == "UPLOAD_CORRUPT"


def test_worksheet_headers_footers_and_extension_text_are_metadata():
    body = (
        '<sheetData>\n <row r="1">\n <c r="A1"><v>40</v></c>\n </row>\n </sheetData>'
        "<headerFooter><oddHeader>&amp;COrder header</oddHeader>"
        "<oddFooter>&amp;CPage &amp;P</oddFooter></headerFooter>"
        '<extLst><ext uri="test"><row xmlns="urn:test">Textual metadata</row></ext></extLst>'
    )
    content = workbook_bytes("", worksheet_body=body)
    spans = parser.split_source("SRC", "a.xlsx", content)
    assert [(span["cell_range"], span["text_excerpt"]) for span in spans] == [("A1", "40")]
