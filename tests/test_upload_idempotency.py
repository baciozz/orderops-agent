"""Task-scoped upload delivery identities without new business side effects."""

import asyncio
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from alembic import command
from app.api import sources
from app.config import Settings
from app.db.models import InputSource, SourceSpan, SourceUploadReceipt, Task, TaskSource
from app.db.schema import _migration_config
from app.db.session import begin_immediate
from app.main import create_app
from app.orchestration.inbox import accept_input
from fastapi.testclient import TestClient
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def upload_env(session_factory, database_url, tmp_path):
    _, task_id, _ = accept_input(
        session_factory, thread_id="upload-test", client_event_key="one", text="Evidence only"
    )
    app = create_app(Settings(_env_file=None, database_url=database_url))
    root = tmp_path / "uploads"
    root.mkdir(mode=0o700)
    app.state.source_storage_dir = root
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, app, task_id


def send(client, task_id, *, key="upload-key", name="evidence.txt", content=b"one\ntwo", **kw):
    data = {"task_id": task_id}
    if key is not None:
        data["client_upload_key"] = key
    return client.post("/sources", data=data, files={"file": (name, content)}, **kw)


def counts(factory):
    with factory() as session:
        return {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (InputSource, SourceSpan, TaskSource, SourceUploadReceipt)
        }


def snapshot(engine):
    with engine.connect() as connection:
        return {
            name: connection.execute(text(f'SELECT * FROM "{name}"')).fetchall()
            for name in inspect(connection).get_table_names()
            if name not in {"input_source", "source_span", "task_source", "source_upload_receipt"}
        }


def test_retry_returns_same_source_current_trace_without_parsing_or_business_writes(
    upload_env, session_factory, engine, monkeypatch
):
    client, app, task_id = upload_env
    before = snapshot(engine)
    first = send(client, task_id, headers={"x-request-id": "first", "x-correlation-id": "corr-1"})
    assert first.status_code == 200
    file_before = list(app.state.source_storage_dir.iterdir())
    monkeypatch.setattr(sources, "split_source", lambda *args: pytest.fail("Replay re-parsed"))
    replay = send(client, task_id, headers={"x-request-id": "retry", "x-correlation-id": "corr-2"})
    assert replay.status_code == 200
    expected = {**first.json(), "request_id": "retry", "correlation_id": "corr-2"}
    assert replay.json() == expected
    assert replay.headers["x-request-id"] == "retry"
    assert replay.headers["x-correlation-id"] == "corr-2"
    assert counts(session_factory) == {
        "input_source": 1,
        "source_span": 2,
        "task_source": 1,
        "source_upload_receipt": 1,
    }
    assert list(app.state.source_storage_dir.iterdir()) == file_before
    assert snapshot(engine) == before


@pytest.mark.parametrize("status", ["CANCELLED", "SUCCEEDED", "FAILED", "RUNNING"])
def test_replay_works_at_cap_and_after_task_status_changes(upload_env, session_factory, status):
    client, _, task_id = upload_env
    first = send(client, task_id)
    assert send(client, task_id, key="second").status_code == 200
    assert send(client, task_id, key="third").status_code == 200
    with session_factory() as session, begin_immediate(session):
        session.get(Task, task_id).status = status
    replay = send(client, task_id)
    assert replay.status_code == 200
    assert replay.json()["source_id"] == first.json()["source_id"]
    assert send(client, task_id, key="fourth").json()["error"]["code"] == "UPLOAD_LIMIT"
    assert counts(session_factory)["input_source"] == 3


@pytest.mark.parametrize(
    "name,content",
    [
        ("different.txt", b"one\ntwo"),
        ("evidence.txt", b"different"),
        ("evidence.xlsx", b"not a workbook"),
    ],
)
def test_conflict_is_409_and_leaves_evidence_untouched(upload_env, session_factory, name, content):
    client, app, task_id = upload_env
    assert send(client, task_id).status_code == 200
    before = counts(session_factory)
    paths = {path: path.read_bytes() for path in app.state.source_storage_dir.iterdir()}
    conflict = send(client, task_id, name=name, content=content)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "UPLOAD_KEY_CONFLICT"
    assert counts(session_factory) == before
    assert {path: path.read_bytes() for path in app.state.source_storage_dir.iterdir()} == paths


def test_identity_uses_exact_filename_not_sanitized_display_name(upload_env, session_factory):
    client, _, task_id = upload_env
    first = send(client, task_id, name="a/evidence.txt")
    assert first.status_code == 200
    assert send(client, task_id, name="b/evidence.txt").status_code == 409
    with session_factory() as session:
        assert session.get(InputSource, first.json()["source_id"]).original_name == "evidence.txt"


@pytest.mark.parametrize("second_name", ["evidence.txt ", " evidence.txt", "  evidence.txt  "])
def test_filename_whitespace_belongs_to_exact_identity(upload_env, session_factory, second_name):
    client, _, task_id = upload_env
    assert send(client, task_id).status_code == 200
    conflict = send(client, task_id, name=second_name)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "UPLOAD_KEY_CONFLICT"
    assert counts(session_factory)["input_source"] == 1


def test_rfc2231_filename_identity_retains_decoded_trailing_space(upload_env):
    client, _, task_id = upload_env

    def encoded(name):
        body = (
            '--b\r\nContent-Disposition: form-data; name="task_id"\r\n\r\n'
            f'{task_id}\r\n--b\r\nContent-Disposition: form-data; name="client_upload_key"'
            '\r\n\r\nupload-key\r\n--b\r\nContent-Disposition: form-data; name="file"; '
            f"filename*=utf-8''{name}\r\n\r\none\ntwo\r\n--b--\r\n"
        ).encode()
        return client.post(
            "/sources", content=body, headers={"Content-Type": "multipart/form-data; boundary=b"}
        )

    first = encoded("%E4%B8%AD.txt%20")
    assert first.status_code == 200
    assert encoded("%E4%B8%AD.txt%20").json()["source_id"] == first.json()["source_id"]
    assert encoded("%E4%B8%AD.txt").status_code == 409


@pytest.mark.parametrize("key", [None, "upload-key"])
def test_legacy_parser_keeps_trimming_filename_while_identity_preserves_it(upload_env, key):
    client, _, task_id = upload_env
    first = send(client, task_id, key=key, name="  evidence.txt  ")
    assert first.status_code == 200
    second = send(client, task_id, key=key, name="evidence.txt")
    assert second.status_code == (200 if key is None else 409)
    if key is None:
        assert first.json()["source_id"] != second.json()["source_id"]


def test_legacy_content_type_filename_fallback_still_works(upload_env):
    client, _, task_id = upload_env
    body = (
        '--b\r\nContent-Disposition: form-data; name="task_id"\r\n\r\n'
        f'{task_id}\r\n--b\r\nContent-Disposition: form-data; name="file"\r\n'
        'Content-Type: text/plain; name="  evidence.txt  "\r\n\r\none\r\n--b--\r\n'
    ).encode()
    result = client.post(
        "/sources", content=body, headers={"Content-Type": "multipart/form-data; boundary=b"}
    )
    assert result.status_code == 200


def test_key_is_task_scoped_and_legacy_callers_can_intentionally_duplicate(
    upload_env, session_factory
):
    client, _, first_task = upload_env
    _, second_task, _ = accept_input(
        session_factory, thread_id="second", client_event_key="one", text="second"
    )
    first = send(client, first_task).json()["source_id"]
    second = send(client, second_task).json()["source_id"]
    legacy = send(client, first_task, key=None).json()["source_id"]
    distinct_key = send(client, first_task, key="intentional-copy").json()["source_id"]
    assert len({first, second, legacy, distinct_key}) == 4
    assert client.get(f"/sources/{first}/spans", params={"task_id": second_task}).status_code == 404
    assert counts(session_factory)["source_upload_receipt"] == 3


@pytest.mark.parametrize("key", ["", " ", "a b", "é", "a" * 129, "x\ny", "x\x00y"])
def test_invalid_key_rejected_without_rows_or_files(upload_env, session_factory, key):
    client, app, task_id = upload_env
    response = send(client, task_id, key=key)
    assert response.status_code == 400
    assert response.json()["parser_status"] == "rejected"
    assert not any(counts(session_factory).values())
    assert list(app.state.source_storage_dir.iterdir()) == []


def test_valid_boundary_key_and_duplicate_multipart_key(upload_env, session_factory):
    client, _, task_id = upload_env
    key = "Ab9._:-" + "x" * 121
    assert send(client, task_id, key=key).status_code == 200
    response = client.post(
        "/sources",
        files=[
            ("task_id", (None, task_id)),
            ("client_upload_key", (None, "one")),
            ("client_upload_key", (None, "two")),
            ("file", ("evidence.txt", b"safe")),
        ],
    )
    assert response.status_code == 400
    assert "client_upload_key 不能重复" in response.json()["error"]["message"]
    assert counts(session_factory)["input_source"] == 1


def test_invalid_first_upload_does_not_claim_key(upload_env, session_factory):
    client, app, task_id = upload_env
    assert send(client, task_id, name="bad.xlsx", content=b"bad").status_code == 400
    assert not any(counts(session_factory).values())
    assert not list(app.state.source_storage_dir.iterdir())
    assert send(client, task_id).status_code == 200


def test_missing_task_and_new_upload_after_terminal_preserve_old_semantics(
    upload_env, session_factory
):
    client, _, task_id = upload_env
    assert send(client, "not-a-task").status_code == 404
    with session_factory() as session, begin_immediate(session):
        session.get(Task, task_id).status = "CANCELLED"
    assert send(client, task_id).status_code == 200
    assert send(client, task_id, key=None).status_code == 200


@pytest.mark.parametrize("same_payload", [True, False])
def test_two_concurrent_fast_misses_only_persist_one_source(
    upload_env, session_factory, database_url, monkeypatch, same_payload
):
    first_client, first_app, task_id = upload_env
    second_app = create_app(Settings(_env_file=None, database_url=database_url))
    second_app.state.source_storage_dir = first_app.state.source_storage_dir
    rendezvous = Barrier(2, timeout=5)
    original = sources.split_source

    def parse_without_write_lock(*args):
        # A separate writer must work here; XLSX parsing must not hold the lock.
        with session_factory() as session, begin_immediate(session):
            session.execute(text("SELECT 1"))
        rendezvous.wait()
        return original(*args)

    monkeypatch.setattr(sources, "split_source", parse_without_write_lock)
    with TestClient(second_app, base_url="http://127.0.0.1") as second_client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [
                pool.submit(send, first_client, task_id),
                pool.submit(
                    send,
                    second_client,
                    task_id,
                    content=b"one\ntwo" if same_payload else b"changed",
                ),
            ]
            results = [call.result(timeout=10) for call in calls]
    assert sorted(result.status_code for result in results) == (
        [200, 200] if same_payload else [200, 409]
    )
    if same_payload:
        assert results[0].json()["source_id"] == results[1].json()["source_id"]
    else:
        assert (
            next(result for result in results if result.status_code == 409).json()["error"]["code"]
            == "UPLOAD_KEY_CONFLICT"
        )
    assert counts(session_factory)["input_source"] == 1
    assert counts(session_factory)["source_upload_receipt"] == 1
    assert len(list(first_app.state.source_storage_dir.iterdir())) == 1


@pytest.mark.parametrize("failure_stage", ["source", "receipt", "commit"])
def test_database_failure_rolls_back_all_rows_and_only_its_new_file(
    upload_env, session_factory, failure_stage
):
    client, app, task_id = upload_env
    target = app.state.engine if failure_stage == "commit" else app.state.session_factory.class_
    event_name = "commit" if failure_stage == "commit" else "before_flush"

    def fail(*args):
        if failure_stage == "commit" or any(
            isinstance(row, InputSource if failure_stage == "source" else SourceUploadReceipt)
            for row in args[0].new
        ):
            raise RuntimeError("synthetic persistence failure")

    event.listen(target, event_name, fail)
    try:
        response = send(client, task_id)
        assert response.status_code == 500 and response.json()["error"]["code"] == "INTERNAL_ERROR"
        assert "synthetic persistence failure" not in response.text
    finally:
        event.remove(target, event_name, fail)
    assert not any(counts(session_factory).values())
    assert list(app.state.source_storage_dir.iterdir()) == []
    assert send(client, task_id).status_code == 200


def test_after_commit_failure_preserves_committed_file_for_safe_replay(upload_env, session_factory):
    client, app, task_id = upload_env

    def fail_after_commit(session):
        raise RuntimeError("synthetic callback failed after durable commit")

    target = app.state.session_factory.class_
    event.listen(target, "after_commit", fail_after_commit)
    try:
        response = send(client, task_id)
        assert response.status_code == 500 and response.json()["error"]["code"] == "INTERNAL_ERROR"
        assert not response.json()["error"]["retryable"]
        assert "synthetic callback failed" not in response.text
    finally:
        event.remove(target, "after_commit", fail_after_commit)
    paths = list(app.state.source_storage_dir.iterdir())
    assert len(paths) == 1
    assert paths[0].read_bytes() == b"one\ntwo"
    assert counts(session_factory)["source_upload_receipt"] == 1
    replay = send(client, task_id)
    assert replay.status_code == 200
    assert paths[0].name == replay.json()["source_id"]
    assert list(app.state.source_storage_dir.iterdir()) == paths


@pytest.mark.parametrize("failure", ["read", "unlink"])
def test_cleanup_uncertainty_preserves_file_and_original_error(
    upload_env, session_factory, monkeypatch, caplog, failure
):
    client, app, task_id = upload_env
    target = app.state.session_factory.class_
    original_get = target.get
    original_unlink = Path.unlink
    failed = False

    def fail_flush(session, *args):
        nonlocal failed
        if any(isinstance(row, InputSource) for row in session.new):
            failed = True
            raise RuntimeError("original write failure")

    def get(session, model, *args, **kwargs):
        if failed and model is InputSource and failure == "read":
            raise OSError("sensitive cleanup database detail")
        return original_get(session, model, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        if path.parent == app.state.source_storage_dir and failure == "unlink":
            raise PermissionError("sensitive cleanup filesystem detail")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(target, "get", get)
    monkeypatch.setattr(Path, "unlink", unlink)
    event.listen(target, "before_flush", fail_flush)
    try:
        response = send(client, task_id)
        assert response.status_code == 500 and response.json()["error"]["code"] == "INTERNAL_ERROR"
        assert "original write failure" not in response.text
        assert "sensitive cleanup" not in response.text
    finally:
        event.remove(target, "before_flush", fail_flush)
    assert not any(counts(session_factory).values())
    paths = list(app.state.source_storage_dir.iterdir())
    assert len(paths) == 1 and paths[0].read_bytes() == b"one\ntwo"
    records = [record for record in caplog.records if record.message == "upload.cleanup_deferred"]
    assert len(records) == 1
    assert records[0].error_code == "UPLOAD_CLEANUP_UNCERTAIN"
    assert "sensitive cleanup" not in caplog.text


def test_xb_collision_never_deletes_preexisting_file(upload_env, session_factory, monkeypatch):
    client, app, task_id = upload_env
    monkeypatch.setattr(sources, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    sentinel = app.state.source_storage_dir / "SRC-fixed"
    sentinel.write_bytes(b"preexisting evidence must survive")
    response = send(client, task_id)
    assert (
        response.status_code == 503
        and response.json()["error"]["code"] == "SOURCE_STORAGE_UNAVAILABLE"
    )
    assert "SRC-fixed" not in response.text
    assert sentinel.read_bytes() == b"preexisting evidence must survive"
    assert not any(counts(session_factory).values())


def test_receipt_composite_foreign_key_prevents_cross_task_source(upload_env, session_factory):
    client, _, first = upload_env
    source_id = send(client, first).json()["source_id"]
    _, second, _ = accept_input(
        session_factory, thread_id="other", client_event_key="one", text="other"
    )
    with pytest.raises(IntegrityError):
        with session_factory() as session, begin_immediate(session):
            session.add(
                SourceUploadReceipt(
                    task_id=second,
                    client_upload_key="x",
                    source_id=source_id,
                    payload_sha256="0" * 64,
                )
            )
    assert counts(session_factory)["source_upload_receipt"] == 1


def test_restart_replay_does_not_allocate_storage(upload_env, database_url, monkeypatch):
    client, app, task_id = upload_env
    first = send(client, task_id)
    old_paths = list(app.state.source_storage_dir.iterdir())
    next_app = create_app(Settings(_env_file=None, database_url=database_url))
    monkeypatch.setattr(
        sources, "mkdtemp", lambda **kwargs: pytest.fail("Replay allocated storage")
    )
    with TestClient(next_app, base_url="http://127.0.0.1") as next_client:
        replay = send(next_client, task_id)
    assert replay.status_code == 200
    assert replay.json()["source_id"] == first.json()["source_id"]
    assert next_app.state.source_storage_dir is None
    assert list(app.state.source_storage_dir.iterdir()) == old_paths


def test_migration_upgrade_downgrade_preserves_original_evidence(upload_env, engine):
    client, _, task_id = upload_env
    original = send(client, task_id).json()["source_id"]
    cfg = _migration_config()
    with engine.connect() as connection:
        before = {
            table: connection.execute(text(f'SELECT * FROM "{table}"')).fetchall()
            for table in ("input_source", "source_span", "task_source")
        }
        connection.rollback()
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, "verification_retries")
        assert "source_upload_receipt" not in inspect(connection).get_table_names()
        for table, rows in before.items():
            assert connection.execute(text(f'SELECT * FROM "{table}"')).fetchall() == rows
        connection.rollback()
        command.upgrade(cfg, "head")
        assert connection.execute(text("SELECT count(*) FROM source_upload_receipt")).scalar() == 0
        assert connection.execute(text("SELECT id FROM input_source")).scalar() == original
        for table, rows in before.items():
            assert connection.execute(text(f'SELECT * FROM "{table}"')).fetchall() == rows


def _child_upload(database_url, root, task_id, crash):
    # Direct ASGI Request avoids servers, ports, browsers, and TestClient IPC.
    settings = Settings(_env_file=None, database_url=database_url)
    app = create_app(settings)
    app.state.source_storage_dir = Path(root)
    body = (
        f'--b\r\nContent-Disposition: form-data; name="task_id"\r\n\r\n{task_id}\r\n'
        '--b\r\nContent-Disposition: form-data; name="client_upload_key"\r\n\r\nupload-key\r\n'
        '--b\r\nContent-Disposition: form-data; name="file"; filename="evidence.txt"\r\n'
        "\r\none\ntwo\r\n--b--\r\n"
    ).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "app": app,
            "headers": [(b"content-type", b"multipart/form-data; boundary=b")],
        },
        receive,
    )
    request.state.request_id = "child"
    request.state.correlation_id = "child"
    if crash == "before-commit":

        def exit_after_file(session, *args):
            if any(isinstance(row, InputSource) for row in session.new):
                os._exit(71)

        event.listen(app.state.session_factory.class_, "before_flush", exit_after_file)
    if crash.startswith("race-"):
        original_parse = sources.split_source

        def wait_for_other_process(*args):
            (Path(root).parent / crash).touch()
            deadline = time.monotonic() + 10
            while not (Path(root).parent / "start-race").exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("upload race barrier timed out")
                time.sleep(0.01)
            return original_parse(*args)

        sources.split_source = wait_for_other_process
    result = asyncio.run(sources.upload_source(request))
    if crash == "after-commit":
        os._exit(72)
    print(json.dumps(result.model_dump()))


@pytest.mark.parametrize("crash", ["before-commit", "after-commit"])
def test_process_crash_and_retry_preserves_documented_storage_boundary(
    upload_env, session_factory, database_url, crash
):
    client, app, task_id = upload_env
    environment = {k: v for k, v in os.environ.items() if not k.startswith("OOS_")}
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            database_url,
            str(app.state.source_storage_dir),
            task_id,
            crash,
        ],
        cwd=ROOT,
        env={**environment, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == (71 if crash == "before-commit" else 72), result.stderr
    old_paths = set(app.state.source_storage_dir.iterdir())
    assert len(old_paths) == 1
    expected = 0 if crash == "before-commit" else 1
    assert counts(session_factory)["input_source"] == expected
    assert counts(session_factory)["source_upload_receipt"] == expected
    response = send(client, task_id)
    assert response.status_code == 200
    assert counts(session_factory)["input_source"] == 1
    assert counts(session_factory)["source_upload_receipt"] == 1
    # Precommit hard crashes can leave an orphan, retained rather than overwritten.
    # Postcommit ACK loss must not create another file or consume another slot.
    assert len(list(app.state.source_storage_dir.iterdir())) == (2 if expected == 0 else 1)
    assert all(path.read_bytes() == b"one\ntwo" for path in old_paths)


def test_independent_processes_commit_one_receipt_after_simultaneous_miss(
    upload_env, session_factory, database_url
):
    _, app, task_id = upload_env
    root = app.state.source_storage_dir
    environment = {k: v for k, v in os.environ.items() if not k.startswith("OOS_")}
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                database_url,
                str(root),
                task_id,
                f"race-{index}",
            ],
            cwd=ROOT,
            env={**environment, "PYTHONPATH": str(ROOT)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    try:
        deadline = time.monotonic() + 10
        while not all((root.parent / f"race-{index}").exists() for index in range(2)):
            assert time.monotonic() < deadline, "Children did not reach simultaneous fast misses"
            assert all(process.poll() is None for process in processes)
            time.sleep(0.01)
        (root.parent / "start-race").touch()
        responses = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            responses.append(json.loads(stdout))
        assert responses[0]["source_id"] == responses[1]["source_id"]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
    assert counts(session_factory) == {
        "input_source": 1,
        "source_span": 2,
        "task_source": 1,
        "source_upload_receipt": 1,
    }
    assert len(list(root.iterdir())) == 1


if __name__ == "__main__":
    _child_upload(*sys.argv[1:])
