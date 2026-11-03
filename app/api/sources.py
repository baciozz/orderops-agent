"""Local, task-scoped source ingestion. Uploads only store evidence."""

from email import policy
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from tempfile import mkdtemp
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from schemas.api import SourceUploadResponse
from schemas.common import SourceRef
from sqlalchemy import func, select

from app.adapters.parser import split_source
from app.adapters.uploads import MAX_FILE_BYTES, MAX_FILES, MAX_NONEMPTY_ROWS, MAX_SHEETS
from app.db.models import InputSource, SourceSpan, Task, TaskSource
from app.db.session import begin_immediate
from app.errors import AppError
from app.observability import enrich, get_logger, observed

logger = get_logger(__name__)

router = APIRouter()
LIMITS = {
    "max_file_bytes": MAX_FILE_BYTES,
    "max_files": MAX_FILES,
    "max_sheets": MAX_SHEETS,
    "max_nonempty_rows": MAX_NONEMPTY_ROWS,
}


async def _upload_form(request: Request) -> tuple[str, str, bytes]:
    # A bounded stdlib MIME reader avoids an optional multipart dependency. One
    # file per request; MAX_FILES also applies across all uploads for this task.
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data;") or any(
        char in content_type for char in "\r\n"
    ):
        raise ValueError("需要 multipart/form-data 上传")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_FILE_BYTES + 64 * 1024:
            raise ValueError("上传请求超过大小上限")
        body.extend(chunk)
    message = BytesParser(policy=policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    if not message.is_multipart() or message.defects:
        raise ValueError("multipart 上传格式不正确")
    fields = {}
    file_parts = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if part.defects or part.get_content_disposition() != "form-data":
            raise ValueError("multipart 字段格式不正确")
        if part.get_filename() is not None:
            file_parts.append(part)
        elif name == "task_id":
            if name in fields:
                raise ValueError("task_id 不能重复")
            fields[name] = (part.get_payload(decode=True) or b"").decode("utf-8")
    if (
        len(file_parts) != 1
        or file_parts[0].get_param("name", header="content-disposition") != "file"
    ):
        raise ValueError("每次请通过 file 字段上传一个文件；每任务最多 3 个文件")
    if not fields.get("task_id"):
        raise ValueError("上传必须提供 task_id")
    file = file_parts[0]
    return fields["task_id"], file.get_filename(), file.get_payload(decode=True) or b""


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


@router.post("/sources", response_model=SourceUploadResponse)
@observed(logger, "upload.received")
async def upload_source(request: Request):
    source_id = f"SRC-{uuid4().hex}"
    response = SourceUploadResponse(
        request_id=request.state.request_id,
        correlation_id=request.state.correlation_id,
        source_id=source_id,
        parser_status="ok",
        limits=LIMITS,
    )
    try:
        task_id, filename, content = await _upload_form(request)
        enrich(
            task_id=task_id,
            source_id=source_id,
            file_size=len(content),
            file_sha256=sha256(content).hexdigest()[:12],
        )
        spans = split_source(source_id, filename, content)
    except ValueError as exc:
        enrich(outcome="failed", error_code="UPLOAD_REJECTED")
        request.state.error_code = "UPLOAD_REJECTED"
        response.parser_status = "rejected"
        return JSONResponse(
            status_code=400,
            content={
                **response.model_dump(),
                "error": {"code": "UPLOAD_REJECTED", "message": str(exc)},
            },
        )
    storage_path = None
    try:
        with request.app.state.session_factory() as session, begin_immediate(session):
            if session.get(Task, task_id) is None:
                raise AppError("任务不存在", code="TASK_NOT_FOUND", http_status=404)
            count = session.scalar(
                select(func.count()).select_from(TaskSource).where(TaskSource.task_id == task_id)
            )
            if count >= MAX_FILES:
                raise AppError("每任务最多 3 个文件", code="UPLOAD_LIMIT", http_status=400)
            # The form task_id is bound at upload time; reads use this explicit
            # join rather than trusting later event payloads or client paths.
            root = request.app.state.source_storage_dir
            if root is None:
                root = Path(mkdtemp(prefix="oos-sources-"))
                request.app.state.source_storage_dir = root
            storage_path = root / source_id
            with storage_path.open("xb") as target:
                target.write(content)
            session.add(
                InputSource(
                    id=source_id,
                    sha256=sha256(content).hexdigest(),
                    media_type={
                        ".xlsx": "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet",
                        ".md": "text/markdown",
                    }.get(Path(filename).suffix.lower(), "text/plain"),
                    original_name=PureWindowsPath(filename).name[:255],
                    storage_ref=str(storage_path),
                    size=len(content),
                    parser_version="static-v1",
                )
            )
            session.flush()
            session.add(TaskSource(task_id=task_id, source_id=source_id))
            session.add_all(SourceSpan(id=f"SPAN-{uuid4().hex}", **span) for span in spans)
    except Exception:
        if storage_path is not None:
            storage_path.unlink(missing_ok=True)
        raise
    return response


@router.get("/sources/{source_id}/spans", response_model=list[SourceRef])
def get_spans(source_id: str, task_id: str, request: Request):
    with request.app.state.session_factory() as session:
        return task_spans(session, task_id, source_id)
