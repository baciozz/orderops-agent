"""Best-effort structured events confined to the oos logging namespace."""

import hashlib
import json
import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from pydantic import SecretStr

from app.config import Settings
from app.errors import AppError

_REDACTED = "[REDACTED]"
_SENSITIVE = re.compile(
    r"(?:^|[_-])(api_key|apikey|authorization|token|secret|password|cookie)$", re.I
)
_CREDENTIAL = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]+|Bearer\s+[^\s,;\"'<>]+", re.I)
_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)


def _string(value: object) -> str:
    try:
        return str(value)
    except Exception:
        return "[UNPRINTABLE]"


class RedactionFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.secrets: set[str] = set()

    def clean(self, value: object, seen: frozenset[int] = frozenset()) -> Any:
        if isinstance(value, SecretStr):
            return _REDACTED
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, _REDACTED)
            return _CREDENTIAL.sub(_REDACTED, value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if id(value) in seen:
            return "[CIRCULAR]"
        seen = seen | {id(value)}
        if isinstance(value, Mapping):
            return {
                self.clean(_string(key)): _REDACTED
                if _SENSITIVE.search(_string(key))
                else self.clean(item, seen)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.clean(item, seen) for item in value]
        return self.clean(_string(value), seen)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Mutate before propagation so root handlers (including caplog) see safe data.
            record.msg = self.clean(record.msg)
            record.args = self.clean(record.args)
            if isinstance(record.args, list):
                record.args = tuple(record.args)
            record.msg = self.clean(record.getMessage())
            record.args = ()
            for key in list(record.__dict__):
                if key not in _STANDARD:
                    record.__dict__[key] = (
                        _REDACTED if _SENSITIVE.search(key) else self.clean(record.__dict__[key])
                    )
            # Exception strings/tracebacks can contain arbitrary customer data.
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            return True
        except Exception:
            return False


_redaction = RedactionFilter()


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if not _redaction.filter(record):
            return json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "level": "ERROR",
                    "logger": "oos",
                    "event": "logging.failed",
                    "message": _REDACTED,
                }
            )
        body = {key: value for key, value in vars(record).items() if key not in _STANDARD}
        body.update(
            ts=datetime.fromtimestamp(record.created, UTC).isoformat(),
            level=record.levelname,
            logger=record.name,
            event=getattr(record, "event", "log.message"),
            message=record.getMessage(),
        )
        return json.dumps(body, ensure_ascii=False, default=_string)


class TextFormatter(JSONFormatter):
    def format(self, record: logging.LogRecord) -> str:
        return f"{record.levelname} {record.name} {super().format(record)}"


class _SafeStreamHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # Logging must never interrupt the business path, even with raiseExceptions=True.
        pass


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger("oos." + name.removeprefix("app."))
    if _redaction not in logger.filters:
        logger.addFilter(_redaction)
    return logger


def configure_logging(settings: Settings) -> None:
    """Idempotent process-wide oos setup; root handlers/levels are untouched."""
    logger = logging.getLogger("oos")
    key = settings.llm_api_key_value()
    if key:
        # Retain prior configured keys too, for apps sharing a process.
        _redaction.secrets.add(key)
    logger.addFilter(_redaction)
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logger.setLevel(level if isinstance(level, int) else logging.INFO)
    logger.propagate = True
    handler = next((h for h in logger.handlers if isinstance(h, _SafeStreamHandler)), None)
    if handler is None:
        handler = _SafeStreamHandler()
        handler.addFilter(_redaction)
        logger.addHandler(handler)
    handler.setFormatter(JSONFormatter() if settings.log_json else TextFormatter())


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    try:
        logger.info(event, extra={**(_context.get() or {}), **fields, "event": event})
    except Exception:
        pass


def exception_code(exc: Exception) -> str:
    return exc.code if isinstance(exc, AppError) else "INTERNAL_ERROR"


def text_digest(text: str) -> dict[str, int | str]:
    return {
        "text_len": len(text),
        "text_sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12],
    }


def enrich(**fields: Any) -> None:
    """Add identifiers already available to the current observation scope."""
    context = _context.get()
    if context is not None:
        context.update(fields)


@contextmanager
def log_context(**fields: Any) -> Iterator[dict[str, Any]]:
    context = {**(_context.get() or {}), **fields}
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


@contextmanager
def observe(
    logger: logging.Logger,
    event: str,
    *,
    failed_event: str | None = None,
    start_event: str | None = None,
    **fields: Any,
) -> Iterator[dict[str, Any]]:
    started = perf_counter()
    with log_context(**fields) as context:
        if start_event:
            log_event(logger, start_event, outcome="started", duration_ms=0.0)
        try:
            yield context
        except Exception as exc:
            log_event(
                logger,
                failed_event or event,
                outcome="failed",
                error_code=exception_code(exc),
                duration_ms=round((perf_counter() - started) * 1000, 3),
            )
            raise
        else:
            log_event(
                logger,
                event,
                outcome=context.get("outcome", "success"),
                duration_ms=round((perf_counter() - started) * 1000, 3),
            )


def task_log_fields(session: Any, task_id: str) -> dict[str, Any]:
    """Best-effort metadata reads in the caller's existing session, without autoflush."""
    try:
        from sqlalchemy import select

        from app.db.models import JobAttempt, ModelUsage, Task

        with session.no_autoflush:
            attempt_id = session.scalar(
                select(JobAttempt.id)
                .where(JobAttempt.task_id == task_id)
                .order_by(JobAttempt.attempt_no.desc())
                .limit(1)
            )
            live_usage = session.scalar(
                select(ModelUsage.id).where(ModelUsage.task_id == task_id).limit(1)
            )
            task = session.get(Task, task_id)
            return {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "mode": "LIVE" if live_usage else "FIXTURE",
                "order_id": task.order_id if task else None,
            }
    except Exception:
        return {"task_id": task_id}


def task_fields_from_factory(factory: Any, task_id: str) -> dict[str, Any]:
    try:
        with factory() as session:
            return task_log_fields(session, task_id)
    except Exception:
        return {"task_id": task_id}
