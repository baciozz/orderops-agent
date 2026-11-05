"""Apply Alembic migrations. Request handlers do not create tables."""

from pathlib import Path
from sqlite3 import Error as SQLiteError

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.db import models as _models  # noqa: F401
from app.db.base import Base

_ROOT = Path(__file__).resolve().parents[2]


def _migration_config() -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    return cfg


def schema_ready(engine: Engine) -> bool:
    """Reject outdated/partial schemas; a completely empty health server is allowed.

    The caller must avoid opening a connection to an absent SQLite file. This
    function only inspects existing tables and required mapped column names; it
    never runs migrations. Types, constraints, indexes, and data are not validated.
    """
    # Migration files are trusted release inputs, not database-controlled values.
    # Keep configuration/programming failures outside the database error boundary.
    scripts = ScriptDirectory.from_config(_migration_config())
    head = scripts.get_current_head()
    revisions = {revision.revision for revision in scripts.walk_revisions()}
    if head is None or head not in revisions:
        raise RuntimeError("应用迁移配置没有有效的最新版本，请检查发布文件")
    metadata_error = (
        "数据库迁移版本信息缺失、不一致或无法读取，请检查数据库访问、结构与迁移历史；"
        "确认迁移适用后才显式运行 python -m app.cli init-db，必要时从有效备份恢复"
    )
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            if not tables:
                return False
            missing = set(Base.metadata.tables) - tables
            if missing:
                metadata_error += "；数据库还缺少必需表，迁移不一定能修复结构损坏"
            try:
                current = MigrationContext.configure(connection).get_current_heads()
            except (SQLAlchemyError, SQLiteError):
                raise RuntimeError(metadata_error) from None
            # Exact membership only: do not resolve database values as Alembic
            # prefixes/branches, deduplicate rows, or include values in diagnostics.
            known_single = (
                len(current) == 1 and isinstance(current[0], str) and current[0] in revisions
            )
            if not known_single:
                raise RuntimeError(metadata_error)
            if current != (head,):
                message = (
                    f"数据库 schema 未迁移到最新版本（需要 {head}），"
                    "请先检查数据库结构与迁移历史，再显式运行 python -m app.cli init-db"
                )
                if missing:
                    message += "；存在缺少的必需表，迁移不一定能修复结构损坏，必要时从有效备份恢复"
                raise RuntimeError(message)
            if missing:
                raise RuntimeError(
                    "数据库 schema 缺少必需表，请检查数据库结构并从有效备份恢复；"
                    "迁移仍须显式运行 python -m app.cli init-db，版本标记不会修复缺失表"
                )
            for table in Base.metadata.tables.values():
                required_columns = {column.name for column in table.columns}
                actual_columns = {column["name"] for column in inspector.get_columns(table.name)}
                if not required_columns <= actual_columns:
                    # A current migration stamp cannot repair damage automatically.
                    raise RuntimeError(
                        "数据库 schema 缺少必需列，请检查数据库结构并从有效备份恢复；"
                        "迁移仍须显式运行 python -m app.cli init-db，版本标记不会修复缺失列"
                    )
    except (SQLAlchemyError, SQLiteError):
        # Suppress driver SQL, parameters, paths and connection details in normal
        # formatted tracebacks. This is not a global logging/debugger guarantee.
        raise RuntimeError(
            "数据库 schema 核验未完成，已停止启动；请检查数据库可用性、访问权限与结构"
        ) from None
    return True


def init_db(engine: Engine) -> None:
    cfg = _migration_config()
    with engine.connect() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
