"""Isolated settings and a file-backed SQLite engine for each test."""

import os
import selectors
import socket
from pathlib import Path

import pytest
from app.config import Settings, reset_settings_cache
from app.db.schema import init_db
from app.db.session import make_engine, make_session_factory
from app.metrics import reset_for_tests


@pytest.fixture(autouse=True)
def _isolate_metrics():
    reset_for_tests()
    yield
    reset_for_tests()


def pytest_configure(config: pytest.Config) -> None:
    """Disable dotenv before collection can import app.main's default app."""
    patch = pytest.MonkeyPatch()
    patch.setitem(Settings.model_config, "env_file", None)
    reset_settings_cache()
    config.add_cleanup(patch.undo)
    config.add_cleanup(reset_settings_cache)


@pytest.fixture(scope="session", autouse=True)
def _sandbox_asyncio_polling():
    """TestClient needs thread wakeups even where sandboxed local IPC is denied.

    CPython silently ignores EPERM from its socketpair wakeup. A short selector
    timeout lets queued callbacks run without IPC. Normal environments are unchanged.
    No IP socket or outbound connection is opened by this probe.
    """
    reader, writer = socket.socketpair()
    try:
        try:
            writer.send(b"x")
        except PermissionError:
            restricted = True
        else:
            restricted = False
    finally:
        reader.close()
        writer.close()
    with pytest.MonkeyPatch.context() as patch:
        if restricted:
            original = selectors.DefaultSelector.select

            def poll(self, timeout=None):
                return original(self, 0.01 if timeout is None else min(timeout, 0.01))

            patch.setattr(selectors.DefaultSelector, "select", poll)
        yield


@pytest.fixture(autouse=True)
def _isolate_oos_env(monkeypatch: pytest.MonkeyPatch):
    for key in list(os.environ):
        if key.startswith("OOS_"):
            monkeypatch.delenv(key, raising=False)
    reset_settings_cache()
    yield
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
