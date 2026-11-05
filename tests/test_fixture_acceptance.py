"""The acceptance entry point is synthetic, repeatable, bounded and non-destructive."""

import json
import os
import signal
import subprocess
import sys

import pytest
from scripts import accept_fixture as runner


def invoke(output, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-B", str(runner.SCRIPT), "--output", str(output)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_two_fresh_runs_preserve_business_and_external_canaries(tmp_path):
    canary = tmp_path / "existing.db"
    canary.write_bytes(b"DO NOT OPEN OR REPLACE")
    (tmp_path / ".env").write_text(
        f"OOS_DATABASE_URL=sqlite:///{canary}\nOOS_LIVE_ENABLED=true\n"
        "OOS_LLM_API_KEY=FAKE_DOTENV_KEY\n",
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        OOS_DATABASE_URL=f"sqlite:///{canary}",
        OOS_LIVE_ENABLED="true",
        OOS_LLM_API_KEY="FAKE_ENV_KEY",
        HTTPS_PROXY="http://invalid.invalid:9",
    )
    operations = []
    for number in (1, 2):
        output = tmp_path / f"run-{number}"
        result = invoke(output, tmp_path, env)
        assert result.returncode == 0, result.stderr + (output / "run.log").read_text()
        assert not list(output.glob("*-ephemeral-*"))
        assert not list(output.rglob("*.db"))
        assert "PENDING_SUPPLIER" in (output / "SUMMARY.md").read_text()
        for name in ("stock", "uncut"):
            report = json.loads((output / f"{name}-result.json").read_text())
            audit = json.loads((output / f"{name}-audit.json").read_text())
            assert report["no_business_change_before_approval"]
            assert report["before"]["lines"] == report["preapproval"]["lines"]
            assert report["after"]["current_revision"] == 2
            assert report["commit"]["operation_id"] == report["replay"]["operation_id"]
            assert len(audit["committed_operations"]) == 1
            assert report["supplier_contacted"] is False
            assert sum(s.get("cli") == "worker-once" for s in report["steps"]) == (
                1 if name == "stock" else 2
            )
            assert len(audit["inputs"]) == (1 if name == "stock" else 2)
            lines = {row["sku_id"]: row for row in report["after"]["lines"]}
            assert lines["A-BLACK-M"]["shipped_qty"] == 60
            assert lines["C-BLACK-L"]["cut_qty"] == 120
            if name == "uncut":
                assert report["clarification"]["card"]["status"] == "NEEDS_CLARIFICATION"
                assert report["answer_text"] == "只改未开裁"
                assert lines["C-NAVY-L"]["ordered_qty"] == 180
                purchase = {row["id"]: row for row in report["stored_after"]["purchase_order"]}
                assert purchase["PUR-3002"]["change_state"] == "PENDING_SUPPLIER"
            else:
                assert lines["A-WHITE-M"]["ordered_qty"] == 40
                assert lines["A-BLACK-M"]["ordered_qty"] == 60
            operations.append(report["commit"]["operation_id"])
            assert (output / f"{name}-changes.csv").stat().st_size > 100
            assert (output / f"{name}-changes.xlsx").stat().st_size > 100
        for path in output.iterdir():
            if path.suffix != ".xlsx":
                assert "FAKE_ENV_KEY" not in path.read_text()
                assert "FAKE_DOTENV_KEY" not in path.read_text()
    assert len(set(operations)) == 4
    assert canary.read_bytes() == b"DO NOT OPEN OR REPLACE"


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "missing-parent"])
def test_refuses_unsafe_output(tmp_path, kind):
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "keep"
    marker.write_text("unchanged")
    output = tmp_path / "output"
    if kind == "directory":
        output = target
    elif kind == "file":
        output.write_text("unchanged")
    elif kind == "symlink":
        output.symlink_to(target, target_is_directory=True)
    else:
        output = tmp_path / "missing" / "output"
    result = invoke(output, tmp_path)
    assert result.returncode == 2
    assert marker.read_text() == "unchanged"
    assert list(target.iterdir()) == [marker]
    if kind == "file":
        assert output.read_text() == "unchanged"


def test_configured_child_ignores_dotenv_and_blocks_network(tmp_path):
    canary = tmp_path / "canary.db"
    canary.write_text("untouched")
    (tmp_path / ".env").write_text(f"OOS_DATABASE_URL=sqlite:///{canary}\nOOS_LIVE_ENABLED=true")
    database = tmp_path / "new.db"
    code = runner.child_code(
        "m['configure'](m['Path'](sys.argv[1])); "
        "from app.main import app; "
        "assert str(app.state.engine.url) == 'sqlite:///' + sys.argv[1]; "
        "assert not app.state.settings.live_enabled; "
        "assert app.state.settings.llm_api_key is None; "
        "import socket; socket.getaddrinfo('invalid.invalid',443)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(database)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "Network/server operation forbidden" in result.stderr
    assert canary.read_text() == "untouched"
    assert not database.exists()


def test_plan_ambiguity_fails_closed():
    with pytest.raises(runner.AcceptanceError, match="exactly one"):
        runner.only_plan({"plans": [{"plan_id": "old"}, {"plan_id": "new"}]})
    assert runner.only_plan({"plans": [{"plan_id": "old"}, {"plan_id": "new"}]}, ["old"]) == "new"


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_failure_retains_output_without_success_summary(tmp_path, monkeypatch, capsys, failure):
    def fail(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("synthetic controller", 180)
        return 7

    monkeypatch.setattr(runner, "run_controller", fail)
    output = tmp_path / "failure"
    assert runner.main(["--output", str(output)]) == 1
    assert (output / "run.log").exists()
    assert not (output / "SUMMARY.md").exists()
    assert "incomplete" in capsys.readouterr().err


def test_timeout_kills_controller_and_descendant(tmp_path):
    marker = tmp_path / "late-write"
    child = "import time,pathlib; time.sleep(2); pathlib.Path('late-write').write_text('bad')"
    code = (
        f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "time.sleep(30)"
    )
    old_handler = signal.getsignal(signal.SIGTERM)
    with (tmp_path / "log").open("w") as log:
        with pytest.raises(subprocess.TimeoutExpired):
            runner.run_controller([sys.executable, "-c", code], tmp_path, log, timeout=0.5)
    assert signal.getsignal(signal.SIGTERM) == old_handler
    # Wait beyond the child's planned write, without contacting anything.
    subprocess.run([sys.executable, "-c", "import time; time.sleep(2.2)"], check=True)
    assert not marker.exists()


def source_fixture(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    for directory in runner.RUNTIME_DIRS:
        (root / directory).mkdir()
        (root / directory / "module.py").write_text("# synthetic code\n")
    for relative in runner.SOURCE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic input\n")
    monkeypatch.setattr(runner, "ROOT", root)
    return root


def test_fingerprint_excludes_arbitrary_docs_outputs_and_real_dotenv(tmp_path, monkeypatch):
    root = source_fixture(tmp_path, monkeypatch)
    before = runner.source_identity()["files"]
    docs = root / "docs" / "old-output"
    docs.mkdir(parents=True)
    (docs / "SOURCE.json").write_text('{"old": "output"}')
    (root / ".env").write_text("FAKE SECRET")
    (docs / "linked.json").symlink_to(root / ".env")
    assert runner.source_identity()["files"] == before
    assert "scripts/accept_fixture.py" in before
    assert not any(name.startswith("docs/") for name in before)


@pytest.mark.parametrize(
    "relative",
    [
        "app/module.py",
        "requirements.lock",
        "scripts",
        "app",
        "order_ops_migrations",
        "order_ops_migrations/script.py.mako",
    ],
)
def test_fingerprint_rejects_symlink_files_and_ancestors(tmp_path, monkeypatch, relative):
    root = source_fixture(tmp_path, monkeypatch)
    path = root / relative
    moved = tmp_path / "fake-target"
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(runner.AcceptanceError, match="[Ss]ymlink|Unsafe runtime"):
        runner.source_identity()


@pytest.mark.parametrize("directory", runner.RUNTIME_DIRS)
def test_output_inside_runtime_source_refused_before_creation(tmp_path, monkeypatch, directory):
    root = source_fixture(tmp_path, monkeypatch)
    output = root / directory / "new-output"
    with pytest.raises(SystemExit) as exc:
        runner.main(["--output", str(output)])
    assert exc.value.code == 2
    assert not output.exists()
