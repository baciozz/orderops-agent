"""Single schema entry point.

TODO(stage-1A): move this to Alembic at the first real schema change. Stage 0
keeps ``create_all`` here so request handlers never create tables, and so we
do not freeze a migration the next stage would immediately rewrite.
"""

from sqlalchemy.engine import Engine

from app.db import models as _models  # noqa: F401
from app.db.base import Base


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
