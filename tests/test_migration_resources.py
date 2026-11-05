"""Canonical resources must work independently of cwd and runtime ini files."""

import shutil
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.db import schema
from order_ops_migrations import MIGRATIONS_DIR

CHAIN = [
    "3e057b273982",
    "durable_stage2",
    "source_stage3",
    "budget_reservations",
    "budget_price_snapshot",
    "verification_retries",
    "upload_idempotency",
]


def test_runtime_and_development_config_share_canonical_resources(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = bogus\n")
    runtime = schema._migration_config()
    assert runtime.config_file_name is None
    assert runtime.get_main_option("sqlalchemy.url") is None
    scripts = ScriptDirectory.from_config(runtime)
    assert Path(scripts.dir).resolve() == MIGRATIONS_DIR
    assert scripts.get_heads() == [CHAIN[-1]]
    assert [r.revision for r in reversed(list(scripts.walk_revisions()))] == CHAIN
    # Development CLI retains its source-root cwd convention.
    monkeypatch.chdir(root)
    for cfg in (Config(str(root / "alembic.ini")),):
        scripts = ScriptDirectory.from_config(cfg)
        assert Path(scripts.dir).resolve() == MIGRATIONS_DIR
        assert scripts.get_heads() == [CHAIN[-1]]
        assert [r.revision for r in reversed(list(scripts.walk_revisions()))] == CHAIN
    assert (MIGRATIONS_DIR / "script.py.mako").is_file()
    assert not (root / "alembic").exists()


def test_runtime_config_preserves_percent_and_spaces(tmp_path, monkeypatch):
    target = tmp_path / "installed 100% resources"
    monkeypatch.setattr(schema, "MIGRATIONS_DIR", target)
    assert schema._migration_config().get_main_option("script_location") == str(target)


@pytest.mark.parametrize("name", ["source checkout", "source 100% checkout"])
def test_development_config_preserves_source_root_path_characters(tmp_path, monkeypatch, name):
    root = Path(__file__).resolve().parents[1]
    checkout = tmp_path / name
    checkout.mkdir()
    shutil.copyfile(root / "alembic.ini", checkout / "alembic.ini")
    shutil.copytree(MIGRATIONS_DIR, checkout / "order_ops_migrations")
    monkeypatch.chdir(checkout)
    scripts = ScriptDirectory.from_config(Config(str(checkout / "alembic.ini")))
    assert Path(scripts.dir).resolve() == checkout / "order_ops_migrations"
    assert scripts.get_heads() == [CHAIN[-1]]
    assert [r.revision for r in reversed(list(scripts.walk_revisions()))] == CHAIN
