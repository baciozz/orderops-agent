"""Deterministic file-to-span splitting; text is evidence, never instructions."""

from app.adapters.uploads import static_workbook, validate_upload
from app.observability import enrich, get_logger, observed, text_digest

logger = get_logger(__name__)


@observed(logger, "source.parsed", fields=lambda source_id, **_: {"source_id": source_id})
def split_source(source_id: str, filename: str, content: bytes) -> list[dict]:
    suffix = validate_upload(filename, content)
    spans = []

    def add(text, **location):
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
            raise ValueError("文本文件必须使用 UTF-8 编码") from exc
        for index, line in enumerate(text.splitlines(), start=1):
            if line.strip():
                add(line, line_start=index, line_end=index)
    else:
        with static_workbook(content) as workbook:
            for sheet in workbook.worksheets:
                for row in sheet.iter_rows():
                    for cell in row:
                        if cell.value is not None and str(cell.value).strip():
                            add(str(cell.value), sheet=sheet.title, cell_range=cell.coordinate)
    enrich(**text_digest("\n".join(span["text_excerpt"] for span in spans)))
    enrich(span_count=len(spans))
    return spans
