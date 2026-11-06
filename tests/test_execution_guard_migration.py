"""No legacy certification/backfill and no silent loss of scoped observation semantics."""

import pytest
from app.db.models import Operation
from app.db.schema import init_db, schema_ready
from app.db.session import begin_immediate
from app.services import verification
from sqlalchemy import event
from tests import test_execution_evidence_migration as migration
from tests import test_link_audit as support
from tests import test_verification_result_fencing as fencing

seeded = support.seeded


def test_upgrade_preserves_old_pending_and_receipt_without_observation(engine):
    migration.migrate(engine, "execution_evidence_binding")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO operation(id,idempotency_key,request_hash,status,result_json,"
            "committed_verification_pending) VALUES('old','old','hash','COMMITTED','{}',0)"
        )
        before = connection.exec_driver_sql("SELECT * FROM operation").mappings().one()
    init_db(engine)
    with engine.connect() as connection:
        after = connection.exec_driver_sql("SELECT * FROM operation").mappings().one()
        assert after["verification_observation"] is None
        assert {k: after[k] for k in before} == before
    assert schema_ready(engine)


@pytest.mark.parametrize("phase", ["before_ddl", "after_ddl", "version_stamp"])
def test_interrupted_guard_migration_rolls_back_and_restarts(engine, database_url, phase):
    migration.migrate(engine, "execution_evidence_binding")
    before = support.dump(database_url)
    target = "after_cursor_execute" if phase == "after_ddl" else "before_cursor_execute"

    def interrupt(connection, cursor, statement, parameters, context, many):
        sql = statement.upper()
        if ("ADD COLUMN VERIFICATION_OBSERVATION" in sql and phase != "version_stamp") or (
            "UPDATE ALEMBIC_VERSION" in sql and phase == "version_stamp"
        ):
            raise RuntimeError("synthetic guard migration failure")

    event.listen(engine, target, interrupt)
    try:
        with pytest.raises(RuntimeError, match="guard migration failure"):
            init_db(engine)
    finally:
        event.remove(engine, target, interrupt)
    assert support.dump(database_url) == before
    init_db(engine)
    assert schema_ready(engine)


def test_observation_alone_blocks_downgrade_before_any_schema_change(engine, database_url):
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO operation(id,idempotency_key,request_hash,status,result_json,"
            "committed_verification_pending,verification_observation) "
            "VALUES('old','old','hash','COMMITTED','{}',1,'{}')"
        )
    before = support.dump(database_url)
    with pytest.raises(RuntimeError, match="核验观察记录非空"):
        migration.migrate(engine, "task_runtime_mode")
    assert support.dump(database_url) == before


def test_result_write_fault_keeps_pending_and_can_recover(seeded, engine):
    result = support.commit(seeded)
    with seeded() as session, begin_immediate(session):
        session.get(Operation, result.operation_id).committed_verification_pending = True

    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("UPDATE operation SET") and "verification_observation" in statement:
            raise RuntimeError("synthetic publication failure")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        assert verification.retry_pending(seeded, result.operation_id, now=fencing.NOW) == 1
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    state = fencing.operation(seeded, result.operation_id)
    assert state["committed_verification_pending"] and state["verification_last_error_code"]
    assert (
        verification.retry_pending(
            seeded, result.operation_id, now=state["verification_next_attempt_at"]
        )
        == 1
    )
    assert not fencing.operation(seeded, result.operation_id)["committed_verification_pending"]
