"""SQLite engine with WAL, foreign keys, and an explicit BEGIN IMMEDIATE helper.

Ordinary sessions emit BEGIN (DEFERRED) so a read does not take the reserved
lock. Write paths call ``begin_immediate``, which emits BEGIN IMMEDIATE before
any read in that transaction. SQLite's implicit BEGIN is disabled
(``isolation_level = None``) so SQLAlchemy is the only thing that starts a
transaction; COMMIT and ROLLBACK still go through the driver.
"""

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection as SQLiteConnection

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Task
from app.observability import get_logger, log_event

logger = get_logger(__name__)


class _ObservedSession(Session):
    """Emit terminal task events only after their transaction becomes durable."""


@event.listens_for(_ObservedSession, "after_flush")
def _collect_terminal_tasks(session: Session, flush_context: object) -> None:
    pending = session.info.get("terminal_tasks", {})
    for row in session.new | session.dirty:
        if (
            isinstance(row, Task)
            and row.status in {"SUCCEEDED", "FAILED"}
            and (inspect(row).attrs.status.history.has_changes() or row.id in pending)
        ):
            session.info.setdefault("terminal_tasks", {})[row.id] = {
                "task_id": row.id,
                "order_id": row.order_id,
                "status": row.status,
                "error_code": row.error_code,
            }


@event.listens_for(_ObservedSession, "after_commit")
def _emit_terminal_tasks(session: Session) -> None:
    for fields in session.info.pop("terminal_tasks", {}).values():
        log_event(
            logger,
            "task.finished",
            **fields,
            outcome="success" if fields["status"] == "SUCCEEDED" else "failed",
        )


@event.listens_for(_ObservedSession, "after_rollback")
def _discard_terminal_tasks(session: Session) -> None:
    session.info.pop("terminal_tasks", None)


_BEGIN_IMMEDIATE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "oos_begin_immediate",
    default=False,
)


def make_engine(url: str, *, busy_timeout_ms: int = 5000) -> Engine:
    engine = create_engine(
        url,
        connect_args={"timeout": max(busy_timeout_ms, 1) / 1000},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: SQLiteConnection, connection_record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        cursor.close()
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _on_begin(connection: Connection) -> None:
        statement = "BEGIN IMMEDIATE" if _BEGIN_IMMEDIATE.get() else "BEGIN"
        connection.exec_driver_sql(statement)

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=_ObservedSession, expire_on_commit=False)


@contextmanager
def begin_immediate(session: Session) -> Iterator[Session]:
    """Commit on success, roll back on exception. The session must be idle."""
    if session.in_transaction():
        raise RuntimeError("begin_immediate 要求会话还没有打开事务")
    token = _BEGIN_IMMEDIATE.set(True)
    try:
        with session.begin():
            yield session
    finally:
        _BEGIN_IMMEDIATE.reset(token)


def ensure_sqlite_parent(url: str) -> None:
    if not url.startswith("sqlite:///"):
        return
    path_part = url[len("sqlite:///") :]
    if path_part in ("", ":memory:") or path_part.startswith("file:"):
        return
    path = Path(path_part)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
