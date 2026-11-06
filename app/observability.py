"""Best-effort structured events confined to the oos logging namespace."""

import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from inspect import iscoroutinefunction, signature
from time import perf_counter
from typing import Any, TypeVar, cast

from pydantic import SecretStr

from app.config import Settings
from app.errors import AppError
from app.metrics import record_event

_REDACTED = "[REDACTED]"
_SENSITIVE = re.compile(
    r"(?:^|[_-])(api_key|apikey|authorization|token|secret|password|cookie)$", re.I
)
_CREDENTIAL = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}|(?i:Bearer)\s+[^\s,;\"'<>]+")
_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)
_F = TypeVar("_F", bound=Callable[..., Any])


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


class _DropHTTPDetails(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


_drop_http_details = _DropHTTPDetails()
# Names used by pinned httpx/httpcore (sync and async transports). Pre-create
# their loggers so later imports cannot emit headers/URLs through root handlers.
_HTTP_LOGGERS = {
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
}


def protect_http_logging() -> None:
    """Suppress transport metadata; retain our structured model events instead."""
    existing = set(logging.Logger.manager.loggerDict)
    names = _HTTP_LOGGERS | {name for name in existing if name.startswith(("httpx.", "httpcore."))}
    for name in names:
        logger = logging.getLogger(name)
        logger.addFilter(_drop_http_details)
        # Also prevent future descendants that inherit their namespace's level.
        logger.setLevel(logging.CRITICAL + 1)


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
    protect_http_logging()
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
    metadata = {**(_context.get() or {}), **fields}
    try:
        record_event(event, metadata)
    except Exception:
        pass
    try:
        logger.info(event, extra={**metadata, "event": event})
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
def log_context(*, exclude: tuple[str, ...] = (), **fields: Any) -> Iterator[dict[str, Any]]:
    context = {**(_context.get() or {}), **fields}
    for key in exclude:
        context.pop(key, None)
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
    exclude: tuple[str, ...] = (),
    **fields: Any,
) -> Iterator[dict[str, Any]]:
    started = perf_counter()
    with log_context(exclude=exclude, **fields) as context:
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


def observed(
    logger: logging.Logger,
    event: str,
    *,
    fields: Callable[..., Mapping[str, Any]] | None = None,
    failed_event: str | None = None,
    start_event: str | None = None,
    exclude: tuple[str, ...] = (),
) -> Callable[[_F], _F]:
    """Observe sync/async calls; field callbacks receive bound arguments with defaults."""

    def decorate(function: _F) -> _F:
        call_signature = signature(function)

        def scope(
            args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> AbstractContextManager[dict[str, Any]]:
            metadata: Mapping[str, Any] = {}
            if fields is not None:
                try:
                    bound = call_signature.bind(*args, **kwargs)
                    bound.apply_defaults()
                    metadata = fields(**bound.arguments)
                except Exception:
                    # Metadata collection must not prevent the business call.
                    pass
            return observe(
                logger,
                event,
                failed_event=failed_event,
                start_event=start_event,
                exclude=exclude,
                **metadata,
            )

        if iscoroutinefunction(function):

            @wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with scope(args, kwargs):
                    return await function(*args, **kwargs)

            return cast(_F, async_wrapper)

        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with scope(args, kwargs):
                return function(*args, **kwargs)

        return cast(_F, wrapper)

    return decorate


def task_log_fields(session: Any, task_id: str) -> dict[str, Any]:
    """Best-effort metadata reads in the caller's existing session, without autoflush."""
    try:
        from sqlalchemy import select

        from app.db.models import JobAttempt, Task

        with session.no_autoflush:
            attempt_id = session.scalar(
                select(JobAttempt.id)
                .where(JobAttempt.task_id == task_id)
                .order_by(JobAttempt.attempt_no.desc())
                .limit(1)
            )
            task = session.get(Task, task_id)
            return {
                "task_id": task_id,
                "attempt_id": attempt_id,
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
