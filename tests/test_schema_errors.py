"""Local startup diagnostics: synthetic databases and fake secret-shaped canaries only."""

import sqlite3
import traceback
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from alembic.script import ScriptDirectory
from app.config import Settings
from app.db import schema
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

CANARY = "FAKE_ONLY_SECRET_https://fake:password@example.invalid/private.db\n\x1b[31m"
METADATA_ERROR = (
    "数据库迁移版本信息缺失、不一致或无法读取，请检查数据库访问、结构与迁移历史；"
    "确认迁移适用后才显式运行 python -m app.cli init-db，必要时从有效备份恢复"
)
READ_ERROR = "数据库 schema 核验未完成，已停止启动；请检查数据库可用性、访问权限与结构"


def _head():
    return ScriptDirectory.from_config(schema._migration_config()).get_current_head()


def _safe(error, expected):
    assert type(error) is RuntimeError
    assert str(error) == expected
    rendered = "".join(traceback.format_exception(error))
    assert "FAKE_ONLY_SECRET" not in rendered
    assert "OperationalError" not in rendered
    assert error.__cause__ is None


def _refuses_startup(url, monkeypatch, expected):
    reconcile = Mock(side_effect=AssertionError("Unexpected reconciliation"))
    migrate = Mock(side_effect=AssertionError("Unexpected migration"))
    monkeypatch.setattr("app.main.reconcile_reservations", reconcile)
    monkeypatch.setattr(schema.command, "upgrade", migrate)
    app = create_app(Settings(_env_file=None, database_url=url))
    with pytest.raises(RuntimeError) as caught, TestClient(app, base_url="http://127.0.0.1"):
        pytest.fail("Failed verification must stop startup")
    _safe(caught.value, expected)
    reconcile.assert_not_called()
    migrate.assert_not_called()


@pytest.mark.parametrize(
    "values",
    [
        [],
        ["unknown_revision"],
        [""],
        [None],
        [123],
        [b"FAKE_ONLY_SECRET"],
        [CANARY],
        [CANARY * 2000],
        ["upload"],  # A trusted revision prefix is not a revision identifier.
        ["head"],
        ["@head", "@head"],
        ["source_stage3", "budget_reservations"],
        ["@head", CANARY],
        [CANARY, "@head"],
    ],
)
def test_invalid_revision_records_fail_closed_without_echo(
    engine, database_url, monkeypatch, values
):
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version")
        # No uniqueness or type constraints, so malformed rows are testable.
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num)")
        for value in values:
            connection.exec_driver_sql(
                "INSERT INTO alembic_version VALUES (?)", (_head() if value == "@head" else value,)
            )
    with pytest.raises(RuntimeError) as caught:
        schema.schema_ready(engine)
    _safe(caught.value, METADATA_ERROR)
    _refuses_startup(database_url, monkeypatch, METADATA_ERROR)


def test_malformed_version_column_is_safe(engine, database_url, monkeypatch):
    with engine.begin() as connection:
        connection.exec_driver_sql(
            'ALTER TABLE alembic_version RENAME COLUMN version_num TO "FAKE_ONLY_SECRET"'
        )
    with pytest.raises(RuntimeError) as caught:
        schema.schema_ready(engine)
    _safe(caught.value, METADATA_ERROR)
    assert caught.value.__suppress_context__
    _refuses_startup(database_url, monkeypatch, METADATA_ERROR)


@pytest.mark.parametrize("stage", ["connect", "inspect", "tables", "version", "columns", "exit"])
@pytest.mark.parametrize("kind", ["sqlalchemy", "sqlite"])
def test_database_failures_have_safe_local_diagnostics(
    engine, database_url, monkeypatch, stage, kind
):
    failure = (
        OperationalError("SELECT " + CANARY, {"fake": CANARY}, sqlite3.OperationalError(CANARY))
        if kind == "sqlalchemy"
        else sqlite3.OperationalError(CANARY)
    )

    def fail(*args, **kwargs):
        raise failure

    if stage == "connect":
        monkeypatch.setattr(type(engine), "connect", fail)
    elif stage == "exit":
        original = type(engine).connect

        @contextmanager
        def failing_exit(self):
            with original(self) as connection:
                yield connection
            raise failure

        monkeypatch.setattr(type(engine), "connect", failing_exit)
    elif stage == "inspect":
        monkeypatch.setattr(schema, "inspect", fail)
    elif stage == "version":
        monkeypatch.setattr(schema.MigrationContext, "get_current_heads", fail)
    else:
        original_inspect = schema.inspect

        def failing_inspect(connection):
            inspector = original_inspect(connection)
            method = "get_table_names" if stage == "tables" else "get_columns"
            monkeypatch.setattr(inspector, method, fail)
            return inspector

        monkeypatch.setattr(schema, "inspect", failing_inspect)
    expected = METADATA_ERROR if stage == "version" else READ_ERROR
    with pytest.raises(RuntimeError) as caught:
        schema.schema_ready(engine)
    _safe(caught.value, expected)
    assert caught.value.__suppress_context__
    _refuses_startup(database_url, monkeypatch, expected)


@pytest.mark.parametrize("failure", [TypeError("programming bug"), KeyError("programming bug")])
def test_programming_errors_are_not_relabeled(engine, monkeypatch, failure):
    monkeypatch.setattr(schema, "inspect", Mock(side_effect=failure))
    with pytest.raises(type(failure)) as caught:
        schema.schema_ready(engine)
    assert caught.value is failure


def test_reconciliation_errors_are_outside_boundary(engine, database_url, monkeypatch):
    failure = OperationalError(
        "synthetic reconciliation", {}, sqlite3.OperationalError("synthetic")
    )
    monkeypatch.setattr("app.main.reconcile_reservations", Mock(side_effect=failure))
    with (
        pytest.raises(OperationalError) as caught,
        TestClient(
            create_app(Settings(_env_file=None, database_url=database_url)),
            base_url="http://127.0.0.1",
        ),
    ):
        pytest.fail("Reconciliation must fail")
    assert caught.value is failure


def test_migration_errors_are_outside_boundary(engine, monkeypatch):
    failure = OperationalError("synthetic migration", {}, sqlite3.OperationalError("synthetic"))
    monkeypatch.setattr(schema.command, "upgrade", Mock(side_effect=failure))
    with pytest.raises(OperationalError) as caught:
        schema.init_db(engine)
    assert caught.value is failure


def test_missing_head_is_release_configuration_failure(engine, monkeypatch):
    scripts = Mock()
    scripts.get_current_head.return_value = None
    scripts.walk_revisions.return_value = []
    monkeypatch.setattr(schema.ScriptDirectory, "from_config", Mock(return_value=scripts))
    connect = Mock(side_effect=AssertionError("Must validate release before connecting"))
    monkeypatch.setattr(engine, "connect", connect)
    with pytest.raises(RuntimeError, match="应用迁移配置"):
        schema.schema_ready(engine)
    connect.assert_not_called()


def test_migration_tree_errors_are_not_database_errors(engine, monkeypatch):
    failure = RuntimeError("synthetic invalid or multiple migration heads")
    monkeypatch.setattr(schema.ScriptDirectory, "from_config", Mock(side_effect=failure))
    with pytest.raises(RuntimeError) as caught:
        schema.schema_ready(engine)
    assert caught.value is failure


@pytest.mark.parametrize("revision", ["unknown_revision", "source_stage3", "@head"])
def test_missing_tables_never_receive_upgrade_only_advice(engine, monkeypatch, revision):
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE customer")
        connection.exec_driver_sql(
            "UPDATE alembic_version SET version_num = ?",
            (_head() if revision == "@head" else revision,),
        )
    with pytest.raises(RuntimeError) as caught:
        schema.schema_ready(engine)
    message = str(caught.value)
    assert "必需表" in message
    assert "恢复" in message
    assert "python -m app.cli init-db" in message
    assert "unknown_revision" not in message
