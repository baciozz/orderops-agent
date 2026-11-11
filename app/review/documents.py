"""Bounded, common source previews. Readers never infer an order or execute a file."""

import csv
import json
import re
import subprocess
import sys
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import Field

from app.adapters.uploads import MAX_FILE_BYTES
from app.errors import AppError
from app.review.contracts import ReviewModel

MAX_ROWS = 1000
MAX_COLUMNS = 64
MAX_CELLS = 20_000
MAX_TEXT = 60_000
MAX_PAGES = 20
MAX_PIXELS = 8_000_000
MAX_SIDE = 6000
SUPPORTED = {".txt", ".md", ".csv", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg"}


class Span(ReviewModel):
    text: str
    locator: str
    page: int | None = None
    sheet: str | None = None
    cell: str | None = None
    box: list[int] | None = None
    confidence: float | None = None
    hidden: bool = False


class Table(ReviewModel):
    table_id: str
    label: str
    rows: list[list[str]]
    locators: list[list[str]]
    hidden_rows: list[int] = Field(default_factory=list)
    merged_ranges: list[str] = Field(default_factory=list)


class OCRSizeConflict(ReviewModel):
    """A review cue, never a replacement for the primary OCR text."""

    box: list[int] = Field(min_length=4, max_length=4)
    primary: str
    alternate: str | None = None
    reason: Literal["disagreement", "uncertain_glyph", "uncertain_mixed"] = "disagreement"


class Document(ReviewModel):
    kind: str
    spans: list[Span]
    tables: list[Table] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    pages: int | None = None
    image_width: int | None = None
    image_height: int | None = None
    rotation: int = 0
    ocr_version: str | None = None
    # Hashes identify language data, not the engine or recognition accuracy.
    # An empty map means the data files could not be identified safely.
    ocr_language_sha256: dict[str, str] = Field(default_factory=dict)
    # None is a legacy record produced before this optional cross-check existed.
    ocr_size_crosscheck: Literal["checked", "unavailable"] | None = None
    ocr_size_conflicts: list[OCRSizeConflict] = Field(default_factory=list)
    # None denotes a legacy image not checked with this rule.
    ocr_cjk_confidence: Literal["clear", "review", "unavailable"] | None = None
    ocr_cjk_low_count: int = 0
    ocr_cjk_low_regions: list[list[int]] = Field(default_factory=list)
    # None denotes a legacy image that has not had numeric-token review cues.
    # "available" is not a claim that all numbers were recognized correctly.
    ocr_numeric_check: Literal["available", "unavailable"] | None = None
    ocr_numeric_low_count: int = 0
    ocr_numeric_low_regions: list[list[int]] = Field(default_factory=list)
    confirmable: bool = True


def invalid(message: str, code: str = "DOCUMENT_INVALID") -> AppError:
    return AppError(message, code=code, http_status=422)


def validate_file(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise invalid(
            "支持TXT/MD、CSV、静态XLSX、文本PDF、PNG/JPEG；XLS/XLSM/语音请先转成受支持形式",
            "FORMAT_UNSUPPORTED",
        )
    if not content or len(content) > MAX_FILE_BYTES:
        raise invalid("文件须非空且不超过5 MiB", "DOCUMENT_SIZE_LIMIT")
    return suffix


def _limits(rows: list[list[str]]) -> None:
    if len(rows) > MAX_ROWS or any(len(row) > MAX_COLUMNS for row in rows):
        raise invalid("表格超过1000行或64列上限，请按订单拆分", "DOCUMENT_SIZE_LIMIT")
    if sum(len(row) for row in rows) > MAX_CELLS:
        raise invalid("表格超过20000单元格上限", "DOCUMENT_SIZE_LIMIT")
    if sum(len(value) for row in rows for value in row) > MAX_TEXT:
        raise invalid("提取文本超过60000字符上限，未截断资料，请拆分", "DOCUMENT_SIZE_LIMIT")


def text_document(content: bytes, *, markdown: bool = False) -> Document:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        raise invalid("文本须为UTF-8；中文CSV可显式选择GB18030编码", "ENCODING_INVALID") from None
    if "\x00" in text or len(text) > MAX_TEXT or len(text.splitlines()) > MAX_ROWS:
        raise invalid("文本含无效字符或超过行数/长度上限", "DOCUMENT_SIZE_LIMIT")
    spans = [
        Span(text=line, locator=f"原文第{i}行")
        for i, line in enumerate(text.splitlines(), 1)
        if line.strip()
    ]
    if not spans:
        raise invalid("没有可读取文字")
    doc = Document(kind="markdown" if markdown else "text", spans=spans)
    lines = text.splitlines()
    # Only explicit tabular layouts are offered for mapping; surrounding notes remain spans.
    tab_rows = [(i, line.split("\t")) for i, line in enumerate(lines, 1) if "\t" in line]
    if tab_rows:
        rows = [row for _, row in tab_rows]
        _limits(rows)
        doc.tables.append(
            Table(
                table_id="T1",
                label="制表符表格",
                rows=rows,
                locators=[
                    [f"原文第{i}行，第{j}列" for j in range(1, len(row) + 1)] for i, row in tab_rows
                ],
            )
        )
    elif markdown or any("|" in line for line in lines):
        for index in range(len(lines) - 1):
            if "|" in lines[index] and re.fullmatch(r"[\s|:\-]+", lines[index + 1] or "x"):
                indexed = [
                    (i + 1, line)
                    for i, line in enumerate(lines)
                    if i >= index and i != index + 1 and "|" in line
                ]
                rows = [
                    [
                        cell.strip().replace("\\|", "|")
                        for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
                    ]
                    for _, line in indexed
                ]
                _limits(rows)
                doc.tables.append(
                    Table(
                        table_id="T1",
                        label="Markdown表格",
                        rows=rows,
                        locators=[
                            [f"原文第{i}行，第{j}列" for j in range(1, len(row) + 1)]
                            for (i, _), row in zip(indexed, rows, strict=True)
                        ],
                    )
                )
                break
    return doc


def csv_document(content: bytes, *, encoding: str = "utf-8-sig") -> Document:
    if encoding not in {"utf-8-sig", "gb18030"}:
        raise invalid("CSV编码仅支持UTF-8/BOM或显式选择GB18030", "ENCODING_INVALID")
    try:
        text = content.decode(encoding)
        if "\x00" in text:
            raise ValueError
        reader = csv.reader(StringIO(text, newline=""), strict=True)
        rows, locators, spans = [], [], []
        previous = 0
        for number, row in enumerate(reader, 1):
            start, end = previous + 1, reader.line_num
            previous = end
            if not any(value.strip() for value in row):
                continue
            where = f"CSV记录{number}（物理行{start}–{end}）"
            rows.append(row)
            locators.append([f"{where}，列{index}" for index in range(1, len(row) + 1)])
            spans.append(Span(text="\t".join(row), locator=where))
            _limits(rows)
    except (UnicodeError, csv.Error, ValueError):
        raise invalid(
            "CSV损坏或编码不符；请选择实际编码，保留款号文本与引号内换行", "ENCODING_INVALID"
        ) from None
    if not rows:
        raise invalid("CSV没有可读取记录")
    return Document(
        kind="csv",
        spans=spans,
        tables=[Table(table_id="T1", label="CSV", rows=rows, locators=locators)],
        warnings=["所有字段按文本读取，保留前导零；公式样式文字不执行。请确认表头与数量语义。"],
    )


def xlsx_document(content: bytes) -> Document:
    from io import BytesIO
    from zipfile import ZipFile

    from defusedxml.ElementTree import fromstring
    from openpyxl.utils.cell import range_boundaries

    from app.adapters.uploads import UploadValidationError, static_workbook

    tables, spans = [], []
    try:
        with static_workbook(content) as book, ZipFile(BytesIO(content)) as archive:
            for sheet_index, sheet in enumerate(book.worksheets, 1):
                xml = fromstring(archive.read(sheet._worksheet_path))
                namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                hidden = {
                    int(row.attrib["r"])
                    for row in xml.iter(f"{namespace}row")
                    if row.attrib.get("hidden") in {"1", "true"}
                }
                merges = [item.attrib["ref"] for item in xml.iter(f"{namespace}mergeCell")]
                if any(
                    range_boundaries(ref)[2] > MAX_COLUMNS or range_boundaries(ref)[3] > MAX_ROWS
                    for ref in merges
                ):
                    raise invalid("合并范围超过支持边界", "DOCUMENT_SIZE_LIMIT")
                sheet.reset_dimensions()
                rows, locators = [], []
                for row_index, cells in enumerate(sheet.iter_rows(), 1):
                    if row_index > MAX_ROWS:
                        raise invalid("工作表超过1000行上限", "DOCUMENT_SIZE_LIMIT")
                    values, refs = [], []
                    for column, cell in enumerate(cells, 1):
                        if column > MAX_COLUMNS:
                            raise invalid("工作表超过64列上限", "DOCUMENT_SIZE_LIMIT")
                        value = cell.value
                        display = "" if value is None else str(value)
                        # Apply only a literal zero-padding format, never a formula/format program.
                        if (
                            type(value) is int
                            and value >= 0
                            and re.fullmatch(r"0{1,30}", cell.number_format)
                        ):
                            display = str(value).zfill(len(cell.number_format))
                        coordinate = (
                            f"{__import__('openpyxl').utils.get_column_letter(column)}{row_index}"
                        )
                        location = f"{sheet.title}!{coordinate}"
                        values.append(display)
                        refs.append(location)
                        if display:
                            spans.append(
                                Span(
                                    text=display,
                                    locator=location,
                                    sheet=sheet.title,
                                    cell=coordinate,
                                    hidden=row_index in hidden,
                                )
                            )
                    rows.append(values)
                    locators.append(refs)
                    _limits(rows)
                # Keep empty rows and merge metadata, so mappings use real worksheet positions.
                tables.append(
                    Table(
                        table_id=f"T{sheet_index}",
                        label=sheet.title,
                        rows=rows,
                        locators=locators,
                        hidden_rows=sorted(hidden),
                        merged_ranges=merges,
                    )
                )
    except UploadValidationError as exc:
        raise invalid(str(exc), exc.code) from None
    if not spans:
        raise invalid("工作簿没有可读取静态值")
    if sum(len(span.text) for span in spans) > MAX_TEXT:
        raise invalid("工作簿总文字超过60000字符上限", "DOCUMENT_SIZE_LIMIT")
    return Document(
        kind="xlsx",
        spans=spans,
        tables=tables,
        warnings=[
            "公式、宏与外链会整文件拒绝，不读取公式缓存；隐藏行保留且标记，不默默略过。",
            "请选择工作表、布局和列映射。合并表头只在预览/映射中明确展开，不修改原件。",
        ],
    )


def read_document(
    filename: str,
    content: bytes,
    *,
    encoding: str = "utf-8-sig",
    rotation: int = 0,
    ocr_data_dir: Path | None = None,
) -> tuple[Document, bytes | None]:
    """Parse untrusted files in a bounded child, without model credentials or network clients."""
    suffix = validate_file(filename, content)
    if rotation not in {0, 90, 180, 270}:
        raise invalid("图片旋转仅支持0/90/180/270度")
    with TemporaryDirectory(prefix="oos-read-") as temporary:
        folder = Path(temporary)
        source, result, preview = folder / "source", folder / "result.json", folder / "preview.jpg"
        source.write_bytes(content)
        command = [
            sys.executable,
            "-m",
            "app.review.reader_worker",
            str(source),
            str(result),
            str(preview),
            suffix,
            encoding,
            str(rotation),
        ]
        if ocr_data_dir is not None:
            command.append(str(ocr_data_dir))
        # The reader has no reason to receive API credentials, proxies or other OOS settings.
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        }
        try:
            # The worker runs bounded language discovery, primary OCR, version
            # lookup, and (for images) a second bounded glyph cross-check.
            # Allow their combined worst-case time plus process/decoding margin.
            completed = subprocess.run(
                command, env=environment, capture_output=True, timeout=55, check=False
            )
            if completed.returncode or not result.exists() or result.stat().st_size > 2_000_000:
                raise invalid(
                    "读取失败或超过资源限制；请拆分或重新导出静态文件", "DOCUMENT_READ_FAILED"
                )
            payload = json.loads(result.read_bytes())
        except subprocess.TimeoutExpired:
            raise invalid("读取超时；请压缩或按订单拆分后重试", "DOCUMENT_TIMEOUT") from None
        except (OSError, ValueError):
            raise invalid("读取未完成，请重新导出文件", "DOCUMENT_READ_FAILED") from None
        if "error" in payload:
            raise invalid(payload["error"]["message"], payload["error"]["code"])
        document = Document.model_validate(payload)
        data = preview.read_bytes() if preview.exists() else None
        if data is not None and len(data) > MAX_FILE_BYTES:
            raise invalid("图片预览超过上限，请缩小图像", "DOCUMENT_SIZE_LIMIT")
        return document, data
