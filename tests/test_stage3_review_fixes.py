"""Fixture-only regressions for Stage 3 spreadsheet review fixes."""

import csv
from decimal import Decimal
from io import BytesIO, StringIO
from zipfile import ZIP_DEFLATED, ZipFile

import defusedxml
import openpyxl
import pytest
from app.adapters.uploads import static_workbook
from app.db.models import InputSource, SourceSpan
from app.services.export import _export_value, export_change_list, sanitize_export_text
from sqlalchemy import func, select
from tests.test_stage3 import new_task, upload, workbook_bytes
from tests.test_stage3 import stage3 as stage3

FORMULA_PREFIXES = [
    "",
    " ",
    "\t",
    "\r",
    "\n",
    "\u3000",
    "\xa0",
    "\u2007",
    "\u200b",
    "\ufeff",
    "\u0085",
    "\u1680",
    "\u2028",
    "\u2029",
    "\u202f",
    "\u205f",
    " \u200b\t\ufeff\u3000",
]


@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
@pytest.mark.parametrize(
    ("value", "dangerous"),
    [
        (prefix + formula, True)
        for prefix in FORMULA_PREFIXES
        for formula in ["=1+1", "+SUM(A1)", "-1+2", "@SUM(A1)"]
    ]
    + [(value, True) for value in ["\t", "\r", "\tplain", "\r普通文字", "\t123", "\r123"]]
    + [
        (value, False)
        for value in [
            "普通文字",
            "plain",
            "123",
            " text",
            " \tplain",
            "\nplain",
            "\u3000text",
            "\u200btext",
            "\ufefftext",
            "a=b",
            "text\t=1",
            "'=1+1",
        ]
    ],
)
def test_export_text_prefixes_round_trip_as_strings(fmt, value, dangerous):
    expected = ("'" if dangerous else "") + value
    assert sanitize_export_text(value) == expected
    # Both untrusted column headers and cell values use the sanitizer.
    content = export_change_list([{value: value}], fmt=fmt)
    if fmt == "csv":
        rows = list(csv.reader(StringIO(content.decode("utf-8-sig"), newline="")))
        assert rows == [[expected], [expected]]
    else:
        workbook = openpyxl.load_workbook(BytesIO(content), data_only=False)
        try:
            # XML normalizes carriage returns when openpyxl reopens the workbook.
            for coordinate in ["A1", "A2"]:
                cell = workbook.active[coordinate]
                assert cell.value == expected.replace("\r", "\n")
                assert cell.data_type == "s"
        finally:
            workbook.close()


@pytest.mark.parametrize("prefix", ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x1f"])
def test_sanitize_export_text_handles_whitespace_disallowed_in_xml(prefix):
    assert prefix.isspace()
    for formula in "=+-@":
        value = prefix + formula + "1"
        assert sanitize_export_text(value) == "'" + value
    assert sanitize_export_text(prefix + "plain") == prefix + "plain"
    assert sanitize_export_text("") == ""


@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
@pytest.mark.parametrize("column", ["qty", "quantity", "delta", "reservation_delta", "new_qty"])
@pytest.mark.parametrize(
    ("value", "message"),
    [
        (float("nan"), "必须是有限数值"),
        (float("inf"), "必须是有限数值"),
        (float("-inf"), "必须是有限数值"),
        # Decimal is not a Real on CPython; the existing numeric type guard rejects it.
        (Decimal("NaN"), "数量列"),
    ],
)
def test_export_quantity_rejects_nonfinite_numbers(fmt, column, value, message):
    with pytest.raises(ValueError, match=message):
        _export_value(column, value)
    with pytest.raises(ValueError, match=message):
        export_change_list([{column: value}], fmt=fmt)


@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
@pytest.mark.parametrize("column", ["qty", "quantity", "delta", "reservation_delta", "new_qty"])
@pytest.mark.parametrize("value", [0, 30, -2, 0.0, 1.5, -2.5])
def test_export_quantity_preserves_finite_numbers(fmt, column, value):
    assert _export_value(column, value) is value
    content = export_change_list([{column: value}], fmt=fmt)
    if fmt == "csv":
        rows = list(csv.DictReader(StringIO(content.decode("utf-8-sig"))))
        assert rows == [{column: str(value)}]
    else:
        workbook = openpyxl.load_workbook(BytesIO(content), data_only=False)
        try:
            cell = workbook.active["A2"]
            assert cell.value == value
            assert cell.data_type == "n"
        finally:
            workbook.close()


def _replace_workbook_parts(content, replacements):
    with ZipFile(BytesIO(content)) as source:
        parts = {name: source.read(name) for name in source.namelist()}
    parts.update(replacements)
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as target:
        for name, data in parts.items():
            target.writestr(name, data)
    return output.getvalue()


@pytest.mark.parametrize("kind", ["part-and-type", "part-only", "mixed-case-part", "type-only"])
def test_external_link_workbooks_rejected_by_static_reader_and_upload(stage3, kind):
    content = workbook_bytes()
    replacements = {}
    if kind != "type-only":
        path = (
            "XL/ExternalLinks/externalLink1.xml"
            if kind == "mixed-case-part"
            else "xl/externalLinks/externalLink1.xml"
        )
        replacements[path] = (
            b'<externalLink xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
        )
    if kind in {"part-and-type", "type-only"}:
        with ZipFile(BytesIO(content)) as archive:
            content_types = archive.read("[Content_Types].xml")
        replacements["[Content_Types].xml"] = content_types.replace(
            b"</Types>",
            b'<Override PartName="/xl/externalLinks/externalLink1.xml" '
            b'ContentType="application/vnd.openxmlformats-officedocument.'
            b'spreadsheetml.externalLink+xml"/></Types>',
        )
    content = _replace_workbook_parts(content, replacements)
    with pytest.raises(ValueError, match="外部链接"):
        with static_workbook(content):
            pytest.fail("External links must be rejected before yielding a workbook")

    # TestClient dispatches in process; this does not open a live HTTP connection.
    client, factory, _, app = stage3
    response = upload(client, new_task(factory), "external-links.xlsx", content)
    assert response.status_code == 400
    assert response.json()["parser_status"] == "rejected"
    assert "外部链接" in response.json()["error"]["message"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(InputSource)) == 0
        assert session.scalar(select(func.count()).select_from(SourceSpan)) == 0
    assert list(app.state.source_storage_dir.iterdir()) == []


def test_defusedxml_is_installed_and_enabled_in_openpyxl():
    assert defusedxml.__version__ == "0.7.1"
    assert openpyxl.xml.DEFUSEDXML is True


@pytest.mark.parametrize("entity_kind", ["internal", "nested"])
def test_static_workbook_rejects_xml_entities_with_read_error(entity_kind):
    content = workbook_bytes()
    with ZipFile(BytesIO(content)) as archive:
        workbook_xml = archive.read("xl/workbook.xml")
    entities = b'<!ENTITY payload "entity text">'
    if entity_kind == "nested":
        entities = (
            b'<!ENTITY seed "ha">'
            b'<!ENTITY repeat "&seed;&seed;&seed;&seed;">'
            b'<!ENTITY payload "&repeat;&repeat;&repeat;&repeat;">'
        )
    workbook_xml = (
        b"<!DOCTYPE workbook ["
        + entities
        + b"]>"
        + workbook_xml.replace('name="订单"'.encode(), b'name="&payload;"')
    )
    content = _replace_workbook_parts(content, {"xl/workbook.xml": workbook_xml})
    with pytest.raises(ValueError, match="无法读取 XLSX，请提供有效的静态工作簿") as error:
        with static_workbook(content):
            pytest.fail("XML entity declarations must be rejected before yielding a workbook")
    cause = error.value.__cause__
    while cause is not None and not isinstance(cause, defusedxml.DefusedXmlException):
        cause = cause.__cause__
    assert isinstance(cause, defusedxml.DefusedXmlException)
