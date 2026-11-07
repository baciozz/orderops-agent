"""Real local provider + application HTTP: terminal failure, explicit retry and commit."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from app.db.models import Approval, ModelUsage, Operation, Plan
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from sqlalchemy import func, select
from tests import test_link_audit as support
from tests.test_http_storage_failure import real_http
from tests.test_stage4_live import intent

seeded = support.seeded
PRIVATE_BODY = "SYNTHETIC_PRIVATE_PROVIDER_BODY"
FAULTS = [
    ("timeout", "MODEL_TIMEOUT", "模型请求超时"),
    ("format", "SCHEMA_INVALID", "格式不符合要求"),
    ("http", "MODEL_UPSTREAM", "提供方服务出错"),
    ("transport", "MODEL_TRANSPORT", "连接模型提供方失败"),
]


@contextmanager
def local_provider(fault):
    state = {"mode": fault, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/v1/chat/completions"
            state["requests"].append(body)
            mode = state["mode"]
            if mode == "timeout":
                time.sleep(1.3)  # Real socket timeout; the application is configured for 1s.
            if mode == "transport":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            status = 503 if mode == "http" else 200
            payload = (
                {"error": {"message": PRIVATE_BODY, "code": "synthetic_unavailable"}}
                if mode == "http"
                else {
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": PRIVATE_BODY
                                if mode == "format"
                                else json.dumps(intent()),
                            },
                        }
                    ],
                }
            )
            encoded = json.dumps(payload).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected after the application timed out.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def business_snapshot(database_url):
    tables = {
        "sales_order",
        "order_revision",
        "order_line",
        "shipment_record",
        "stock_balance",
        "reservation",
        "stock_movement",
        "production_order",
        "purchase_order",
        "doc_revision",
        "downstream_link",
        "operation",
        "execution_evidence",
        "outbox_delivery",
    }
    with sqlite3.connect(database_url.removeprefix("sqlite:///")) as db:
        snapshot = {}
        for table in sorted(tables):
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
            # Intake advances an order's workflow intent even if parsing fails;
            # that is distinct from applying an order or inventory change.
            if table == "sales_order":
                columns.remove("intent_revision")
            fields = ",".join(f'"{column}"' for column in columns)
            snapshot[table] = sorted(
                db.execute(f'SELECT {fields} FROM "{table}"').fetchall(), key=repr
            )
        return json.dumps(snapshot, sort_keys=True)


def run_live_worker(database_url, tmp_path, overrides):
    # Public CLI and demo worker deliberately remain FIXTURE-only. Exercise the
    # existing backend worker with an explicitly configured local test consumer.
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "OOS_DATABASE_URL": database_url,
        **overrides,
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
from app.config import Settings
from app.db.session import make_engine, make_session_factory
from app.orchestration.worker import run_once
s = Settings(_env_file=None)
e = make_engine(s.database_url)
outcome = run_once(make_session_factory(e), settings=s)
from dataclasses import asdict
print('WORKER_RESULT=' + json.dumps(asdict(outcome) if outcome else None, default=str))
e.dispose()
""",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert PRIVATE_BODY not in result.stdout + result.stderr
    rows = result.stdout.splitlines()
    return json.loads(
        next(row.removeprefix("WORKER_RESULT=") for row in rows if row.startswith("WORKER_RESULT="))
    )


@pytest.mark.parametrize("path", ["sync", "durable"])
@pytest.mark.parametrize("fault,code,reason", FAULTS)
def test_provider_failure_is_explained_and_explicit_new_task_recovers(
    seeded, database_url, tmp_path, path, fault, code, reason
):
    with local_provider(fault) as (base_url, provider):
        overrides = {
            "OOS_LIVE_ENABLED": "true",
            "OOS_LLM_PROVIDER": "openai_compatible",
            "OOS_LLM_BASE_URL": base_url,
            "OOS_LLM_MODEL": "synthetic-loopback-parser",
            "OOS_MODEL_TIMEOUT_S": "1",
            "OOS_PRICE_IN_PER_MTOK": "0.10",
            "OOS_PRICE_OUT_PER_MTOK": "0.50",
        }
        initial_business = business_snapshot(database_url)
        with real_http(database_url, tmp_path, env_overrides=overrides) as (client, log):

            def submit(key):
                body = {"text": TEXT_BLACK_TO_WHITE, "client_event_key": key}
                if path == "durable":
                    body.update(thread_id="provider-recovery", selected_order_id="SO-1001")
                response = client.post("/dispatch" if path == "sync" else "/inputs", json=body)
                assert response.status_code == (200 if path == "sync" else 202), response.text
                return response.json()

            failed = submit("original-input")
            if path == "durable":
                run_live_worker(database_url, tmp_path, overrides)
            task_id = failed["task_id"]
            view = client.get(f"/tasks/{task_id}/view").json()
            detail = client.get(f"/tasks/{task_id}").json()
            html = client.get(f"/ui/tasks/{task_id}").text
            (tmp_path / "failure.html").write_text(html)
            assert detail["status"] == "FAILED" and detail["error_code"] == code
            assert view["card"]["mode"] == "LIVE" and not view["approval"]["approvable"]
            assert detail["plans"] == [] and view["result"]["operation_id"] is None
            assert '<button type="button" id="approve" disabled' in html
            assert '<button type="button" id="commit" disabled' in html
            assert business_snapshot(database_url) == initial_business
            with seeded() as session:
                for model in [Approval, Plan, Operation]:
                    assert session.scalar(select(func.count()).select_from(model)) == 0
                usage = session.scalars(select(ModelUsage)).all()
                assert len(usage) == len(provider["requests"]) > 0
                assert all(row.status == "SETTLED" for row in usage)
                failure_calls = len(usage)

            provider["mode"] = "healthy"
            before_replay = support.dump(database_url)
            replay = submit("original-input")
            assert replay["task_id"] == task_id and replay["status"] == "FAILED"
            if path == "durable":
                assert run_live_worker(database_url, tmp_path, overrides) is None
            assert support.dump(database_url) == before_replay
            assert len(provider["requests"]) == failure_calls

            recovered = submit("explicit-new-input")
            if path == "durable":
                recovered = run_live_worker(database_url, tmp_path, overrides)
            assert recovered["task_id"] != task_id
            assert recovered["status"] == ("VALIDATED" if path == "sync" else "AWAITING_APPROVAL")
            assert (
                client.get(f"/tasks/{recovered['task_id']}/view").json()["card"]["status"]
                == "AWAITING_APPROVAL"
            )
            assert business_snapshot(database_url) == initial_business
            url = f"/plans/{recovered['plan_id']}"
            approval = client.post(
                url + "/approve",
                json={
                    "plan_hash": recovered["plan_hash"],
                    "expected_intent_revision": recovered["intent_revision"],
                },
            )
            assert approval.status_code == 200, approval.text
            assert business_snapshot(database_url) == initial_business
            commit_body = {
                "plan_hash": recovered["plan_hash"],
                "approval_id": approval.json()["approval_id"],
                "idempotency_key": "provider-recovery-commit",
            }
            committed = client.post(url + "/commit", json=commit_body)
            assert committed.status_code == 200, committed.text
            after_commit = support.dump(database_url)
            assert (
                client.post(url + "/commit", json=commit_body).json()["operation_id"]
                == committed.json()["operation_id"]
            )
            assert support.dump(database_url) == after_commit
            final_view = client.get(f"/tasks/{recovered['task_id']}/view").json()
            assert final_view["result"]["verification"] == "partial"
            assert (
                final_view["result"]["verification_details"]["global_completeness"]
                == "not_assessed"
            )
            with seeded() as session:
                assert session.scalar(select(func.count()).select_from(Operation)) == 1
                assert session.scalar(select(func.count()).select_from(Approval)) == 1
            assert PRIVATE_BODY not in html + log.read_text()
            (tmp_path / "journey.json").write_text(
                json.dumps(
                    {
                        "path": path,
                        "fault": fault,
                        "error_code": code,
                        "failure_provider_calls": failure_calls,
                        "total_provider_calls": len(provider["requests"]),
                        "old_key_replay_unchanged": True,
                        "operation_id": committed.json()["operation_id"],
                        "failed_task": task_id,
                        "recovered_task": recovered["task_id"],
                        "verification": final_view["result"]["verification"],
                        "page_contains_code": code in html,
                        "page_explains_reason": reason in html,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            assert code in html and reason in html
            assert "不会自动重试" in html and "新提交标识" in html
            assert (
                "用量" in html
                and "/ui/demo/submit"
                not in html.split('id="task-failure-guidance"', 1)[1].split("</section>", 1)[0]
            )
