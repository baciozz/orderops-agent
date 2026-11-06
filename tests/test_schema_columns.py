"""Required mapped names are a startup guard, not full schema certification."""

from unittest.mock import Mock

import pytest
from alembic.script import ScriptDirectory
from app.config import Settings
from app.db.base import Base
from app.db.schema import _migration_config, schema_ready
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect

_COLUMN_ERROR = (
    "数据库 schema 缺少必需列，请检查数据库结构并从有效备份恢复；"
    "迁移仍须显式运行 python -m app.cli init-db，版本标记不会修复缺失列"
)


def _snapshot(engine):
    with engine.connect() as connection:
        return list(connection.exec_driver_sql("SELECT * FROM sqlite_master ORDER BY name")), {
            table: list(connection.exec_driver_sql(f'SELECT * FROM "{table}"'))
            for table in inspect(connection).get_table_names()
        }


@pytest.mark.parametrize("table_name", sorted(Base.metadata.tables))
@pytest.mark.parametrize("subset", ["first", "last", "all"])
def test_every_table_requires_its_mapped_column_names(table_name, subset):
    # Deliberately name-only synthetic schema: no constraints/types are promised.
    engine = create_engine("sqlite:///:memory:")
    head = ScriptDirectory.from_config(_migration_config()).get_current_head()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE alembic_version (version_num TEXT)")
            connection.exec_driver_sql("INSERT INTO alembic_version VALUES (?)", (head,))
            for table in Base.metadata.tables.values():
                columns = [column.name for column in table.columns]
                if table.name == table_name:
                    columns = columns[1:] if subset == "first" else columns[:-1]
                    if subset == "all":
                        columns = []
                names = ", ".join(f'"{column}" TEXT' for column in columns)
                connection.exec_driver_sql(
                    f'CREATE TABLE "{table.name}" ({names + ", " if names else ""}'
                    '"unexpected_column_canary" TEXT)'
                )
        before = _snapshot(engine)
        with pytest.raises(RuntimeError) as error:
            schema_ready(engine)
        assert str(error.value) == _COLUMN_ERROR
        assert _snapshot(engine) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("customer", "display_name"),
        ("model_usage", "reserved_at"),  # Nullable still belongs to the mapped contract.
        ("operation", "verification_last_error_code"),
        ("input_source", "original_name"),
        ("source_upload_receipt", "payload_sha256"),
    ],
)
def test_current_head_missing_column_refuses_startup_without_writes(
    engine, database_url, monkeypatch, table, column
):
    with engine.begin() as connection:
        connection.exec_driver_sql(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        connection.exec_driver_sql(
            "INSERT INTO customer (id, code, active) VALUES ('canary', 'synthetic', 1)"
            if table == "customer"
            else "INSERT INTO customer VALUES ('canary', 'synthetic', 'private_canary', 1)"
        )
    before = _snapshot(engine)
    reconcile = Mock(side_effect=AssertionError("Must not reconcile a damaged schema"))
    migrate = Mock(side_effect=AssertionError("Must not migrate during startup"))
    monkeypatch.setattr("app.main.reconcile_reservations", reconcile)
    monkeypatch.setattr("app.db.schema.command.upgrade", migrate)
    app = create_app(Settings(_env_file=None, database_url=database_url))
    statements = []
    event.listen(
        app.state.engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, many: statements.append(statement),
    )
    with pytest.raises(RuntimeError) as error, TestClient(app, base_url="http://127.0.0.1"):
        pytest.fail("Corrupt latest schema must reject startup")
    assert str(error.value) == _COLUMN_ERROR
    reconcile.assert_not_called()
    migrate.assert_not_called()
    assert _snapshot(engine) == before
    assert statements
    assert all(s.lstrip().upper().startswith(("SELECT", "PRAGMA", "BEGIN")) for s in statements)


def test_latest_schema_and_extra_columns_are_accepted(engine, database_url, monkeypatch):
    assert schema_ready(engine) is True
    with engine.begin() as connection:
        connection.exec_driver_sql('ALTER TABLE customer ADD COLUMN "extra_canary" TEXT')
        connection.exec_driver_sql('CREATE TABLE "extra_table_canary" (value TEXT)')
    assert schema_ready(engine) is True
    reconcile = Mock()
    monkeypatch.setattr("app.main.reconcile_reservations", reconcile)
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)),
        base_url="http://127.0.0.1",
    ) as client:
        assert client.get("/health").status_code == 200
    reconcile.assert_called_once()


def test_missing_database_health_does_not_create_or_reconcile(tmp_path, monkeypatch):
    path = tmp_path / "absent" / "missing.db"
    reconcile = Mock(side_effect=AssertionError("Must not reconcile an absent database"))
    monkeypatch.setattr("app.main.reconcile_reservations", reconcile)
    with TestClient(
        create_app(Settings(_env_file=None, database_url=f"sqlite:///{path}")),
        base_url="http://127.0.0.1",
    ) as client:
        assert client.get("/health").status_code == 200
    reconcile.assert_not_called()
    assert not path.parent.exists()
