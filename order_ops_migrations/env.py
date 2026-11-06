"""Alembic environment. Tests and the CLI pass an existing connection."""

from alembic import context
from app.db import models as _models  # noqa: F401
from app.db.base import Base
from app.db.session import make_engine
from sqlalchemy.engine import Connection

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )


def run_migrations_online() -> None:
    given = config.attributes.get("connection")
    if given is not None:
        _configure(given)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(config.get_main_option("sqlalchemy.url") or "sqlite:///./data/oos.db")
    with engine.connect() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
