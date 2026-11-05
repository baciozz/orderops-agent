"""Crash reservations stay charged, including across startup and CLI recovery."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from app.adapters.budget import BudgetLedger, reconcile_reservations
from app.cli import main
from app.config import Settings
from app.db.models import ModelUsage
from app.db.schema import init_db
from app.db.session import make_engine
from app.errors import BudgetExceeded, CallLimitExceeded
from app.main import create_app
from app.orchestration.inbox import accept_input
from app.orchestration.state import aware
from fastapi.testclient import TestClient
from sqlalchemy import select, text

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
TTL = timedelta(seconds=180)


def ledger(factory):
    return BudgetLedger(
        factory,
        cap_usd=Decimal("0.01"),
        price_in_per_mtok=Decimal("9"),
        price_out_per_mtok=Decimal("0"),
    )


def reserve(book, at, **kwargs):
    return book.reserve(
        provider="test", model="fixture", input_tokens=1000, max_output_tokens=0, at=at, **kwargs
    )


def test_orphan_ttl_keeps_cost_spent_and_call_slot(session_factory):
    task_id = accept_input(
        session_factory, thread_id="budget", client_event_key="budget", text="local"
    )[1]
    book = ledger(session_factory)
    old = reserve(book, NOW - TTL - timedelta(microseconds=1), task_id=task_id)
    before = book.spent()
    assert before == Decimal("0.009000")
    assert reconcile_reservations(session_factory, now=NOW, ttl=TTL) == 1
    assert reconcile_reservations(session_factory, now=NOW, ttl=TTL) == 0
    assert book.spent() == before
    assert book.calls_for_task(task_id) == 1
    with session_factory() as session:
        row = session.get(ModelUsage, old)
        assert row.status == "ORPHANED"
        assert row.cost_usd == before
        assert row.input_tokens == 1000
        assert aware(row.reserved_at) == NOW - TTL - timedelta(microseconds=1)
        assert row.settled_at is None
    with pytest.raises(CallLimitExceeded):
        reserve(book, NOW, task_id=task_id, max_calls_per_task=1)
    with pytest.raises(BudgetExceeded):
        reserve(book, NOW)
    assert book.spent() == before


def test_reconciliation_leaves_young_boundary_and_settled_rows(session_factory):
    book = ledger(session_factory)
    young = reserve(book, NOW - TTL + timedelta(seconds=1))
    book.settle(young, input_tokens=0, output_tokens=0, at=NOW)
    boundary = reserve(book, NOW - TTL)
    # Free enough cap for another reservation without changing its state.
    book.cap_usd = Decimal("1")
    recent = reserve(book, NOW - timedelta(seconds=1))
    assert reconcile_reservations(session_factory, now=NOW, ttl=TTL) == 0
    with session_factory() as session:
        assert session.get(ModelUsage, young).status == "SETTLED"
        assert session.get(ModelUsage, boundary).status == "RESERVED"
        assert session.get(ModelUsage, recent).status == "RESERVED"
        assert session.get(ModelUsage, recent).settled_at is None


@pytest.mark.parametrize("late", [False, True])
def test_settlement_is_idempotent_and_allows_late_actual_usage(session_factory, late):
    book = ledger(session_factory)
    reservation = reserve(book, NOW - TTL - timedelta(seconds=1))
    with session_factory() as session:
        assert session.get(ModelUsage, reservation).status == "RESERVED"
    if late:
        assert reconcile_reservations(session_factory, now=NOW, ttl=TTL) == 1
    row = book.settle(reservation, input_tokens=100, output_tokens=0, at=NOW)
    assert row.status == "SETTLED"
    assert row.settled_at == NOW
    assert book.spent() == Decimal("0.000900")
    again = book.settle(reservation, input_tokens=100, output_tokens=0, at=NOW + TTL)
    assert aware(again.settled_at) == NOW
    with pytest.raises(ValueError, match="已经结算"):
        book.settle(reservation, input_tokens=200, output_tokens=0)
    assert reconcile_reservations(session_factory, now=NOW + TTL, ttl=TTL) == 0
    assert book.spent() == Decimal("0.000900")
    with session_factory() as session:
        assert len(list(session.scalars(select(ModelUsage)))) == 1


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("new_prices", [(Decimal("90"), Decimal("80")), (None, None)])
def test_settlement_uses_reserved_prices_after_configuration_change(
    session_factory, late, new_prices
):
    first = BudgetLedger(
        session_factory,
        cap_usd=Decimal("1"),
        price_in_per_mtok=Decimal("9"),
        price_out_per_mtok=Decimal("2"),
    )
    reservation = first.reserve(
        provider="test",
        model="fixture",
        input_tokens=1000,
        max_output_tokens=500,
        at=NOW - TTL - timedelta(seconds=1),
    )
    if late:
        assert reconcile_reservations(session_factory, now=NOW, ttl=TTL) == 1
    second = BudgetLedger(
        session_factory,
        cap_usd=Decimal("1"),
        price_in_per_mtok=new_prices[0],
        price_out_per_mtok=new_prices[1],
    )
    row = second.settle(reservation, input_tokens=100, output_tokens=50, at=NOW)
    assert row.status == "SETTLED"
    assert row.price_in_per_mtok == Decimal("9")
    assert row.price_out_per_mtok == Decimal("2")
    assert row.cost_usd == second.spent() == Decimal("0.001000")
    assert aware(second.settle(reservation, input_tokens=100, output_tokens=50).settled_at) == NOW


def test_unknown_historical_prices_fall_back_to_ledger(session_factory):
    book = ledger(session_factory)
    reservation = reserve(book, NOW)
    with session_factory() as session, session.begin():
        row = session.get(ModelUsage, reservation)
        row.price_in_per_mtok = row.price_out_per_mtok = None
    assert book.settle(reservation, input_tokens=100, output_tokens=0).cost_usd == Decimal(
        "0.000900"
    )


def test_startup_reconciles_existing_ledger(session_factory, database_url):
    now = datetime.now(UTC)
    book = ledger(session_factory)
    old = reserve(book, now - timedelta(days=1))
    app = create_app(Settings(_env_file=None, database_url=database_url))
    with session_factory() as session:
        assert session.get(ModelUsage, old).status == "RESERVED"
    with TestClient(base_url="http://127.0.0.1", app=app, raise_server_exceptions=False) as client:
        assert client.get("/health").status_code == 200
        with session_factory() as session:
            assert session.get(ModelUsage, old).status == "ORPHANED"
    assert book.spent() == Decimal("0.009000")
    with TestClient(base_url="http://127.0.0.1", app=app):
        assert book.spent() == Decimal("0.009000")


def test_startup_does_not_create_uninitialized_database(tmp_path):
    path = tmp_path / "absent.db"
    app = create_app(Settings(_env_file=None, database_url=f"sqlite:///{path}"))
    with TestClient(base_url="http://127.0.0.1", app=app) as client:
        assert client.get("/health").status_code == 200
    assert not path.exists()


def test_cli_reconciles_existing_ledger(session_factory, database_url, monkeypatch, capsys):
    book = ledger(session_factory)
    old = reserve(book, datetime.now(UTC) - timedelta(days=1))
    monkeypatch.setenv("OOS_DATABASE_URL", database_url)
    assert main(["reconcile-budget"]) == 0
    assert "1" in capsys.readouterr().out
    with session_factory() as session:
        assert session.get(ModelUsage, old).status == "ORPHANED"
    assert main(["reconcile-budget"]) == 0
    assert "0" in capsys.readouterr().out
    assert book.spent() == Decimal("0.009000")


def test_reservation_ttl_has_safe_floor_and_env_override(monkeypatch):
    assert Settings(_env_file=None).effective_budget_reservation_ttl_s == 180
    assert Settings(_env_file=None, model_timeout_s=200).effective_budget_reservation_ttl_s == 460
    monkeypatch.setenv("OOS_BUDGET_RESERVATION_TTL_S", "900")
    assert Settings(_env_file=None).effective_budget_reservation_ttl_s == 900
    monkeypatch.setenv("OOS_BUDGET_RESERVATION_TTL_S", "1")
    assert Settings(_env_file=None).effective_budget_reservation_ttl_s == 180
    with pytest.raises(ValueError):
        Settings(_env_file=None, budget_reservation_ttl_s=0)


def test_reconciliation_rejects_nonpositive_ttl(session_factory):
    with pytest.raises(ValueError):
        reconcile_reservations(session_factory, now=NOW, ttl=timedelta(0))


def test_migration_backfills_historical_usage_as_settled(tmp_path):
    root = Path(__file__).resolve().parents[1]
    engine = make_engine(f"sqlite:///{tmp_path / 'historical.db'}")
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    try:
        with engine.connect() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "source_stage3")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO model_usage "
                    "(id, provider, model, input_tokens, output_tokens, "
                    "cost_usd, price_version, at) "
                    "VALUES ('historical', 'test', 'fixture', 1, 2, '0.003', 'test', :at)"
                ),
                {"at": NOW.isoformat()},
            )
        init_db(engine)
        with engine.connect() as connection:
            row = connection.execute(text("SELECT * FROM model_usage")).mappings().one()
            assert row["status"] == "SETTLED"
            assert row["reserved_at"] == row["at"] == row["settled_at"]
            assert Decimal(str(row["cost_usd"])) == Decimal("0.003")
        # Exercise the reverse migration and upgrade on a populated database too.
        with engine.connect() as connection:
            cfg.attributes["connection"] = connection
            command.downgrade(cfg, "source_stage3")
        init_db(engine)
    finally:
        engine.dispose()


def test_price_snapshot_migration_backfills_versions_and_downgrades(tmp_path):
    root = Path(__file__).resolve().parents[1]
    engine = make_engine(f"sqlite:///{tmp_path / 'historical-prices.db'}")
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    versions = [
        "in=1.25;out=10.5",
        "in=0;out=2e-2",
        "test",
        "in=NaN;out=1",
        "in=1;out=Infinity",
        "in=-1;out=2",
        "in=1;out=bad",
        "in=1;out=2;extra=3",
    ]
    try:
        with engine.connect() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "budget_reservations")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO model_usage "
                    "(id, provider, model, input_tokens, output_tokens, cost_usd, price_version, "
                    "at, reserved_at, settled_at) "
                    "VALUES (:id, 'test', 'fixture', 1, 2, '0.003', :version, :at, :at, :at)"
                ),
                [
                    {"id": str(index), "version": version, "at": NOW.isoformat()}
                    for index, version in enumerate(versions)
                ],
            )
        init_db(engine)
        with engine.connect() as connection:
            rows = (
                connection.execute(text("SELECT * FROM model_usage ORDER BY id")).mappings().all()
            )
            for row, version in zip(rows, versions, strict=True):
                assert row["price_version"] == version
                assert row["status"] == "SETTLED"
                assert row["at"] == row["reserved_at"] == row["settled_at"]
                assert Decimal(row["cost_usd"]) == Decimal("0.003")
            assert Decimal(rows[0]["price_in_per_mtok"]) == Decimal("1.25")
            assert Decimal(rows[0]["price_out_per_mtok"]) == Decimal("10.5")
            assert Decimal(rows[1]["price_in_per_mtok"]) == Decimal("0")
            assert Decimal(rows[1]["price_out_per_mtok"]) == Decimal("0.02")
            for row in rows[2:]:
                assert row["price_in_per_mtok"] is row["price_out_per_mtok"] is None
        with engine.connect() as connection:
            cfg.attributes["connection"] = connection
            command.downgrade(cfg, "budget_reservations")
            columns = connection.execute(text("PRAGMA table_info(model_usage)")).all()
            assert "price_in_per_mtok" not in {column[1] for column in columns}
            assert "price_out_per_mtok" not in {column[1] for column in columns}
        init_db(engine)
    finally:
        engine.dispose()
