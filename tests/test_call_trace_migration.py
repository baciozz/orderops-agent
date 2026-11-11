"""call_trace migration adds one table and preserves existing rows."""

from alembic import command
from app.db.schema import _migration_config, init_db, schema_ready
from sqlalchemy import inspect, text


def migrate(engine, target, *, upgrade=False):
    cfg = _migration_config()
    with engine.connect() as connection:
        cfg.attributes["connection"] = connection
        (command.upgrade if upgrade else command.downgrade)(cfg, target)


def test_upgrade_and_downgrade_on_empty_database(engine):
    migrate(engine, "local_review_workspace")
    assert "call_trace" not in inspect(engine).get_table_names()
    migrate(engine, "head", upgrade=True)
    assert "call_trace" in inspect(engine).get_table_names()
    init_db(engine)
    assert schema_ready(engine)
    migrate(engine, "local_review_workspace")
    assert "call_trace" not in inspect(engine).get_table_names()
    assert inspect(engine).get_table_names()
    migrate(engine, "head", upgrade=True)
    assert "call_trace" in inspect(engine).get_table_names()
    assert schema_ready(engine)


def test_upgrade_from_populated_legacy_database_preserves_rows(engine):
    migrate(engine, "local_review_workspace")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO customer (id, code, display_name, active) "
                "VALUES ('C-KEEP', 'keep', '原有客户', 1)"
            )
        )
        before_tables = [
            name for name in inspect(connection).get_table_names() if name != "alembic_version"
        ]
        before = {
            name: (
                [column["name"] for column in inspect(connection).get_columns(name)],
                connection.exec_driver_sql('SELECT * FROM "' + name + '"').all(),
            )
            for name in before_tables
        }
    migrate(engine, "head", upgrade=True)
    with engine.connect() as connection:
        for name, (columns, rows) in before.items():
            quoted = ", ".join('"' + column + '"' for column in columns)
            assert (
                connection.exec_driver_sql("SELECT " + quoted + ' FROM "' + name + '"').all()
                == rows
            )
        assert "call_trace" in inspect(connection).get_table_names()
        assert connection.exec_driver_sql("SELECT * FROM call_trace").all() == []
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    # Downgrade must actually run and drop only call_trace.
    migrate(engine, "local_review_workspace")
    with engine.connect() as connection:
        assert "call_trace" not in inspect(connection).get_table_names()
        for name, (columns, rows) in before.items():
            quoted = ", ".join('"' + column + '"' for column in columns)
            assert (
                connection.exec_driver_sql("SELECT " + quoted + ' FROM "' + name + '"').all()
                == rows
            )
