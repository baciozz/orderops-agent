"""Real loopback provider and HTTP cancellation, late output, commit races and restart."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Barrier, Event, Thread

import httpx
import pytest
from tests import test_link_audit as support
from tests.test_demo_clarification_journeys import Page
from tests.test_http_storage_failure import real_http
from tests.test_provider_failure_journeys import business_snapshot, run_live_worker
from tests.test_stage4_live import intent

seeded = support.seeded


@contextmanager
def provider(hold=False):
    received, release = Event(), Event()
    calls = []
    if not hold:
        release.set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            received.set()
            assert release.wait(10)
            body = json.dumps(
                {
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": json.dumps(intent())},
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    configured = {
        "OOS_LIVE_ENABLED": "true",
        "OOS_LLM_PROVIDER": "openai_compatible",
        "OOS_LLM_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
        "OOS_LLM_MODEL": "synthetic-cancel-parser",
        "OOS_MODEL_TIMEOUT_S": "15",
        "OOS_PRICE_IN_PER_MTOK": "0.10",
        "OOS_PRICE_OUT_PER_MTOK": "0.50",
    }
    try:
        yield configured, received, release, calls
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def input_body():
    return {
        "thread_id": "cancel-journey",
        "selected_order_id": "SO-1001",
        "client_event_key": "original-input",
        "text": "SO-1001 把未发的 40 件黑色 M 改成白色 M",
    }


def submit(client):
    response = client.post("/inputs", json=input_body())
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


def state(client, task):
    return client.get(f"/tasks/{task}").json()


def check_cancelled_page(client, task):
    view = client.get(f"/tasks/{task}/view").json()
    response = client.get(f"/ui/tasks/{task}")
    assert response.status_code == 200
    page = Page(response.text)
    assert view["card"]["status"] == "CANCELLED" and not view["approval"]["approvable"]
    assert view["result"]["operation_id"] is None
    for identity in ["approve", "commit"]:
        assert "disabled" in page.element(identity)
    assert not any(
        a.get("id") in {"cancel-task", "clarification-answer", "source-upload"}
        for _, a in page.tags
    )
    assert "任务已取消" in response.text
    assert "重启、刷新或重放原输入不会恢复此任务" in response.text
    assert "已发出的模型请求可能仍会返回" in response.text
    assert "仍可能产生用量" in response.text


def test_cancel_during_provider_call_discards_late_result_and_survives_restart(
    seeded, database_url, tmp_path
):
    before_business = business_snapshot(database_url)
    restart = tmp_path / "restart"
    restart.mkdir()
    with provider(hold=True) as (configured, received, release, calls):
        with real_http(database_url, tmp_path, env_overrides=configured) as (client, _):
            task = submit(client)
            with ThreadPoolExecutor(1) as pool:
                result = pool.submit(run_live_worker, database_url, tmp_path, configured)
                try:
                    assert received.wait(10)
                    assert state(client, task)["status"] == "RUNNING"
                    cancel = {"expected_intent_revision": state(client, task)["intent_revision"]}
                    assert (
                        client.post(f"/tasks/{task}/cancel", json=cancel).json()["status"]
                        == "CANCELLED"
                    )
                finally:
                    release.set()
                try:
                    outcome = result.result(timeout=15)
                except AssertionError as exc:
                    assert "STALE_ATTEMPT" in str(exc)
                else:
                    assert outcome["status"] == "CANCELLED"
            assert state(client, task)["status"] == "CANCELLED"
            assert business_snapshot(database_url) == before_business
            with seeded() as session:
                from app.db.models import Approval, ModelUsage, Operation, Plan
                from sqlalchemy import func, select

                for model in [Plan, Approval, Operation]:
                    assert session.scalar(select(func.count()).select_from(model)) == 0
                assert session.scalars(select(ModelUsage.status)).all() == ["SETTLED"]
        with real_http(database_url, restart, env_overrides=configured) as (client, _):
            before_replay = support.dump(database_url)
            assert submit(client) == task
            assert run_live_worker(database_url, tmp_path, configured) is None
            assert client.post(f"/tasks/{task}/cancel", json=cancel).json()["status"] == "CANCELLED"
            assert support.dump(database_url) == before_replay and len(calls) == 1
            check_cancelled_page(client, task)
            assert business_snapshot(database_url) == before_business


@pytest.mark.parametrize("ordering", ["cancel-first", "commit-first", "simultaneous"])
def test_cancel_and_approved_commit_have_one_outcome_after_restart(
    seeded, database_url, tmp_path, ordering
):
    from app.db.models import ExecutionEvidence, Operation
    from sqlalchemy import func, select

    before_business = business_snapshot(database_url)
    restart = tmp_path / "restart"
    restart.mkdir()
    with provider() as (configured, _, _, calls):
        with real_http(database_url, tmp_path, env_overrides=configured) as (client, _):
            task = submit(client)
            planned = run_live_worker(database_url, tmp_path, configured)
            assert planned["status"] == "AWAITING_APPROVAL"
            plan_url = f"/plans/{planned['plan_id']}"
            approved = client.post(
                plan_url + "/approve",
                json={
                    "plan_hash": planned["plan_hash"],
                    "expected_intent_revision": planned["intent_revision"],
                },
            )
            assert approved.status_code == 200
            commit = {
                "approval_id": approved.json()["approval_id"],
                "plan_hash": planned["plan_hash"],
                "idempotency_key": "original-commit",
            }
            cancel = {"expected_intent_revision": planned["intent_revision"]}

            def act(kind, barrier=None):
                with httpx.Client(base_url=client.base_url, trust_env=False, timeout=15) as own:
                    if barrier:
                        barrier.wait(10)
                    response = own.post(
                        plan_url + "/commit" if kind == "commit" else f"/tasks/{task}/cancel",
                        json=commit if kind == "commit" else cancel,
                    )
                    return response.status_code, response.json()

            if ordering == "cancel-first":
                cancelled, committed = act("cancel"), act("commit")
            elif ordering == "commit-first":
                committed, cancelled = act("commit"), act("cancel")
            else:
                barrier = Barrier(2)
                with ThreadPoolExecutor(2) as pool:
                    a, b = pool.submit(act, "cancel", barrier), pool.submit(act, "commit", barrier)
                    cancelled, committed = a.result(), b.result()
            final = state(client, task)["status"]
            assert final in {"CANCELLED", "SUCCEEDED"}
            if final == "CANCELLED":
                assert cancelled[1]["status"] == "CANCELLED" and committed[0] == 409
                assert committed[1]["error"]["code"] == "CANCELLED"
                assert business_snapshot(database_url) == before_business
                before_approval = support.dump(database_url)
                rejected = client.post(
                    plan_url + "/approve",
                    json={
                        "plan_hash": planned["plan_hash"],
                        "expected_intent_revision": planned["intent_revision"],
                    },
                )
                assert (
                    rejected.status_code == 409 and rejected.json()["error"]["code"] == "CANCELLED"
                )
                assert support.dump(database_url) == before_approval
            else:
                assert committed[0] == 200 and cancelled[1]["status"] == "ALREADY_COMMITTED"
        with real_http(database_url, restart, env_overrides=configured) as (client, _):
            before_replay = support.dump(database_url)
            assert submit(client) == task
            assert run_live_worker(database_url, tmp_path, configured) is None
            assert client.post(f"/tasks/{task}/cancel", json=cancel).json()["status"] == (
                "CANCELLED" if final == "CANCELLED" else "ALREADY_COMMITTED"
            )
            replay = client.post(plan_url + "/commit", json=commit)
            if final == "CANCELLED":
                assert replay.status_code == 409 and replay.json()["error"]["code"] == "CANCELLED"
                check_cancelled_page(client, task)
            else:
                assert (
                    replay.status_code == 200
                    and replay.json()["operation_id"] == committed[1]["operation_id"]
                )
                view = client.get(f"/tasks/{task}/view").json()
                assert view["card"]["status"] == "SUCCEEDED"
                assert view["result"]["verification"] == "partial"
                html = client.get(f"/ui/tasks/{task}").text
                assert 'id="cancel-task"' not in html and "业务已提交，请勿重复提交" in html
            assert support.dump(database_url) == before_replay and len(calls) == 1
            with seeded() as session:
                for model in [Operation, ExecutionEvidence]:
                    assert session.scalar(select(func.count()).select_from(model)) == int(
                        final == "SUCCEEDED"
                    )


@pytest.mark.parametrize("prior_approval", ["none", "active", "expired"])
def test_cancelled_task_cannot_create_return_or_renew_approval(
    seeded, database_url, tmp_path, prior_approval
):
    with provider() as (configured, _, _, _):
        if prior_approval == "expired":
            configured["OOS_APPROVAL_TTL_SECONDS"] = "1"
        with real_http(database_url, tmp_path, env_overrides=configured) as (client, _):
            task = submit(client)
            planned = run_live_worker(database_url, tmp_path, configured)
            plan_url = f"/plans/{planned['plan_id']}"
            body = {
                "plan_hash": planned["plan_hash"],
                "expected_intent_revision": planned["intent_revision"],
            }
            if prior_approval != "none":
                assert client.post(plan_url + "/approve", json=body).status_code == 200
            if prior_approval == "expired":
                time.sleep(1.1)
            assert (
                client.post(
                    f"/tasks/{task}/cancel",
                    json={"expected_intent_revision": planned["intent_revision"]},
                ).json()["status"]
                == "CANCELLED"
            )
            before = support.dump(database_url)
            rejected = client.post(plan_url + "/approve", json=body)
            (tmp_path / "approval-attempt.json").write_text(
                json.dumps(
                    {
                        "prior_approval": prior_approval,
                        "http_status": rejected.status_code,
                        "body": rejected.json(),
                        "database_unchanged": support.dump(database_url) == before,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "CANCELLED"
            assert support.dump(database_url) == before
