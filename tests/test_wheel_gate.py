"""Fast orchestration/inventory regressions; installed smoke is make check-wheel."""

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "wheel_gate_test", ROOT / "scripts/check_wheel.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    return module


def test_copy_inputs_excludes_checkout_artifacts(gate, tmp_path):
    target = tmp_path / "copy"
    gate.copy_inputs(ROOT, target)
    assert (target / "order_ops_migrations/script.py.mako").is_file()
    assert (target / "requirements-build.lock").is_file()
    assert not (target / "tests").exists()
    assert not (target / ".env").exists()
    assert not list(target.rglob("*.pyc"))
    assert not list(target.rglob("*.db"))


@pytest.mark.parametrize("names", [[], ["old.whl"], ["order_ops_superagent-1.whl", "stale.whl"]])
def test_selection_rejects_empty_stale_multiple(gate, tmp_path, names):
    for name in names:
        (tmp_path / name).touch()
    with pytest.raises(ValueError, match="exactly one"):
        gate.select_wheel(tmp_path)


def test_selection_accepts_only_project_wheel(gate, tmp_path):
    wheel = tmp_path / "order_ops_superagent-1-py3-none-any.whl"
    wheel.touch()
    assert gate.select_wheel(tmp_path) == wheel


@pytest.mark.parametrize(
    "missing",
    ["order_ops_migrations/script.py.mako", "order_ops_migrations/versions/0001_initial.py"],
)
def test_inventory_rejects_missing_migration(gate, tmp_path, missing):
    # Derive an actual revision filename rather than coupling to revision spelling.
    if missing.endswith("0001_initial.py"):
        missing = (
            next((ROOT / "order_ops_migrations/versions").glob("*.py")).relative_to(ROOT).as_posix()
        )
    wheel = tmp_path / "corrupt.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for directory in ["order_ops_migrations", "app/web/templates", "app/web/static"]:
            for path in (ROOT / directory).rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    name = path.relative_to(ROOT).as_posix()
                    if name != missing:
                        archive.write(path, name)
    with pytest.raises(KeyError):
        gate.check(wheel)


def test_copy_rejects_symlink(gate, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "MANIFEST.in").write_text("include secret\n")
    (source / "pyproject.toml").write_text("")
    (source / "secret").symlink_to(ROOT / ".env.example")
    with pytest.raises(ValueError, match="Unsafe packaging input"):
        gate.copy_inputs(source, tmp_path / "copy")


def test_copy_rejects_unknown_manifest_directive(gate, tmp_path):
    (tmp_path / "MANIFEST.in").write_text("recursive-include app *\n")
    with pytest.raises(ValueError, match="MANIFEST"):
        gate.copy_inputs(tmp_path, tmp_path / "copy")


def test_copy_skips_hidden_secrets_and_rejects_database(gate, tmp_path):
    source = tmp_path / "source"
    (source / "app").mkdir(parents=True)
    (source / "MANIFEST.in").write_text("")
    (source / "pyproject.toml").write_text("")
    (source / "app/.env").write_text("SECRET=synthetic")
    gate.copy_inputs(source, tmp_path / "clean")
    assert not (tmp_path / "clean/app/.env").exists()
    (source / "app/local.sqlite").write_text("synthetic")
    with pytest.raises(ValueError, match="Unsupported packaging input"):
        gate.copy_inputs(source, tmp_path / "rejected")
