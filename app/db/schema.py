"""Apply Alembic migrations. Request handlers do not create tables."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

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
    head = ScriptDirectory.from_config(_migration_config()).get_current_head()
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        if not tables:
            return False
        current = MigrationContext.configure(connection).get_current_heads()
        missing = set(Base.metadata.tables) - tables
        if current != (head,) or missing:
            version = ", ".join(current) or "无版本"
            message = (
                f"数据库 schema 未迁移到最新版本（当前 {version}，需要 {head}），"
                "请先运行 python -m app.cli init-db"
            )
            if missing:
                message += f"（缺少表：{', '.join(sorted(missing))}）"
            raise RuntimeError(message)
        for table in Base.metadata.tables.values():
            required_columns = {column.name for column in table.columns}
            actual_columns = {column["name"] for column in inspector.get_columns(table.name)}
            if not required_columns <= actual_columns:
                # Do not echo reflected database identifiers or values. A current
                # migration stamp cannot repair a damaged schema automatically.
                raise RuntimeError(
                    "数据库 schema 缺少必需列，请检查数据库结构并从有效备份恢复；"
                    "迁移仍须显式运行 python -m app.cli init-db，版本标记不会修复缺失列"
                )
    return True


def init_db(engine: Engine) -> None:
    cfg = _migration_config()
    with engine.connect() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
