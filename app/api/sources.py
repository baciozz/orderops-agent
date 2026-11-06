"""Local, task-scoped source ingestion. Uploads only store evidence."""

from email import policy
from email.message import Message
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from re import fullmatch
from tempfile import mkdtemp
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from schemas.api import SourceUploadResponse
from schemas.common import SourceRef
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.parser import split_source
from app.adapters.uploads import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_NONEMPTY_ROWS,
    MAX_REQUEST_BYTES,
    MAX_SHEETS,
    MAX_SOURCE_SPANS,
    MAX_WORKSHEET_COLUMNS,
    MAX_WORKSHEET_ROWS,
    UploadValidationError,
)
from app.api.errors import error_payload
from app.db.models import InputSource, SourceSpan, SourceUploadReceipt, Task, TaskSource
from app.db.session import begin_immediate
from app.errors import AppError
from app.observability import enrich, get_logger, log_event, observed
from app.services.source_files import original_content
from app.services.source_spans import task_spans

logger = get_logger(__name__)

router = APIRouter()
LIMITS = {
    "max_file_bytes": MAX_FILE_BYTES,
    "max_files": MAX_FILES,
    "max_sheets": MAX_SHEETS,
    "max_nonempty_rows": MAX_NONEMPTY_ROWS,
    "max_source_spans": MAX_SOURCE_SPANS,
    "max_worksheet_rows": MAX_WORKSHEET_ROWS,
    "max_worksheet_columns": MAX_WORKSHEET_COLUMNS,
}


def _payload_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is None:
        return b""
    if not isinstance(payload, bytes):
        raise ValueError("multipart 字段内容格式不正确，必须为字节数据")
    return payload


async def _upload_form(request: Request) -> tuple[str, str, bytes, str | None, str]:
    # A bounded stdlib MIME reader avoids an optional multipart dependency. One
    # file per request; MAX_FILES also applies across all uploads for this task.
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data;") or any(
        char in content_type for char in "\r\n"
    ):
        raise ValueError("需要 multipart/form-data 上传")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            raise UploadValidationError("上传请求超过大小上限", "UPLOAD_TOO_LARGE")
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
        filename = part.get_filename()
        if name == "file" and (filename is None or not filename.strip()):
            raise ValueError("上传文件必须带文件名")
        if filename is not None:
            file_parts.append(part)
        elif name in {"task_id", "client_upload_key"}:
            if name in fields:
                raise ValueError(f"{name} 不能重复")
            fields[name] = _payload_bytes(part).decode("utf-8")
    if (
        len(file_parts) != 1
        or file_parts[0].get_param("name", header="content-disposition") != "file"
    ):
        raise ValueError("每次请通过 file 字段上传一个文件；每任务最多 3 个文件")
    if not fields.get("task_id"):
        raise ValueError("上传必须提供 task_id")
    file = file_parts[0]
    filename = file.get_filename()
    if filename is None or not filename.strip():
        raise ValueError("上传文件必须带文件名")
    key = fields.get("client_upload_key")
    if key is not None and fullmatch(r"[A-Za-z0-9._:-]{1,128}", key) is None:
        raise ValueError("client_upload_key 必须为 1–128 个 ASCII 字母、数字或 . _ : -")
    # get_filename() strips whitespace. Retain that legacy parser/display
    # behavior, but bind retries to the complete decoded parameter instead.
    identity_name = file.get_param("filename", header="content-disposition")
    if identity_name is None:
        identity_name = file.get_param("name", header="content-type")
    if not isinstance(identity_name, str):
        raise ValueError("上传文件名格式不正确")
    return fields["task_id"], filename, _payload_bytes(file), key, identity_name


def _upload_fingerprint(filename: str, content: bytes) -> str:
    # Preserve the complete decoded multipart filename, not the sanitized display
    # name. Length-prefixing makes the name/content boundary unambiguous.
    name = filename.encode("utf-8")
    digest = sha256(b"source-upload-v1\0")
    digest.update(len(name).to_bytes(8, "big"))
    digest.update(name)
    digest.update(content)
    return digest.hexdigest()


def _replay_source(session: Session, task_id: str, key: str, fingerprint: str) -> str | None:
    receipt = session.get(SourceUploadReceipt, (task_id, key))
    if receipt is None:
        return None
    # The composite FK enforces ownership on writes; the join also checks it on
    # reads rather than trusting a receipt in isolation.
    source_id = session.scalar(
        select(TaskSource.source_id)
        .join(InputSource, InputSource.id == TaskSource.source_id)
        .where(TaskSource.task_id == task_id, TaskSource.source_id == receipt.source_id)
    )
    if source_id is None:
        raise AppError("上传回执来源不可用", code="UPLOAD_RECEIPT_INVALID", http_status=409)
    if receipt.payload_sha256 != fingerprint:
        raise AppError(
            "client_upload_key 已用于不同文件，请为新的上传使用不同的 key",
            code="UPLOAD_KEY_CONFLICT",
            http_status=409,
        )
    source = session.get(InputSource, source_id)
    assert source is not None  # Checked by the ownership join above.
    original_content(source)
    enrich(source_id=source_id, upload_replayed=True)
    return source_id


def _cleanup_failed_upload(request: Request, source_id: str, created_path: Path) -> None:
    # The transaction may have committed before an after_commit callback failed.
    # Only delete our newly created file if a fresh read confirms no durable row.
    # Failure to prove rollback is safer as an orphan than destroyed evidence.
    try:
        with request.app.state.session_factory() as session:
            if session.get(InputSource, source_id) is not None:
                return
        created_path.unlink(missing_ok=True)
    except Exception:
        # Never replace the original error or log filenames, bytes or paths.
        log_event(
            logger,
            "upload.cleanup_deferred",
            source_id=source_id,
            outcome="deferred",
            error_code="UPLOAD_CLEANUP_UNCERTAIN",
        )


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
        task_id, filename, content, key, identity_name = await _upload_form(request)
        enrich(task_id=task_id)
        fingerprint = _upload_fingerprint(identity_name, content)
        if key is not None:
            with request.app.state.session_factory() as session:
                replayed = _replay_source(session, task_id, key, fingerprint)
            if replayed is not None:
                response.source_id = replayed
                return response
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
        code = getattr(exc, "code", "UPLOAD_REJECTED")
        error = AppError(
            str(exc), code=code, http_status=400, details=getattr(exc, "details", None)
        )
        return JSONResponse(
            status_code=error.http_status,
            content={
                **response.model_dump(),
                **error_payload(
                    error,
                    request_id=request.state.request_id,
                    correlation_id=request.state.correlation_id,
                ),
            },
        )
    created_path = None
    try:
        with request.app.state.session_factory() as session, begin_immediate(session):
            # Parsing stays outside SQLite's write lock. Another request may
            # have won while we parsed, so recheck before caps or file creation.
            if key is not None:
                replayed = _replay_source(session, task_id, key, fingerprint)
                if replayed is not None:
                    response.source_id = replayed
                    return response
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
                created_path = storage_path
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
            if key is not None:
                session.flush()  # Composite ownership FK must already exist.
                session.add(
                    SourceUploadReceipt(
                        task_id=task_id,
                        client_upload_key=key,
                        source_id=source_id,
                        payload_sha256=fingerprint,
                    )
                )
    except Exception:
        if created_path is not None:
            _cleanup_failed_upload(request, source_id, created_path)
        raise
    return response


@router.get("/sources/{source_id}/spans", response_model=list[SourceRef])
def get_spans(source_id: str, task_id: str, request: Request):
    with request.app.state.session_factory() as session:
        return task_spans(session, task_id, source_id)
