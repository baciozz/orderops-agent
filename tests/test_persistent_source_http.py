"""Local persistent originals survive process/temp restarts and fail closed on loss."""

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from stat import S_IMODE

from app.db.models import InputSource, ModelUsage, Operation
from sqlalchemy import func, select
from tests import test_demo_clarification_journeys as journey
from tests import test_link_audit as support
from tests.test_bounded_sheet_durable import book
from tests.test_http_storage_failure import real_http

seeded = support.seeded


def start(client):
    entry = journey.Page(client.get("/ui/demo/new-stock-sheet").text)
    response = client.post(
        "/ui/demo/new-stock-sheet",
        data={"text": "新建现货订单（表格）", "client_event_key": entry.field("client_event_key")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    task = response.headers["location"].split("/")[-1]
    journey.worker(client, task)
    return task


def upload(client, task, content, key="original"):
    return client.post(
        "/sources",
        data={"task_id": task, "client_upload_key": key},
        files={"file": ("order.xlsx", content)},
    )


def original(factory, source_id):
    with factory() as session:
        return Path(session.get(InputSource, source_id).storage_ref)


def approval(client, task):
    _, page = journey.page(client, task)
    actions = page.element("plan-actions")
    return f"/plans/{actions['data-plan-id']}", {
        "plan_hash": actions["data-plan-hash"],
        "expected_intent_revision": int(actions["data-intent-revision"]),
        "acknowledge_duplicate": True,
    }


def test_persistent_sheet_survives_temp_cleanup_and_separate_process_replays(
    seeded, database_url, tmp_path
):
    root, temp = tmp_path / "originals", tmp_path / "old-temp"
    temp.mkdir()
    env = {"OOS_SOURCE_STORAGE_DIR": str(root), "TMPDIR": str(temp)}
    content = book([["华东服饰", "A", "白", "M", 30]])
    with real_http(database_url, tmp_path, env_overrides=env) as (client, _):
        task = start(client)
        response = upload(client, task, content)
        assert response.status_code == 200
        source_id = response.json()["source_id"]
        path = original(seeded, source_id)
        assert path == root / source_id
        assert S_IMODE(root.stat().st_mode) == 0o700
        assert S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_bytes() == content
        journey.answer(client, task, "按附件新建订单", [source_id])
        assert journey.worker(client, task)["approval"]["approvable"]
        before_restart = support.dump(database_url)
    shutil.rmtree(temp)  # Only this test's disposable temp dir, never originals.
    with real_http(database_url, tmp_path, env_overrides=env) as (client, _):
        assert support.dump(database_url) == before_restart
        assert client.get(f"/tasks/{task}/view").json()["approval"]["approvable"]
        assert upload(client, task, content).json()["source_id"] == source_id
        # Independent server/connection replays the same durable identity.
        other = tmp_path / "other-process"
        other.mkdir()
        with real_http(database_url, other, env_overrides=env) as (second, _):
            with ThreadPoolExecutor(max_workers=2) as pool:
                replies = list(pool.map(lambda c: upload(c, task, content), [client, second]))
            assert all(r.status_code == 200 and r.json()["source_id"] == source_id for r in replies)
        assert list(root.iterdir()) == [path]
        assert support.dump(database_url) == before_restart
        result = journey.approve_commit_replay(client, task, database_url, seeded)
        assert result["revision"] == 1
        with seeded() as session:
            assert session.scalar(select(func.count()).select_from(Operation)) == 1
            assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0
    assert path.read_bytes() == content  # Shutdown does not remove retained evidence.


def test_missing_or_changed_persistent_original_cannot_be_replaced_by_upload_replay(
    seeded, database_url, tmp_path
):
    root = tmp_path / "originals"
    env = {"OOS_SOURCE_STORAGE_DIR": str(root)}
    content = book([["华东服饰", "A", "白", "M", 30]])
    with real_http(database_url, tmp_path, env_overrides=env) as (client, _):
        task = start(client)
        receipt = upload(client, task, content).json()
        source_id = receipt["source_id"]
        path = original(seeded, source_id)
        assert path.parent == root
        journey.answer(client, task, "按附件新建订单", [source_id])
        journey.worker(client, task)
        url, body = approval(client, task)
        before = support.dump(database_url)
        backup = tmp_path / "trusted-original"
        path.rename(backup)
        for changed in [False, True]:
            if changed:
                path.write_bytes(b"synthetic damaged bytes")
            failed = upload(client, task, content)
            assert failed.status_code == 409
            assert failed.json()["error"]["code"] == "SOURCE_UNAVAILABLE"
            denied = client.post(url + "/approve", json=body)
            assert denied.status_code == 409
            assert denied.json()["error"]["code"] == "SHEET_CONTEXT_INVALID"
            assert not client.get(f"/tasks/{task}/view").json()["approval"]["approvable"]
            assert support.dump(database_url) == before
            assert str(root) not in failed.text + denied.text
        path.unlink()  # Remove only the known injected corruption before trusted restore.
        backup.rename(path)
        assert upload(client, task, content).json()["source_id"] == source_id
        assert support.dump(database_url) == before
        result = journey.approve_commit_replay(client, task, database_url, seeded)
        before = support.dump(database_url)
        # Do not re-execute business when an original disappears after commit.
        path.rename(backup)
        assert (
            client.get(f"/tasks/{task}/view").json()["result"]["operation_id"]
            == result["operation_id"]
        )
        assert support.dump(database_url) == before
        backup.rename(path)


def test_enabling_persistent_directory_does_not_move_or_rebind_legacy_sources(
    seeded, database_url, tmp_path
):
    content = book([["华东服饰", "A", "白", "M", 30]])
    temp = tmp_path / "legacy-temp"
    temp.mkdir()
    with real_http(database_url, tmp_path, env_overrides={"TMPDIR": str(temp)}) as (client, _):
        task = start(client)
        source_id = upload(client, task, content).json()["source_id"]
        old_path = original(seeded, source_id)
        assert old_path.parent.parent == temp
        before = support.dump(database_url)
    root = tmp_path / "persistent"
    with real_http(database_url, tmp_path, env_overrides={"OOS_SOURCE_STORAGE_DIR": str(root)}) as (
        client,
        _,
    ):
        assert root.is_dir()
        assert upload(client, task, content).json()["source_id"] == source_id
        assert original(seeded, source_id) == old_path
        assert not list(root.iterdir()) and support.dump(database_url) == before
        backup = tmp_path / "legacy-trusted"
        old_path.rename(backup)
        # A matching filename in the new directory cannot silently rebind an old row.
        (root / source_id).write_bytes(content)
        response = upload(client, task, content)
        assert (
            response.status_code == 409 and response.json()["error"]["code"] == "SOURCE_UNAVAILABLE"
        )
        assert original(seeded, source_id) == old_path and support.dump(database_url) == before
        backup.rename(old_path)
        assert upload(client, task, content).json()["source_id"] == source_id
        assert support.dump(database_url) == before
