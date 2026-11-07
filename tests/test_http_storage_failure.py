"""Loopback Uvicorn HTTP faults: safe response/logs, rollback and original-key recovery."""

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from tests import test_link_audit as support

seeded = support.seeded

SERVER = """
import sys
import uvicorn
from app.main import app
@app.get('/__test_unhandled')
def unhandled():
    raise RuntimeError('SYNTHETIC_PRIVATE_UNHANDLED_BODY')
uvicorn.run(app, fd=int(sys.argv[1]), access_log=False)
"""


@contextmanager
def real_http(database_url, tmp_path, *, env_overrides=None):
    log = tmp_path / "uvicorn.log"
    with socket.socket() as sock, log.open("wb") as stream:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        env = {
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "OOS_DATABASE_URL": database_url,
            "OOS_LIVE_ENABLED": "false",
            "PYTHONUNBUFFERED": "1",
        }
        env.update(env_overrides or {})
        proc = subprocess.Popen(
            [sys.executable, "-c", SERVER, str(sock.fileno())],
            cwd=tmp_path,
            env=env,
            pass_fds=(sock.fileno(),),
            stdout=stream,
            stderr=stream,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10
            ) as client:
                deadline = time.monotonic() + 15
                while True:
                    assert proc.poll() is None, log.read_text()
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, log.read_text()
                    time.sleep(0.05)
                yield client, log
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_real_http_evidence_insert_failure_is_safe_and_original_key_recovers(
    seeded, engine, database_url, tmp_path
):
    with real_http(database_url, tmp_path) as (client, log):
        planned = client.post(
            "/dispatch", json={"text": TEXT_BLACK_TO_WHITE, "client_event_key": "http-fault-input"}
        )
        assert planned.status_code == 200
        plan = planned.json()
        approval = client.post(
            f"/plans/{plan['plan_id']}/approve",
            json={
                "plan_hash": plan["plan_hash"],
                "expected_intent_revision": plan["intent_revision"],
            },
        )
        assert approval.status_code == 200
        body = {
            "approval_id": approval.json()["approval_id"],
            "plan_hash": plan["plan_hash"],
            "idempotency_key": "http-fault-original-key",
        }
        url = f"/plans/{plan['plan_id']}/commit"
        with engine.begin() as connection:
            connection.exec_driver_sql("""CREATE TRIGGER synthetic_retention_failure
                BEFORE INSERT ON execution_evidence BEGIN
                SELECT RAISE(ABORT, 'SYNTHETIC_PRIVATE_SQL_FAILURE'); END""")
        before = support.dump(database_url)
        failed = client.post(url, json=body)
        assert failed.status_code == 503
        error = failed.json()
        assert error["error"]["code"] == "COMMIT_STORAGE_FAILED"
        assert error["error"]["retryable"] is True and error["error"]["details"] == {}
        assert failed.headers["x-request-id"] == error["request_id"]
        assert support.dump(database_url) == before
        for private in (
            body["approval_id"],
            "A-BLACK-M",
            "SYNTHETIC_PRIVATE_SQL_FAILURE",
            "INSERT INTO execution_evidence",
            "approved_payload",
        ):
            assert private not in failed.text
        failure_log = log.read_text()
        for private in (
            "A-BLACK-M",
            "SYNTHETIC_PRIVATE_SQL_FAILURE",
            "approved_payload",
            "INSERT INTO execution_evidence",
            "[parameters:",
            "Exception in ASGI",
        ):
            assert private not in failure_log
        assert "COMMIT_STORAGE_FAILED" in failure_log
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER synthetic_retention_failure")
        recovered = client.post(url, json=body)
        assert recovered.status_code == 200
        op_id = recovered.json()["operation_id"]
        before = support.dump(database_url)
        assert client.post(url, json=body).json()["operation_id"] == op_id
        assert support.dump(database_url) == before
        view = client.get(f"/tasks/{plan['task_id']}/view").json()
        assert view["result"]["verification"] == "partial"


def test_real_http_unhandled_failure_does_not_escape_to_uvicorn_logs(
    seeded, database_url, tmp_path
):
    with real_http(database_url, tmp_path) as (client, log):
        response = client.get("/__test_unhandled")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL_ERROR"
        assert response.json()["error"]["retryable"] is False
        assert response.headers["x-frame-options"] == "DENY"
        assert "SYNTHETIC_PRIVATE_UNHANDLED_BODY" not in response.text + log.read_text()
        assert "Exception in ASGI" not in log.read_text()
