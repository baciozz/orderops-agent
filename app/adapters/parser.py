"""Deterministic file-to-span splitting; text is evidence, never instructions."""

import re

from app.adapters.uploads import (
    MAX_SOURCE_SPANS,
    UploadValidationError,
    static_workbook,
    validate_upload,
)
from app.observability import enrich, get_logger, observed, text_digest

logger = get_logger(__name__)


@observed(logger, "source.parsed", fields=lambda source_id, **_: {"source_id": source_id})
def split_source(source_id: str, filename: str, content: bytes) -> list[dict]:
    suffix = validate_upload(filename, content)
    spans: list[dict] = []

    def add(text, **location):
        if len(spans) >= MAX_SOURCE_SPANS:
            raise UploadValidationError(
                f"来源片段超过上限（每文件最多 {MAX_SOURCE_SPANS} 个非空行或单元格），"
                "请拆分文件后上传"
            )
        spans.append(
            {
                "source_id": source_id,
                "sheet": None,
                "cell_range": None,
                "line_start": None,
                "line_end": None,
                "message_index": None,
                # This is an excerpt; the complete original is stored separately.
                "text_excerpt": text[:500],
                **location,
            }
        )

    if suffix in {".txt", ".md"}:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise UploadValidationError("文本文件必须使用 UTF-8 编码", "ENCODING_INVALID") from exc
        if "\x00" in text:
            raise UploadValidationError("文本文件不能包含 NUL 字符", "ENCODING_INVALID")
        # Match splitlines() boundaries lazily: a small upload may contain millions
        # of short lines, so do not allocate a list before applying the span cap.
        lines = re.finditer(
            r"([^\r\n\v\f\x1c-\x1e\x85\u2028\u2029]*)(?:\r\n|[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]|$)",
            text,
        )
        for index, match in enumerate(lines, start=1):
            line = match.group(1)
            if line.strip():
                add(line, line_start=index, line_end=index)
    else:
        with static_workbook(content) as workbook:
            for sheet in workbook.worksheets:
                for row in sheet.iter_rows():
                    for cell in row:
                        if cell.value is not None and str(cell.value).strip():
                            add(str(cell.value), sheet=sheet.title, cell_range=cell.coordinate)
    if not spans:
        raise UploadValidationError("文件没有非空内容", "INPUT_EMPTY")
    enrich(**text_digest("\n".join(span["text_excerpt"] for span in spans)))
    enrich(span_count=len(spans))
    return spans
