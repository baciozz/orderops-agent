"""Bounded static upload readers. File contents never authorize business actions."""

import logging
from contextlib import contextmanager
from io import BytesIO
from pathlib import PurePath
from uuid import uuid4
from xml.parsers.expat import ExpatError
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError

import openpyxl
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import iterparse
from openpyxl.utils.cell import coordinate_to_tuple
from openpyxl.utils.exceptions import InvalidFileException

from app.errors import AppError

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_FILE_BYTES + 64 * 1024
MAX_FILES = 3
MAX_SHEETS = 5
MAX_NONEMPTY_ROWS = 5000
MAX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_ZIP_MEMBERS = 1000
MAX_CELLS = 100_000
MAX_SOURCE_SPANS = 10_000
MAX_WORKSHEET_ROWS = 10_000
MAX_WORKSHEET_COLUMNS = 256
ALLOWED_SUFFIXES = frozenset({".txt", ".md", ".xlsx"})
logger = logging.getLogger(__name__)


class UploadValidationError(ValueError):
    """A rejected file, also usable by non-HTTP static readers."""

    def __init__(
        self, message: str, code: str = "UPLOAD_REJECTED", *, error_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = {"error_id": error_id} if error_id else {}


def validate_text(text: str | None, *, allow_empty: bool = False) -> None:
    """Validate message text before opening tasks or invalidating existing intent."""
    if text is not None:
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise AppError("文本必须是有效 UTF-8", code="ENCODING_INVALID") from exc
        if "\x00" in text:
            raise AppError("文本不能包含 NUL 字符", code="ENCODING_INVALID")
        if size > MAX_FILE_BYTES:
            raise AppError("文本超过大小上限", code="UPLOAD_TOO_LARGE", http_status=413)
    if not allow_empty and (text is None or not text.strip()):
        raise AppError("输入不能为空", code="INPUT_EMPTY")


def validate_upload(filename: str, content: bytes) -> str:
    suffix = PurePath(filename).suffix.lower()
    if suffix == ".xlsm":
        raise ValueError("不接受 XLSM 宏文件，请提供不含宏的静态 XLSX")
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError("不支持的文件类型；仅接受 TXT、MD、XLSX")
    if len(content) > MAX_FILE_BYTES:
        raise UploadValidationError("文件超过 5 MB 上限", "UPLOAD_TOO_LARGE")
    if not content:
        raise UploadValidationError("文件不能为空", "INPUT_EMPTY")
    return suffix


def _check_archive(content: bytes) -> None:
    with ZipFile(BytesIO(content)) as archive:
        members = archive.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise UploadValidationError("XLSX 压缩包成员数超过上限")
        expanded = sum(item.file_size for item in members)
        compressed = sum(item.compress_size for item in members)
        if expanded > MAX_UNCOMPRESSED_BYTES:
            raise UploadValidationError("XLSX 解压后大小超过上限")
        if expanded > MAX_COMPRESSION_RATIO * max(compressed, 1):
            raise UploadValidationError("XLSX 压缩比超过上限")
        if any("vba" in item.filename.lower() for item in members):
            raise UploadValidationError("不接受含 VBA 宏的文件")
        if any("xl/externallinks/" in item.filename.lower() for item in members):
            raise UploadValidationError("不接受含外部链接的 XLSX 文件，请提供静态工作簿")
        content_types = archive.read("[Content_Types].xml").lower()
        if b"macroenabled" in content_types or b"vba" in content_types:
            raise UploadValidationError("不接受含宏的文件类型")
        if b"externallink" in content_types:
            raise UploadValidationError("不接受含外部链接的 XLSX 文件，请提供静态工作簿")


def _check_sheet_layout(sheet) -> None:
    """Bound actual coordinates before openpyxl expands sparse rows/cells.

    Dimensions are untrusted and deliberately ignored. Use the pinned openpyxl
    read-only source accessor so relationship targets cannot bypass this check.
    """
    row_index = column_index = row_count = cell_count = 0
    depth = 0
    row_depth = None
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with sheet._get_source() as source:
        for event, element in iterparse(source, events=("start", "end")):
            if event == "end":
                if depth == row_depth:
                    row_depth = None
                depth -= 1
                element.clear()
                continue
            depth += 1
            if element.tag == f"{namespace}row":
                if row_depth is not None:
                    raise UploadValidationError(
                        "XLSX 行结构无效：工作表行不能嵌套，请重新导出静态工作簿", "UPLOAD_CORRUPT"
                    )
                row_depth = depth
                row_count += 1
                next_row_index = int(element.get("r", row_index + 1))
                if next_row_index <= row_index:
                    raise UploadValidationError(
                        "XLSX 行位置重复或逆序，请重新导出静态工作簿", "UPLOAD_CORRUPT"
                    )
                row_index = next_row_index
                column_index = cell_count = 0
                if not 1 <= row_index <= MAX_WORKSHEET_ROWS or row_count > MAX_WORKSHEET_ROWS:
                    raise UploadValidationError(
                        f"XLSX 实际行位置超过上限（每张表最多 {MAX_WORKSHEET_ROWS} 行），"
                        "请删除多余空行并将数据移至表格顶部"
                    )
            elif row_depth is not None and depth == row_depth + 1:
                # openpyxl treats every direct row child as a cell, regardless of tag.
                cell_count += 1
                coordinate = element.get("r")
                cell_row = row_index
                next_column_index = column_index + 1
                if coordinate:
                    cell_row, next_column_index = coordinate_to_tuple(coordinate)
                if not 1 <= cell_row <= MAX_WORKSHEET_ROWS:
                    raise UploadValidationError("XLSX 单元格行位置超过上限")
                if cell_row != row_index:
                    raise UploadValidationError(
                        "XLSX 单元格与所属行位置不一致，请重新导出静态工作簿", "UPLOAD_CORRUPT"
                    )
                if next_column_index <= column_index:
                    raise UploadValidationError(
                        "XLSX 单元格列位置重复或逆序，请重新导出静态工作簿", "UPLOAD_CORRUPT"
                    )
                column_index = next_column_index
                if not 1 <= column_index <= MAX_WORKSHEET_COLUMNS or (
                    cell_count > MAX_WORKSHEET_COLUMNS
                ):
                    raise UploadValidationError(
                        f"XLSX 实际列位置超过上限（每张表最多 {MAX_WORKSHEET_COLUMNS} 列），"
                        "请删除多余空列并将数据移至表格左侧"
                    )


@contextmanager
def static_workbook(content: bytes):
    """Validate every sheet before returning any business rows; never read cached formulas."""
    validate_upload("input.xlsx", content)
    workbook = None
    try:
        try:
            _check_archive(content)
            workbook = openpyxl.load_workbook(
                BytesIO(content), data_only=False, read_only=True, keep_links=False
            )
            if not workbook.worksheets:
                raise UploadValidationError("XLSX 不含工作表", "UPLOAD_CORRUPT")
            if len(workbook.worksheets) > MAX_SHEETS:
                raise UploadValidationError("工作表超过 5 张上限")
            nonempty_rows = 0
            cells = 0
            for sheet in workbook.worksheets:
                _check_sheet_layout(sheet)
                # Ignore untrusted worksheet dimensions, which can claim millions of empty rows.
                sheet.reset_dimensions()
                for row in sheet.iter_rows():
                    populated = [cell for cell in row if cell.value is not None]
                    if not populated:
                        continue
                    nonempty_rows += 1
                    cells += len(populated)
                    if nonempty_rows > MAX_NONEMPTY_ROWS:
                        raise UploadValidationError("非空行超过上限（所有工作表总计 5000 行）")
                    if cells > MAX_CELLS:
                        raise UploadValidationError("非空单元格超过上限")
                    for cell in populated:
                        if cell.data_type == "f" or (
                            isinstance(cell.value, str) and cell.value.startswith("=")
                        ):
                            raise UploadValidationError(
                                f"{sheet.title}!{cell.coordinate} 含公式，请改为明确静态值"
                            )
        except UploadValidationError:
            raise
        except (
            BadZipFile,
            KeyError,
            SyntaxError,
            ExpatError,
            DefusedXmlException,
            InvalidFileException,
            EOFError,
            ZlibError,
            ValueError,  # openpyxl also wraps defusedxml exceptions in ValueError.
        ) as exc:
            raise UploadValidationError(
                "无法读取 XLSX，请提供有效的静态工作簿", "UPLOAD_CORRUPT"
            ) from exc
        except Exception as exc:
            error_id = f"ERR-{uuid4().hex}"
            # Exception messages/chains may contain cell values. Keep the stack frames
            # without locals, but replace the message and discard the exception chain.
            safe_error = RuntimeError(f"XLSX parser raised {type(exc).__name__} (message redacted)")
            logger.exception(
                "XLSX parse failed error_id=%s suffix=.xlsx size_bytes=%d",
                error_id,
                len(content),
                exc_info=(type(safe_error), safe_error, exc.__traceback__),
            )
            raise UploadValidationError(
                "XLSX 解析失败，请提供错误编号联系维护人员",
                "UPLOAD_PARSE_FAILED",
                error_id=error_id,
            ) from exc
        yield workbook
    finally:
        if workbook is not None:
            workbook.close()


def read_order_sheet(content: bytes) -> list[dict[str, object]]:
    """Read 客户/款/颜色/尺码/数量 and retain the Stage 1A source coordinates."""
    with static_workbook(content) as workbook:
        sheet = workbook.worksheets[0]
        rows: list[dict[str, object]] = []
        for index, row in enumerate(sheet.iter_rows(min_row=2, max_col=5), start=2):
            if all(cell.value is None for cell in row):
                continue
            rows.append(
                {
                    "customer_name": row[0].value,
                    "style": row[1].value,
                    "color": row[2].value,
                    "size": row[3].value,
                    "qty": int(row[4].value),
                    "source_ref": {
                        "source_id": "SRC-XLSX",
                        "sheet": sheet.title,
                        "cell_range": f"A{index}:E{index}",
                    },
                }
            )
        return rows
