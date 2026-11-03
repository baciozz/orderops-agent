"""Isolated settings and a file-backed SQLite engine for each test."""

import os
from pathlib import Path

import pytest
from app.config import reset_settings_cache
from app.db.schema import init_db
from app.db.session import make_engine, make_session_factory


@pytest.fixture(autouse=True)
def _isolate_oos_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("OOS_"):
            monkeypatch.delenv(key, raising=False)
    reset_settings_cache()


@pytest.fixture()
def database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'oos.db'}"


@pytest.fixture()
def engine(database_url: str):
    eng = make_engine(database_url, busy_timeout_ms=200)
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return make_session_factory(engine)
