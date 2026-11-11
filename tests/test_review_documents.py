"""Actual bytes through bounded readers; no model or renderer sidecar is read."""

from io import BytesIO
from pathlib import Path

import openpyxl
import pytest
from app.errors import AppError
from app.review.documents import csv_document, read_document, text_document
from app.review.mapping import MappingSpec, build_snapshot, mapped_records, suggested_columns
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pypdf import PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas


def book(rows, *, hidden=None, merge=None, extra_sheet=False):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    for row in rows:
        sheet.append(row)
    if hidden:
        sheet.row_dimensions[hidden].hidden = True
    if merge:
        sheet.merge_cells(merge)
    if extra_sheet:
        workbook.create_sheet("说明").append(["这是合成备注，不能视为客户变更"])
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def pdf(pages):
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    stream = BytesIO()
    canvas = Canvas(stream)
    for lines in pages:
        canvas.setFont("STSong-Light", 14)
        for number, line in enumerate(lines):
            canvas.drawString(40, 780 - number * 30, line)
        canvas.showPage()
    canvas.save()
    return stream.getvalue()


def screenshot():
    # Only pixels are supplied to the app; this font/text is not available to its reader.
    font = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    if not font.exists():
        pytest.skip("Synthetic image rendering needs locally installed Noto CJK")
    image = Image.new("RGB", (1500, 300), "white")
    draw = ImageDraw.Draw(image)
    face = ImageFont.truetype(str(font), 34)
    draw.text((35, 25), "模拟资料，不是真实客户记录", font=face, fill="black")
    draw.text((35, 115), "SYN-A 订单 00123 藏蓝 M 总量改为 37 件", font=face, fill="black")
    return image


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gb18030"])
def test_csv_preserves_leading_zeros_quoted_commas_and_newline_locations(encoding):
    raw = '款号,颜色,尺码,数量,备注\r\n00123,藏蓝,M,37,"备注,换行\n继续"\r\n'
    doc, _ = read_document(
        "opaque.csv",
        raw.encode(encoding),
        encoding=("gb18030" if encoding == "gb18030" else "utf-8-sig"),
    )
    assert doc.tables[0].rows[1] == ["00123", "藏蓝", "M", "37", "备注,换行\n继续"]
    assert "物理行2–3" in doc.spans[1].locator


@pytest.mark.parametrize("raw", [b"\xff\xfe\x00", b'a,b\n"unterminated', b"\x00"])
def test_csv_corrupt_or_wrong_encoding_is_not_guessed(raw):
    with pytest.raises(AppError):
        read_document("opaque.csv", raw)


def test_markdown_and_paste_tables_offer_explicit_mapping_and_preserve_notes():
    for raw in [
        "备注：这是合成资料\n款号\t颜色\t尺码\t数量\n00123\t藏蓝\tM\t37",
        "备注：这是合成资料\n|款号|颜色|尺码|数量|\n|---|---|---|---|\n|00123|藏蓝|M|37|",
    ]:
        doc = text_document(raw.encode(), markdown=True)
        assert "备注" in doc.spans[0].text
        assert doc.tables[0].rows[1] == ["00123", "藏蓝", "M", "37"]
        assert suggested_columns(doc.tables[0].rows[0])["style"] == 0


def test_xlsx_hidden_rows_merges_multiple_sheets_and_literal_zero_format():
    raw = book(
        [["款号", "颜色", "尺码", "数量"], [123, "藏蓝", "M", 37], [None, "白", "L", 20]],
        hidden=3,
        merge="A2:A3",
        extra_sheet=True,
    )
    stream = BytesIO(raw)
    workbook = openpyxl.load_workbook(stream)
    workbook["订单"]["A2"].number_format = "00000"
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    doc, _ = read_document("arbitrary.xlsx", output.getvalue())
    assert len(doc.tables) == 2 and doc.tables[0].hidden_rows == [3]
    assert doc.tables[0].rows[1][0] == "00123"
    records = mapped_records(
        doc, MappingSpec(columns=suggested_columns(doc.tables[0].rows[0])), "SOURCE"
    )
    assert records[1]["values"]["style"] == "00123"
    assert "A2:A3" in records[1]["origins"]["style"]["locator"]
    assert any(span.hidden and span.text == "20" for span in doc.spans)


@pytest.mark.parametrize("kind", ["formula", "xlsm", "xls", "corrupt"])
def test_unsafe_or_unsupported_workbooks_never_become_zero_quantities(kind):
    name = "arbitrary." + (kind if kind in {"xls", "xlsm"} else "xlsx")
    raw = book([["数量"], ["=1+1"]]) if kind == "formula" else b"not a workbook"
    with pytest.raises(AppError):
        read_document(name, raw)


@pytest.mark.parametrize("layout", ["rows", "sizes", "matrix"])
def test_three_xlsx_layouts_normalize_to_same_business_lines(layout):
    if layout == "rows":
        rows = [
            ["款号", "颜色", "尺码", "数量"],
            ["00123", "藏蓝", "M", 37],
            ["00123", "藏蓝", "L", 20],
        ]
        spec = MappingSpec(columns={"style": 0, "color": 1, "size": 2, "ordered_qty": 3})
    elif layout == "sizes":
        rows = [["款号", "颜色", "M", "L"], ["00123", "藏蓝", 37, 20]]
        spec = MappingSpec(layout=layout, columns={"style": 0, "color": 1}, quantity_columns=[2, 3])
    else:
        rows = [["颜色", "M", "L"], ["藏蓝", 37, 20]]
        spec = MappingSpec(
            layout=layout,
            columns={"color": 0},
            quantity_columns=[1, 2],
            constants={"style": "00123"},
        )
    spec.constants.update(order_id="SYN-A", revision="1", fulfillment_mode="STOCK", shipped_qty="0")
    doc, _ = read_document("opaque.xlsx", book(rows))
    records = mapped_records(doc, spec, "SOURCE")
    snapshot, origins = build_snapshot(
        [{"confirmed": True, "role": "orders", "records": records}],
        captured_at="2026-11-09",
        synthetic=True,
    )
    assert [(sku.style, sku.color, sku.size) for sku in snapshot.catalog] == [
        ("00123", "藏蓝", "M"),
        ("00123", "藏蓝", "L"),
    ]
    assert [line.ordered_qty for line in snapshot.orders[0].lines] == [37, 20]
    assert all(key in origins for key in ["SYN-A/" + sku.sku_id for sku in snapshot.catalog])


def test_missing_shipped_or_inventory_remains_unknown_and_blank_size_not_zero():
    doc = csv_document("款号,颜色,尺码,数量\n00123,藏蓝,M,37\n".encode())
    spec = MappingSpec(
        columns=suggested_columns(doc.tables[0].rows[0]),
        constants={"order_id": "SYN-A", "revision": "1", "fulfillment_mode": "STOCK"},
    )
    snapshot, _ = build_snapshot(
        [{"confirmed": True, "role": "orders", "records": mapped_records(doc, spec, "S")}],
        captured_at="declared-time",
        synthetic=True,
    )
    assert snapshot.orders[0].lines[0].shipped_qty is None and snapshot.inventory == []
    spec = MappingSpec(
        layout="sizes",
        columns={"color": 0},
        quantity_columns=[1, 2],
        constants={
            "style": "00123",
            "order_id": "SYN-A",
            "revision": "1",
            "fulfillment_mode": "STOCK",
        },
    )
    doc = csv_document("颜色,M,L\n藏蓝,37,\n".encode())
    with pytest.raises(AppError):
        build_snapshot(
            [{"confirmed": True, "role": "orders", "records": mapped_records(doc, spec, "S")}],
            captured_at="time",
            synthetic=True,
        )


def test_pdf_reads_actual_text_layer_and_preserves_page_without_fake_coordinates():
    raw = pdf(
        [["模拟订单 SYN-A 00123 藏蓝 M 总量37件"], ["重复表头：订单 款号 数量", "备注：其他行不变"]]
    )
    doc, _ = read_document("opaque.pdf", raw)
    assert doc.pages == 2 and doc.confirmable
    assert any("37" in span.text and span.page == 1 for span in doc.spans)
    assert any("备注" in span.text and span.page == 2 for span in doc.spans)
    assert all(span.box is None for span in doc.spans)


def test_scan_blank_encrypted_and_too_many_page_pdfs_fail_closed():
    doc, _ = read_document("opaque.pdf", pdf([[]]))
    assert not doc.confirmable and not doc.spans and doc.warnings
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("synthetic-password")
    stream = BytesIO()
    writer.write(stream)
    with pytest.raises(AppError):
        read_document("opaque.pdf", stream.getvalue())
    with pytest.raises(AppError):
        read_document("opaque.pdf", pdf([["合成页"]] * 21))


@pytest.mark.parametrize("variant", ["png", "jpeg", "rotated", "blurred", "cropped"])
def test_actual_ocr_pixels_with_explicit_human_confirmation_boundary(request, variant):
    configured = request.config.getoption("--ocr-data-dir")
    if not configured:
        pytest.skip("Actual Chinese OCR profile requires --ocr-data-dir; no Mock replacement")
    image = screenshot()
    rotation = 0
    if variant == "rotated":
        image = image.rotate(-90, expand=True)
        rotation = 90
    elif variant == "blurred":
        image = image.filter(ImageFilter.GaussianBlur(3))
    elif variant == "cropped":
        image = image.crop((350, 80, 1000, 230))
    stream = BytesIO()
    image.save(stream, format="JPEG" if variant == "jpeg" else "PNG", quality=75)
    doc, preview = read_document(
        "opaque.jpg" if variant == "jpeg" else "opaque.png",
        stream.getvalue(),
        rotation=rotation,
        ocr_data_dir=Path(configured),
    )
    assert doc.kind == "image" and preview and doc.ocr_version and doc.spans
    assert all(span.box and len(span.box) == 4 for span in doc.spans)
    assert any("核对" in warning for warning in doc.warnings)
    if variant in {"png", "jpeg", "rotated"}:
        assert "37" in " ".join(span.text for span in doc.spans)


def test_reader_child_receives_no_model_credentials(monkeypatch):
    from app.review import documents

    real = documents.subprocess.run
    seen = []

    def audited(*args, **kwargs):
        seen.append(kwargs["env"])
        return real(*args, **kwargs)

    monkeypatch.setenv("OOS_LLM_API_KEY", "synthetic-not-a-key")
    monkeypatch.setattr(documents.subprocess, "run", audited)
    doc, _ = read_document("opaque.txt", "合成要求".encode())
    assert doc.spans and "OOS_LLM_API_KEY" not in seen[0] and "HTTPS_PROXY" not in seen[0]
