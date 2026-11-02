"""Apply Alembic migrations. Request handlers do not create tables."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

_ROOT = Path(__file__).resolve().parents[2]


def init_db(engine: Engine) -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    with engine.connect() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
