"""Bounded static upload readers. File contents never authorize business actions."""

from contextlib import contextmanager
from io import BytesIO
from pathlib import PurePath
from zipfile import BadZipFile, ZipFile

import openpyxl
from defusedxml import DefusedXmlException

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 3
MAX_SHEETS = 5
MAX_NONEMPTY_ROWS = 5000
MAX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_ZIP_MEMBERS = 1000
MAX_CELLS = 100_000
ALLOWED_SUFFIXES = frozenset({".txt", ".md", ".xlsx"})


def validate_upload(filename: str, content: bytes) -> str:
    suffix = PurePath(filename).suffix.lower()
    if suffix == ".xlsm":
        raise ValueError("不接受 XLSM 宏文件，请提供不含宏的静态 XLSX")
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError("不支持的文件类型；仅接受 TXT、MD、XLSX")
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("文件超过 5 MB 上限")
    return suffix


def _check_archive(content: bytes) -> None:
    with ZipFile(BytesIO(content)) as archive:
        members = archive.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise ValueError("XLSX 压缩包成员数超过上限")
        expanded = sum(item.file_size for item in members)
        compressed = sum(item.compress_size for item in members)
        if expanded > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("XLSX 解压后大小超过上限")
        if expanded > MAX_COMPRESSION_RATIO * max(compressed, 1):
            raise ValueError("XLSX 压缩比超过上限")
        if any("vba" in item.filename.lower() for item in members):
            raise ValueError("不接受含 VBA 宏的文件")
        if any("xl/externallinks/" in item.filename.lower() for item in members):
            raise ValueError("不接受含外部链接的 XLSX 文件，请提供静态工作簿")
        content_types = archive.read("[Content_Types].xml").lower()
        if b"macroenabled" in content_types or b"vba" in content_types:
            raise ValueError("不接受含宏的文件类型")
        if b"externallink" in content_types:
            raise ValueError("不接受含外部链接的 XLSX 文件，请提供静态工作簿")


@contextmanager
def static_workbook(content: bytes):
    """Validate every sheet before returning any business rows; never read cached formulas."""
    validate_upload("input.xlsx", content)
    workbook = None
    try:
        _check_archive(content)
        try:
            workbook = openpyxl.load_workbook(
                BytesIO(content), data_only=False, read_only=True, keep_links=False
            )
        except ValueError as exc:
            # openpyxl can wrap defusedxml's security exceptions in ValueError.
            raise ValueError("无法读取 XLSX，请提供有效的静态工作簿") from exc
        if len(workbook.worksheets) > MAX_SHEETS:
            raise ValueError("工作表超过 5 张上限")
        nonempty_rows = 0
        cells = 0
        for sheet in workbook.worksheets:
            # Ignore untrusted worksheet dimensions, which can claim millions of empty rows.
            sheet.reset_dimensions()
            for row in sheet.iter_rows():
                populated = [cell for cell in row if cell.value is not None]
                if not populated:
                    continue
                nonempty_rows += 1
                cells += len(populated)
                if nonempty_rows > MAX_NONEMPTY_ROWS:
                    raise ValueError("非空行超过上限（所有工作表总计 5000 行）")
                if cells > MAX_CELLS:
                    raise ValueError("非空单元格超过上限")
                for cell in populated:
                    if cell.data_type == "f" or (
                        isinstance(cell.value, str) and cell.value.startswith("=")
                    ):
                        raise ValueError(
                            f"{sheet.title}!{cell.coordinate} 含公式，请改为明确静态值"
                        )
    except (BadZipFile, KeyError, OSError, SyntaxError, DefusedXmlException) as exc:
        if workbook is not None:
            workbook.close()
        raise ValueError("无法读取 XLSX，请提供有效的静态工作簿") from exc
    except Exception:
        if workbook is not None:
            workbook.close()
        raise
    try:
        yield workbook
    finally:
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
