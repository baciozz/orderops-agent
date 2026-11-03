"""Read task-scoped source evidence shared by APIs, agents, and task views."""

from schemas.common import SourceRef
from sqlalchemy import select

from app.db.models import SourceSpan, TaskSource
from app.errors import AppError


def task_spans(session, task_id: str, source_id: str | None = None) -> list[SourceRef]:
    query = (
        select(SourceSpan)
        .join(TaskSource, TaskSource.source_id == SourceSpan.source_id)
        .where(TaskSource.task_id == task_id)
        .order_by(SourceSpan.source_id, SourceSpan.sheet, SourceSpan.line_start, SourceSpan.id)
    )
    if source_id is not None:
        if session.get(TaskSource, (task_id, source_id)) is None:
            raise AppError("来源不存在或不属于此任务", code="SOURCE_NOT_FOUND", http_status=404)
        query = query.where(SourceSpan.source_id == source_id)
    return [
        SourceRef(
            **{
                key: getattr(span, key)
                for key in (
                    "source_id",
                    "sheet",
                    "cell_range",
                    "line_start",
                    "line_end",
                    "message_index",
                    "text_excerpt",
                )
            }
        )
        for span in session.scalars(query)
    ]
