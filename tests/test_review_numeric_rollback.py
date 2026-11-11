"""Rollback copy keeps originals and invalidates image acknowledgements."""

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
import scripts.prepare_numeric_rollback_copy as rollback
from app.config import Settings
from app.db.models import ReviewWorkspace
from app.errors import AppError
from app.main import create_app
from app.review import assets
from fastapi.testclient import TestClient
from scripts.prepare_numeric_rollback_copy import prepare
from tests.test_review_numeric_safety import image_doc
from tests.test_review_sources import adopt, start, upload


def test_new_storage_is_old_schema_compatible_and_public_shape_is_preserved(
    monkeypatch, engine, database_url, session_factory
):
    reader = assets.read_document
    doc = image_doc("数量 42 件")
    monkeypatch.setattr(
        assets,
        "read_document",
        lambda filename, *args, **kwargs: (
            (doc, b"preview") if filename.endswith(".png") else reader(filename, *args, **kwargs)
        ),
    )
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        for filename, contents in [("story.txt", b"quantity 42"), ("story.png", b"image")]:
            result = upload(client, start(client), filename, contents).json()
            item = result["review"]
            public = item["data"]["assets"][0]["document"]
            assert all(key in public for key in assets.NUMERIC_FIELDS)
            with session_factory() as session:
                raw = session.get(ReviewWorkspace, item["id"]).data["assets"][0]
                assert not set(assets.NUMERIC_FIELDS).intersection(raw["document"])
                if filename.endswith(".png"):
                    assert raw["ocr_numeric"]["ocr_numeric_check"] == "available"
                    assert assets.effective_document(raw).ocr_numeric_check == "available"
                else:
                    assert "ocr_numeric" not in raw


def test_old_embedded_metadata_remains_readable_and_conflicts_fail_closed():
    doc = image_doc("数量 42 件")
    embedded = {"document": doc.model_dump(mode="json")}
    assert assets.effective_document(embedded).ocr_numeric_check == "available"
    conflicting = deepcopy(embedded)
    conflicting["ocr_numeric"] = {key: embedded["document"][key] for key in assets.NUMERIC_FIELDS}
    conflicting["ocr_numeric"]["ocr_numeric_check"] = "unavailable"
    with pytest.raises(AppError) as failure:
        assets.effective_document(conflicting)
    assert failure.value.code == "OCR_NUMERIC_REVIEW_REQUIRED"
    conflicting["corrected_text"] = "人工对照原图后的全文"
    assert assets.effective_document(conflicting).kind == "human_transcription"


def test_copy_converts_302_records_without_changing_source(
    monkeypatch, engine, database_url, session_factory, tmp_path
):
    monkeypatch.setattr(
        assets, "read_document", lambda *args, **kwargs: (image_doc("数量 42 件"), b"preview")
    )
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        result = upload(client, start(client), "story.png", b"image").json()
        item, sid = result["review"], result["source_id"]
        assert adopt(client, item, sid, numeric_fields_checked=True).status_code == 200
        rid = item["id"]
    with session_factory() as session, session.begin():
        row = session.get(ReviewWorkspace, rid)
        data = deepcopy(row.data)
        asset = data["assets"][0]
        asset["document"].update(asset.pop("ocr_numeric"))  # 302's persisted shape
        row.data = data
    with session_factory() as session:
        before = deepcopy(session.get(ReviewWorkspace, rid).data)
    source = Path(database_url.removeprefix("sqlite:///"))
    target = tmp_path / "rollback.sqlite"
    counts = prepare(source, target)
    assert counts == {
        "workspaces": 1,
        "documents_moved": 1,
        "image_reviews_reset": 1,
        "originals_copied": 0,
        "previews_copied": 0,
    }
    with session_factory() as session:
        assert session.get(ReviewWorkspace, rid).data == before
    with sqlite3.connect(target) as connection:
        copied = json.loads(connection.execute("SELECT data FROM review_workspace").fetchone()[0])
    asset = copied["assets"][0]
    assert not set(assets.NUMERIC_FIELDS).intersection(asset["document"])
    assert asset["ocr_numeric"]["ocr_numeric_check"] == "available"
    assert asset["numeric_fields_checked"] is False
    assert asset["ocr_numeric_rollback"] == {
        "previous_checked_present": True,
        "previous_checked": True,
    }
    assert copied["history"] == before["history"]
    assert assets.effective_document(asset).ocr_numeric_check == "available"
    with pytest.raises(AppError) as failure:
        assets.model_lines(copied, verify=False)
    assert failure.value.code == "OCR_NUMERIC_REVIEW_REQUIRED"


def test_copy_refuses_existing_destination_and_conflicting_metadata(
    monkeypatch, engine, database_url, session_factory, tmp_path
):
    monkeypatch.setattr(
        assets, "read_document", lambda *args, **kwargs: (image_doc("数量 42 件"), b"preview")
    )
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        result = upload(client, start(client), "story.png", b"image").json()
        rid = result["review"]["id"]
    with session_factory() as session, session.begin():
        row = session.get(ReviewWorkspace, rid)
        data = deepcopy(row.data)
        asset = data["assets"][0]
        asset["document"].update(asset["ocr_numeric"])
        asset["document"]["ocr_numeric_check"] = "unavailable"
        row.data = data
    source = Path(database_url.removeprefix("sqlite:///"))
    existing = tmp_path / "existing.sqlite"
    existing.write_bytes(b"do not overwrite")
    with pytest.raises(FileExistsError):
        prepare(source, existing)
    assert existing.read_bytes() == b"do not overwrite"
    with pytest.raises(ValueError, match="conflicting numeric metadata"):
        prepare(source, tmp_path / "conflict.sqlite")
    assert not (tmp_path / "conflict.sqlite").exists()
    with session_factory() as session:
        assert session.get(ReviewWorkspace, rid).data == data


def test_copy_moves_original_and_preview_without_touching_source(
    monkeypatch, engine, database_url, session_factory, tmp_path
):
    monkeypatch.setattr(
        assets, "read_document", lambda *args, **kwargs: (image_doc("数量 42 件"), b"preview")
    )
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        result = upload(client, start(client), "story.png", b"image").json()
        rid = result["review"]["id"]
    with session_factory() as session:
        before = deepcopy(session.get(ReviewWorkspace, rid).data)
    source_asset = before["assets"][0]
    originals = tmp_path / "relocated-originals"
    originals.mkdir(mode=0o700)
    target = tmp_path / "relocated.sqlite"
    counts = prepare(Path(database_url.removeprefix("sqlite:///")), target, originals)
    assert counts["originals_copied"] == counts["previews_copied"] == 1
    with sqlite3.connect(target) as connection:
        copied = json.loads(connection.execute("SELECT data FROM review_workspace").fetchone()[0])
    moved = copied["assets"][0]
    assert Path(moved["path"]).parent == Path(moved["preview_path"]).parent == originals
    assert assets.original(moved) == b"image"
    assert assets.original(moved, preview=True) == b"preview"
    assert assets.original(source_asset) == b"image"
    assert assets.original(source_asset, preview=True) == b"preview"
    with session_factory() as session:
        assert session.get(ReviewWorkspace, rid).data == before


def test_copy_rejects_tampered_source_without_overwriting_any_existing_file(
    monkeypatch, engine, database_url, session_factory, tmp_path
):
    monkeypatch.setattr(
        assets, "read_document", lambda *args, **kwargs: (image_doc("数量 42 件"), b"preview")
    )
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        result = upload(client, start(client), "story.png", b"image").json()
        rid = result["review"]["id"]
    with session_factory() as session, session.begin():
        row = session.get(ReviewWorkspace, rid)
        data = deepcopy(row.data)
        data["assets"][0]["sha256"] = "0" * 64
        row.data = data
    destination = tmp_path / "empty-originals"
    destination.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="failed its saved size or hash"):
        prepare(Path(database_url.removeprefix("sqlite:///")), tmp_path / "bad.sqlite", destination)
    assert not (tmp_path / "bad.sqlite").exists()
    assert list(destination.iterdir()) == []
    with session_factory() as session:
        assert session.get(ReviewWorkspace, rid).data == data
    (destination / "existing").write_bytes(b"do not overwrite")
    with pytest.raises(ValueError, match="must be empty"):
        prepare(
            Path(database_url.removeprefix("sqlite:///")), tmp_path / "other.sqlite", destination
        )
    assert (destination / "existing").read_bytes() == b"do not overwrite"


def _confirmed_image(monkeypatch, database_url, session_factory):
    monkeypatch.setattr(
        assets, "read_document", lambda *args, **kwargs: (image_doc("数量 42 件"), b"preview")
    )
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        result = upload(client, start(client), "story.png", b"image").json()
        item, sid = result["review"], result["source_id"]
        assert adopt(client, item, sid, numeric_fields_checked=True).status_code == 200
    with session_factory() as session:
        return item["id"], deepcopy(session.get(ReviewWorkspace, item["id"]).data)


def _read_workspace(db):
    with sqlite3.connect(db) as connection:
        return json.loads(connection.execute("SELECT data FROM review_workspace").fetchone()[0])


def test_repeat_rollback_retains_all_attestations_and_rechecks_each_round(
    monkeypatch, engine, database_url, session_factory, tmp_path
):
    rid, original = _confirmed_image(monkeypatch, database_url, session_factory)
    source = Path(database_url.removeprefix("sqlite:///"))
    first_dir = tmp_path / "first-originals"
    first_dir.mkdir(mode=0o700)
    first_db = tmp_path / "first.sqlite"
    prepare(source, first_db, first_dir)
    first = _read_workspace(first_db)
    assert first["assets"][0]["numeric_fields_checked"] is False
    assert first["assets"][0]["ocr_numeric_rollback_history"] == [
        {"previous_checked_present": True, "previous_checked": True}
    ]
    # The upgraded application rechecks the image before the next rollback.
    first["assets"][0]["numeric_fields_checked"] = True
    with sqlite3.connect(first_db) as connection:
        connection.execute(
            "UPDATE review_workspace SET data = ? WHERE id = ?", (json.dumps(first), rid)
        )
    second_dir = tmp_path / "second-originals"
    second_dir.mkdir(mode=0o700)
    second_db = tmp_path / "second.sqlite"
    prepare(first_db, second_db, second_dir)
    second = _read_workspace(second_db)
    image = second["assets"][0]
    assert image["numeric_fields_checked"] is False
    assert image["ocr_numeric_rollback_history"] == [
        {"previous_checked_present": True, "previous_checked": True},
        {"previous_checked_present": True, "previous_checked": True},
    ]
    assert image["ocr_numeric_rollback"] == image["ocr_numeric_rollback_history"][-1]
    assert assets.original(image) == b"image" and assets.original(image, preview=True) == b"preview"
    assert _read_workspace(first_db) == first
    with session_factory() as session:
        assert session.get(ReviewWorkspace, rid).data == original
    third_dir = tmp_path / "third-originals"
    third_dir.mkdir(mode=0o700)
    third_db = tmp_path / "third.sqlite"
    prepare(second_db, third_db, third_dir)
    history = _read_workspace(third_db)["assets"][0]["ocr_numeric_rollback_history"]
    assert history[:2] == image["ocr_numeric_rollback_history"]
    assert history[-1] == {"previous_checked_present": True, "previous_checked": False}


def test_existing_single_marker_is_preserved_on_next_rollback(
    monkeypatch, engine, database_url, session_factory, tmp_path
):
    rid, _ = _confirmed_image(monkeypatch, database_url, session_factory)
    old_marker = {
        "previous_checked_present": False,
        "previous_checked": None,
        "preserved_legacy_detail": "unchanged",
    }
    with session_factory() as session, session.begin():
        row = session.get(ReviewWorkspace, rid)
        data = deepcopy(row.data)
        data["assets"][0]["ocr_numeric_rollback"] = old_marker
        row.data = data
    db = tmp_path / "next.sqlite"
    prepare(Path(database_url.removeprefix("sqlite:///")), db)
    history = _read_workspace(db)["assets"][0]["ocr_numeric_rollback_history"]
    assert history == [old_marker, {"previous_checked_present": True, "previous_checked": True}]


def test_invalid_later_asset_fails_preflight_before_staging(
    monkeypatch, engine, database_url, session_factory, tmp_path
):
    rid, _ = _confirmed_image(monkeypatch, database_url, session_factory)
    with session_factory() as session, session.begin():
        row = session.get(ReviewWorkspace, rid)
        data = deepcopy(row.data)
        invalid = deepcopy(data["assets"][0])
        invalid["ocr_numeric_rollback"] = {"previous_checked_present": "yes"}
        data["assets"].append(invalid)
        row.data = data
    destination = tmp_path / "originals-target"
    destination.mkdir(mode=0o700)
    target = tmp_path / "must-not-exist.sqlite"
    monkeypatch.setattr(
        rollback, "_write_stage", lambda *args: pytest.fail("preflight wrote output")
    )
    with pytest.raises(ValueError, match="invalid rollback audit record"):
        prepare(Path(database_url.removeprefix("sqlite:///")), target, destination)
    assert not target.exists() and list(destination.iterdir()) == []
    assert not list(tmp_path.glob(".oos-*"))
    with session_factory() as session:
        assert session.get(ReviewWorkspace, rid).data == data


@pytest.mark.parametrize("failure_point", ["staging", "publishing"])
def test_io_failure_removes_only_new_outputs(
    monkeypatch, engine, database_url, session_factory, tmp_path, failure_point
):
    rid, before = _confirmed_image(monkeypatch, database_url, session_factory)
    destination = tmp_path / "originals-target"
    destination.mkdir(mode=0o700)
    target = tmp_path / "must-not-exist.sqlite"
    calls = 0
    if failure_point == "staging":
        real = rollback._write_stage

        def fail_on_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated staging failure")
            return real(*args)

        monkeypatch.setattr(rollback, "_write_stage", fail_on_second)
    else:
        real = rollback._publish

        def fail_on_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated publication failure")
            return real(*args)

        monkeypatch.setattr(rollback, "_publish", fail_on_second)
    with pytest.raises(OSError, match="simulated"):
        prepare(Path(database_url.removeprefix("sqlite:///")), target, destination)
    assert calls == 2 and not target.exists() and list(destination.iterdir()) == []
    assert not list(tmp_path.glob(".oos-*"))
    with session_factory() as session:
        assert session.get(ReviewWorkspace, rid).data == before


def test_concurrent_destination_is_not_overwritten_or_deleted(
    monkeypatch, engine, database_url, session_factory, tmp_path
):
    _confirmed_image(monkeypatch, database_url, session_factory)
    destination = tmp_path / "originals-target"
    destination.mkdir(mode=0o700)
    target = tmp_path / "raced.sqlite"
    real = rollback._publish

    def create_competing_file(stage, output, published):
        if output == target:
            target.write_bytes(b"another writer's file")
        return real(stage, output, published)

    monkeypatch.setattr(rollback, "_publish", create_competing_file)
    with pytest.raises(FileExistsError):
        prepare(Path(database_url.removeprefix("sqlite:///")), target, destination)
    assert target.read_bytes() == b"another writer's file"
    assert list(destination.iterdir()) == []


def test_uncheckpointed_wal_is_included_without_writing_source(tmp_path):
    source = tmp_path / "source-wal.sqlite"
    target = tmp_path / "copy-wal.sqlite"
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE review_workspace (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute(
            "INSERT INTO review_workspace (id, data) VALUES (?, ?)",
            ("wal-only", json.dumps({"assets": [], "value": "committed-in-wal"})),
        )
        writer.commit()
        wal = Path(str(source) + "-wal")
        shm = Path(str(source) + "-shm")
        assert wal.stat().st_size > 0 and shm.exists()
        before = {path: path.read_bytes() for path in (source, wal, shm)}
        prepare(source, target)
        assert _read_workspace(target)["value"] == "committed-in-wal"
        assert {path: path.read_bytes() for path in (source, wal, shm)} == before
        assert not list(tmp_path.glob(".oos-*"))
    finally:
        writer.close()


def test_source_rollback_journal_is_rejected_without_creating_outputs(tmp_path):
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE review_workspace (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
    journal = Path(str(source) + "-journal")
    journal.write_bytes(b"test-only journal placeholder")
    target = tmp_path / "must-not-exist.sqlite"
    with pytest.raises(ValueError, match="rollback journal exists"):
        prepare(source, target)
    assert not target.exists() and journal.read_bytes() == b"test-only journal placeholder"
    assert not list(tmp_path.glob(".oos-*"))
