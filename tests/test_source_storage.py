"""Real filesystem safety and failed writes do not create successful upload receipts."""

from stat import S_IMODE

import pytest
from app.config import Settings
from app.errors import AppError
from app.main import create_app
from app.services import source_storage
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tests import test_link_audit as support
from tests.test_bounded_sheet_durable import book
from tests.test_persistent_source_http import start, upload

seeded = support.seeded


@pytest.mark.parametrize("value", ["relative-private-canary", "/", "/tmp/../private-canary"])
def test_persistent_root_must_be_explicit_absolute_without_parent_jumps(value):
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, source_storage_dir=value)
    assert value not in str(error.value) or value == "/"
    assert Settings(_env_file=None, source_storage_dir="").source_storage_dir is None


@pytest.mark.parametrize(
    "kind", ["public", "symlink", "ancestor-symlink", "file", "missing-parent"]
)
def test_unsafe_root_rejected_without_chmod_or_creating_through_symlink(tmp_path, kind):
    root = tmp_path / "private-canary"
    if kind == "public":
        root.mkdir(mode=0o700)
        root.chmod(0o755)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    elif kind == "ancestor-symlink":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        alias = tmp_path / "alias"
        alias.symlink_to(target, target_is_directory=True)
        root = alias / "new-leaf"
    elif kind == "file":
        root.write_bytes(b"unchanged")
    else:
        root = tmp_path / "unprovisioned-parent" / "leaf"
    with pytest.raises(AppError) as error:
        source_storage.prepare_source_storage(root)
    assert error.value.code == "SOURCE_STORAGE_UNAVAILABLE"
    assert "private-canary" not in str(error.value) and str(tmp_path) not in str(error.value)
    if kind == "public":
        assert S_IMODE(root.stat().st_mode) == 0o755
    if kind == "ancestor-symlink":
        assert not (target / "new-leaf").exists()
    if kind == "file":
        assert root.read_bytes() == b"unchanged"


@pytest.mark.parametrize("failure_at", [1, 2], ids=["file-fsync", "directory-fsync"])
def test_failed_durable_write_rolls_back_and_same_upload_key_can_recover(
    seeded, database_url, tmp_path, monkeypatch, failure_at
):
    root = tmp_path / "originals"
    app = create_app(Settings(_env_file=None, database_url=database_url, source_storage_dir=root))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        task = start(client)
        content = book([["华东服饰", "A", "白", "M", 30]])
        before = support.dump(database_url)
        real = source_storage.fsync
        calls = 0

        def fail(descriptor):
            nonlocal calls
            calls += 1
            if calls == failure_at:
                raise OSError("private-fsync-canary")
            return real(descriptor)

        with monkeypatch.context() as patch:
            patch.setattr(source_storage, "fsync", fail)
            response = upload(client, task, content, key="durable-fault")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SOURCE_STORAGE_UNAVAILABLE"
        assert response.json()["error"]["retryable"]
        assert "private-fsync-canary" not in response.text and str(root) not in response.text
        assert support.dump(database_url) == before and not list(root.iterdir())
        recovered = upload(client, task, content, key="durable-fault")
        assert recovered.status_code == 200
        before = support.dump(database_url)
        assert (
            upload(client, task, content, key="durable-fault").json()["source_id"]
            == recovered.json()["source_id"]
        )
        assert support.dump(database_url) == before and len(list(root.iterdir())) == 1


def test_storage_permission_loss_fails_closed_and_does_not_silently_fall_back(
    seeded, database_url, tmp_path
):
    root = tmp_path / "originals"
    app = create_app(Settings(_env_file=None, database_url=database_url, source_storage_dir=root))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        task = start(client)
        content = book([["华东服饰", "A", "白", "M", 30]])
        before = support.dump(database_url)
        root.chmod(0o755)
        rejected = upload(client, task, content, key="same-key")
        assert rejected.status_code == 503 and not list(root.iterdir())
        assert support.dump(database_url) == before
        root.chmod(0o700)
        recovered = upload(client, task, content, key="same-key")
        assert recovered.status_code == 200
        assert (root / recovered.json()["source_id"]).read_bytes() == content


def test_existing_name_or_symlink_is_never_overwritten(tmp_path):
    root = tmp_path / "originals"
    source_storage.prepare_source_storage(root)
    target = root / "SRC-synthetic"
    with source_storage.new_original(root, target.name) as (_, stream):
        stream.write(b"retained")
    for symlink in [False, True]:
        if symlink:
            target.rename(root / "retained")
            target.symlink_to(root / "retained")
        with pytest.raises(AppError):
            with source_storage.new_original(root, target.name) as (_, stream):
                stream.write(b"replacement")
        assert target.read_bytes() == b"retained"


def test_bad_storage_startup_is_sanitized_and_does_not_create_database(tmp_path):
    root = tmp_path / "private-path-canary"
    root.mkdir(mode=0o700)
    root.chmod(0o755)
    database = tmp_path / "not-created.db"
    with pytest.raises(RuntimeError) as error:
        create_app(
            Settings(_env_file=None, database_url=f"sqlite:///{database}", source_storage_dir=root)
        )
    assert str(root) not in str(error.value)
    assert not database.exists()
