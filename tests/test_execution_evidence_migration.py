"""Empty/legacy migration, guarded downgrade, fault rollback and old-protocol gaps."""

import sqlite3

import pytest
from alembic import command
from app.db.models import ExecutionEvidence
from app.db.schema import _migration_config, init_db, schema_ready
from app.db.session import begin_immediate
from app.services import execution_audit, kernel
from sqlalchemy import delete, event, inspect, select
from tests import test_link_audit as support

seeded = support.seeded


def migrate(engine, target, *, upgrade=False):
    cfg = _migration_config()
    with engine.connect() as connection:
        cfg.attributes["connection"] = connection
        (command.upgrade if upgrade else command.downgrade)(cfg, target)


def test_legacy_upgrade_preserves_rows_and_does_not_backfill_or_change_pending(
    seeded, engine, database_url, monkeypatch
):
    # Simulate the old protocol's lack of a retention hook, not a security fence.
    monkeypatch.setattr(execution_audit, "retain_execution", lambda *args, **kwargs: None)
    token = kernel.verify_failpoint.set(True)
    try:
        old = support.commit(seeded)
    finally:
        kernel.verify_failpoint.reset(token)
    migrate(engine, "task_runtime_mode")
    with engine.connect() as connection:
        before = {
            name: connection.exec_driver_sql(f'SELECT * FROM "{name}"').all()
            for name in inspect(connection).get_table_names()
            if name != "alembic_version"
        }
    migrate(engine, "head", upgrade=True)
    with engine.connect() as connection:
        for name, rows in before.items():
            assert connection.exec_driver_sql(f'SELECT * FROM "{name}"').all() == rows
        assert connection.exec_driver_sql("SELECT * FROM execution_evidence").all() == []
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    init_db(engine)
    assert schema_ready(engine)
    report = execution_audit.audit_execution(database_url, old.operation_id)
    assert report.status == "uncertain" and report.reason_code == "EXECUTION_EVIDENCE_NOT_RETAINED"


def test_empty_upgrade_downgrade_and_repeat_init(engine):
    migrate(engine, "task_runtime_mode")
    assert "execution_evidence" not in inspect(engine).get_table_names()
    with pytest.raises(RuntimeError, match="未迁移"):
        schema_ready(engine)
    init_db(engine)
    init_db(engine)
    assert schema_ready(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT * FROM execution_evidence").all() == []


def test_downgrade_with_evidence_refuses_without_losing_receipt_or_version(
    seeded, engine, database_url
):
    result = support.commit(seeded)
    before = support.dump(database_url)
    with pytest.raises(RuntimeError, match="执行证据非空"):
        migrate(engine, "task_runtime_mode")
    assert support.dump(database_url) == before
    assert schema_ready(engine)
    assert (
        execution_audit.audit_execution(database_url, result.operation_id).status
        == "consistent_for_retained_fields"
    )


@pytest.mark.parametrize("phase", ["before_ddl", "after_ddl", "version_stamp"])
def test_upgrade_failure_is_atomic_on_supported_engine(seeded, engine, database_url, phase):
    migrate(engine, "task_runtime_mode")
    before = support.dump(database_url)
    target_event = "after_cursor_execute" if phase == "after_ddl" else "before_cursor_execute"

    def interrupt(connection, cursor, statement, parameters, context, many):
        sql = statement.lstrip().upper()
        if ("CREATE TABLE EXECUTION_EVIDENCE" in sql and phase != "version_stamp") or (
            "UPDATE ALEMBIC_VERSION" in sql and phase == "version_stamp"
        ):
            raise RuntimeError("synthetic migration interruption")

    event.listen(engine, target_event, interrupt)
    try:
        with pytest.raises(RuntimeError, match="migration interruption"):
            migrate(engine, "head", upgrade=True)
    finally:
        event.remove(engine, target_event, interrupt)
    assert support.dump(database_url) == before
    init_db(engine)
    assert schema_ready(engine)


def test_foreign_keys_off_evidence_loss_is_uncertain_not_legacy_certification(seeded, database_url):
    result = support.commit(seeded)
    with sqlite3.connect(database_url.removeprefix("sqlite:///")) as writer:
        writer.execute("PRAGMA foreign_keys=OFF")
        writer.execute("DELETE FROM execution_evidence")
    assert (
        execution_audit.audit_execution(database_url, result.operation_id).reason_code
        == "EXECUTION_EVIDENCE_MISSING"
    )


def test_missing_evidence_reupgrade_never_recreates_an_attestation(seeded, engine, database_url):
    result = support.commit(seeded)
    with seeded() as session, begin_immediate(session):
        session.execute(delete(ExecutionEvidence))  # Deliberate synthetic corruption.
    migrate(engine, "task_runtime_mode")
    init_db(engine)
    with seeded() as session:
        assert session.scalar(select(ExecutionEvidence)) is None
    assert (
        execution_audit.audit_execution(database_url, result.operation_id).reason_code
        == "EXECUTION_EVIDENCE_MISSING"
    )


def test_same_database_permissions_are_not_an_immutable_evidence_boundary(seeded, database_url):
    result = support.commit(seeded)
    # Direct SQL is outside this protocol's writer control; do not advertise
    # cryptographic authenticity or immutable database rows.
    with sqlite3.connect(database_url.removeprefix("sqlite:///")) as writer:
        writer.execute("UPDATE execution_evidence SET digest='changed'")
    assert execution_audit.audit_execution(database_url, result.operation_id).status == "uncertain"
