"""WAL, foreign keys, and BEGIN IMMEDIATE actually take the reserved lock."""

import pytest
from app.db.models import ShipmentRecord, Warehouse
from app.db.session import begin_immediate
from seeds.golden import SEED_AT
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session


def test_connection_enables_wal_and_foreign_keys(engine) -> None:
    with engine.connect() as connection:
        journal = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert str(journal).lower() == "wal"
    assert foreign_keys == 1


def test_foreign_key_rejects_unknown_order(session_factory) -> None:
    session: Session = session_factory()
    with pytest.raises(IntegrityError):
        with begin_immediate(session):
            session.add(
                ShipmentRecord(
                    id="SHP-BAD",
                    order_id="SO-MISSING",
                    sku_id="A-BLACK-M",
                    qty=1,
                    shipped_at=SEED_AT,
                    source="test",
                    operation_id=None,
                )
            )
    session.close()


def test_begin_immediate_locks_out_another_writer(session_factory) -> None:
    first: Session = session_factory()
    second: Session = session_factory()
    try:
        with begin_immediate(first):
            first.execute(text("SELECT 1"))
            with pytest.raises(OperationalError, match="locked"):
                with begin_immediate(second):
                    second.execute(text("SELECT 1"))
        with begin_immediate(second):
            second.add(Warehouse(id="W-AFTER", code="W-AFTER", name="after"))
        check = session_factory()
        try:
            assert check.get(Warehouse, "W-AFTER") is not None
        finally:
            check.close()
    finally:
        first.close()
        second.close()


def test_deferred_read_does_not_block_immediate_writer(session_factory) -> None:
    reader: Session = session_factory()
    writer: Session = session_factory()
    try:
        with reader.begin():
            reader.execute(text("SELECT 1"))
            with begin_immediate(writer):
                writer.add(Warehouse(id="W-FREE", code="W-FREE", name="free"))
    finally:
        reader.close()
        writer.close()


def test_begin_immediate_rolls_back(session_factory) -> None:
    session: Session = session_factory()
    with pytest.raises(RuntimeError, match="boom"):
        with begin_immediate(session):
            session.add(Warehouse(id="W-ROLL", code="W-ROLL", name="roll"))
            raise RuntimeError("boom")
    session.close()
    check = session_factory()
    try:
        assert check.get(Warehouse, "W-ROLL") is None
    finally:
        check.close()
