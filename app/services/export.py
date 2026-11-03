"""Safe CSV/XLSX downloads of change-list rows."""

import csv
import math
from io import BytesIO, StringIO
from numbers import Real

import openpyxl


def sanitize_export_text(value: str) -> str:
    # Spreadsheet importers can ignore leading whitespace/control characters.
    if value.startswith(("\t", "\r")):
        return "'" + value
    for char in value:
        if char.isspace() or char in "\u200b\ufeff":
            continue
        if char in "=+-@":
            return "'" + value
        break
    return value


def _export_value(column: str, value: object) -> object:
    if value is None:
        return None
    if column in {"qty", "quantity", "delta", "reservation_delta"} or column.endswith("_qty"):
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"数量列 {column} 必须是已校验数值")
        if not math.isfinite(value):
            raise ValueError(f"数量列 {column} 必须是有限数值")
        return value
    return sanitize_export_text(str(value))


def export_change_list(rows: list[dict], *, fmt: str = "csv") -> bytes:
    if fmt not in {"csv", "xlsx"}:
        raise ValueError("不支持的导出格式；仅接受 CSV、XLSX")
    columns = list(dict.fromkeys(key for row in rows for key in row))
    headers = [sanitize_export_text(key) for key in columns]
    values = [[_export_value(key, row.get(key)) for key in columns] for row in rows]
    if fmt == "csv":
        output = StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(values)
        return output.getvalue().encode("utf-8-sig")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "变更清单"
    for row in [headers, *values]:
        sheet.append(row)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()
