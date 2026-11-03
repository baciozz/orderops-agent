"""The process starts, reports FIXTURE, and only binds the Stage 0 routes."""

from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from app.config import Settings
from app.db.schema import init_db
from app.db.session import make_engine
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text


def test_health_and_root() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))
    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["stage"] == "0"
    assert body["mode"] == "FIXTURE"
    assert body["live_requested"] is False
    assert body["live_block_reason"] is None
    assert Decimal(body["effective_live_budget_usd"]) == Decimal("5.00")
    assert body["schema_version"] == "1.0"
    assert health.headers["x-request-id"]
    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["task_view_stub"] == "/tasks/T-STUB/view"


def test_openapi_lists_mounted_routes() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))
    schema = client.get("/openapi.json").json()
    assert "/health" in schema["paths"]
    assert "/tasks/{task_id}/view" in schema["paths"]
    assert "/plans" in schema["paths"]
    assert "/dispatch" in schema["paths"]
    assert "/orders/{order_id}" in schema["paths"]


@pytest.mark.parametrize("revision", ["source_stage3", "budget_reservations"])
def test_startup_requires_explicit_migration(tmp_path, revision):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = make_engine(url)
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    settings = Settings(_env_file=None, database_url=url)
    try:
        with engine.connect() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, revision)
        with pytest.raises(RuntimeError) as error, TestClient(create_app(settings)):
            pytest.fail("An outdated schema must refuse startup")
        assert str(error.value).startswith(
            f"数据库 schema 未迁移到最新版本（当前 {revision}，需要 budget_price_snapshot），"
            "请先运行 python -m app.cli init-db"
        )
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == revision
        init_db(engine)
        with TestClient(create_app(settings)) as client:
            assert client.get("/health").status_code == 200
    finally:
        engine.dispose()


@pytest.mark.parametrize("damage", ["no-version", "empty-version", "missing-table"])
def test_startup_rejects_partial_or_unversioned_schema(tmp_path, damage):
    url = f"sqlite:///{tmp_path / 'partial.db'}"
    engine = make_engine(url)
    try:
        init_db(engine)
        with engine.begin() as connection:
            if damage == "no-version":
                connection.execute(text("DROP TABLE alembic_version"))
            elif damage == "empty-version":
                connection.execute(text("DELETE FROM alembic_version"))
            else:
                connection.execute(text("DROP TABLE model_usage"))
        with (
            pytest.raises(RuntimeError, match="python -m app.cli init-db"),
            TestClient(create_app(Settings(_env_file=None, database_url=url))),
        ):
            pytest.fail("A partial schema must refuse startup")
    finally:
        engine.dispose()


@pytest.mark.parametrize("database", ["file", "memory"])
def test_empty_database_allows_health_without_reconciliation(tmp_path, monkeypatch, database):
    url = f"sqlite:///{tmp_path / 'empty.db'}" if database == "file" else "sqlite:///:memory:"
    engine = make_engine(url)
    # Create an existing, entirely empty file (distinct from the absent-file test).
    with engine.connect() as connection:
        assert inspect(connection).get_table_names() == []
    engine.dispose()

    def unexpected_reconciliation(*args, **kwargs):
        pytest.fail("An empty health database must not reconcile a ledger")

    monkeypatch.setattr("app.main.reconcile_reservations", unexpected_reconciliation)
    app = create_app(Settings(_env_file=None, database_url=url))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        with app.state.engine.connect() as connection:
            assert inspect(connection).get_table_names() == []
