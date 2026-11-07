"""Actual bounded CLI and opt-in cycle driver after a cancelled late response.

The CLI remains FIXTURE-only. Its deterministic pause is a test-process timing
instrumentation around one fixture response, never a product LIVE mode switch.
"""

import os
import subprocess
import sys

import pytest
from app.db.models import Approval, ExecutionEvidence, ModelUsage, Operation, Plan
from sqlalchemy import func, select
from tests import test_link_audit as support
from tests.test_cancel_journeys import provider
from tests.test_demo_clarification_journeys import Page
from tests.test_http_storage_failure import real_http

seeded = support.seeded
CLI = ["-m", "app.cli", "worker-loop", "--interval-seconds", "0.1", "--max-iterations", "2"]

FIXTURE_PAUSE = """
import os,httpx
from app.adapters.fixture_model import FixtureLLMClient
original=FixtureLLMClient.structured_call
used=False
def pause(self,request):
    global used
    if not used:
        used=True
        with httpx.Client(trust_env=False,timeout=15) as client:
            client.post(os.environ['OOS_LLM_BASE_URL']+'/chat/completions',
                        json={'test_fixture_pause':True}).raise_for_status()
    return original(self,request)
FixtureLLMClient.structured_call=pause
from app.cli import main
raise SystemExit(main(['worker-loop','--interval-seconds','0.1','--max-iterations','2']))
"""

LIVE_CYCLES = """
from app.config import Settings
from app.db.session import make_engine,make_session_factory
from app.observability import configure_logging
from app.orchestration.worker_cycle import run_cycle
settings=Settings(_env_file=None)
configure_logging(settings)
engine=make_engine(settings.database_url)
for i in range(2):
    cycle=run_cycle(make_session_factory(engine),settings,fixture_only=False)
    print('TEST_CYCLE',i,cycle.outcome.status if cycle.outcome else None,flush=True)
engine.dispose()
"""


def environment(database_url, configured):
    from pathlib import Path

    return {
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "OOS_DATABASE_URL": database_url,
        **configured,
    }


def submit(client, key):
    response = client.post(
        "/inputs",
        json={
            "thread_id": key,
            "client_event_key": key,
            "selected_order_id": "SO-1001",
            "text": "SO-1001 把未发的 40 件黑色 M 改成白色 M",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


def state(client, task):
    return client.get(f"/tasks/{task}").json()["status"]


def test_native_cli_still_does_not_consume_live_tasks(seeded, database_url, tmp_path):
    with provider() as (configured, _, _, calls):
        with real_http(database_url, tmp_path, env_overrides=configured) as (client, _):
            live = submit(client, "live-input")
            page = Page(client.get("/ui/demo/submit").text)
            response = client.post(
                "/ui/demo/submit",
                data={
                    "text": "SO-1001 把未发的 40 件黑色 M 改成白色 M",
                    "selected_order_id": "SO-1001",
                    "client_event_key": page.field("client_event_key"),
                    "thread_id": page.field("thread_id"),
                },
            )
            assert response.status_code == 303
            fixture = response.headers["location"].split("/")[-1]
            run = subprocess.run(
                [sys.executable, *CLI],
                cwd=tmp_path,
                env=environment(database_url, configured),
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert run.returncode == 0, run.stderr
            assert state(client, live) == "QUEUED"
            assert state(client, fixture) == "AWAITING_APPROVAL" and calls == []


@pytest.mark.parametrize("mode", ["fixture-cli", "explicit-live-cycles"])
def test_cancelled_response_does_not_stop_following_business(seeded, database_url, tmp_path, mode):
    restart = tmp_path / "restart"
    restart.mkdir()
    with provider(hold=True) as (configured, received, release, calls):
        configured = dict(configured)
        if mode == "fixture-cli":
            configured["OOS_LIVE_ENABLED"] = "false"
        env = environment(database_url, configured)
        with real_http(database_url, tmp_path, env_overrides=configured) as (client, _):
            first = submit(client, "first-input")
            code = FIXTURE_PAUSE if mode == "fixture-cli" else LIVE_CYCLES
            with (tmp_path / "worker-first.log").open("wb") as stream:
                proc = subprocess.Popen(
                    [sys.executable, "-c", code],
                    cwd=tmp_path,
                    env=env,
                    stdout=stream,
                    stderr=stream,
                )
                try:
                    assert received.wait(10)
                    assert state(client, first) == "RUNNING"
                    revision = client.get(f"/tasks/{first}").json()["intent_revision"]
                    cancelled = client.post(
                        f"/tasks/{first}/cancel", json={"expected_intent_revision": revision}
                    )
                    assert cancelled.json()["status"] == "CANCELLED"
                    second = submit(client, "second-input")
                finally:
                    release.set()
                rc = proc.wait(timeout=20)
            assert rc == 0, (tmp_path / "worker-first.log").read_text()
            assert state(client, first) == "CANCELLED"
            assert state(client, second) == "AWAITING_APPROVAL"
            with seeded() as session:
                assert session.scalar(select(func.count()).select_from(Plan)) == 1
                assert session.scalar(select(func.count()).select_from(Approval)) == 0
                assert session.scalar(select(func.count()).select_from(Operation)) == 0
            html = client.get(f"/ui/tasks/{first}").text
            assert "任务已取消" in html and 'id="cancel-task"' not in html
            assert not client.get(f"/tasks/{first}/view").json()["approval"]["approvable"]
            assert client.get(f"/tasks/{second}/view").json()["approval"]["approvable"]

        with real_http(database_url, restart, env_overrides=configured) as (client, _):
            before = support.dump(database_url)
            assert submit(client, "first-input") == first
            command = CLI if mode == "fixture-cli" else ["-c", LIVE_CYCLES]
            resumed = subprocess.run(
                [sys.executable, *command],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert resumed.returncode == 0, resumed.stderr
            assert support.dump(database_url) == before
            assert state(client, first) == "CANCELLED"
            assert state(client, second) == "AWAITING_APPROVAL"
            plan = client.get(f"/tasks/{second}").json()["plans"][0]["plan_id"]
            view = client.get(f"/tasks/{second}/view").json()
            approved = client.post(
                f"/plans/{plan}/approve",
                json={
                    "plan_hash": view["approval"]["plan_hash"],
                    "expected_intent_revision": view["card"]["intent_revision"],
                },
            )
            assert approved.status_code == 200
            body = {
                "plan_hash": view["approval"]["plan_hash"],
                "approval_id": approved.json()["approval_id"],
                "idempotency_key": "normal-key",
            }
            completed = client.post(f"/plans/{plan}/commit", json=body)
            assert completed.status_code == 200, completed.text
            before = support.dump(database_url)
            assert (
                client.post(f"/plans/{plan}/commit", json=body).json()["operation_id"]
                == completed.json()["operation_id"]
            )
            assert support.dump(database_url) == before
            assert client.get(f"/tasks/{second}/view").json()["result"]["verification"] == "partial"
            with seeded() as session:
                for model in [Approval, Operation, ExecutionEvidence]:
                    assert session.scalar(select(func.count()).select_from(model)) == 1
                usage = session.scalars(select(ModelUsage)).all()
                assert len(usage) == (0 if mode == "fixture-cli" else 2)
                assert all(row.status == "SETTLED" for row in usage)
            assert len(calls) == (1 if mode == "fixture-cli" else 2)


@pytest.mark.parametrize("kind", ["running", "replaced-attempt", "other-error"])
def test_worker_does_not_hide_unconfirmed_stale_or_other_errors(seeded, monkeypatch, kind):
    from app.api.inputs import cancel_task
    from app.db.models import JobAttempt, Task
    from app.db.session import begin_immediate
    from app.errors import AppError
    from app.orchestration import worker
    from app.orchestration.inbox import accept_input

    task_id = accept_input(
        seeded,
        thread_id="negative",
        client_event_key="negative",
        text="SO-1001 把未发的 40 件黑色 M 改成白色 M",
    )[1]

    def fail(factory, **kwargs):
        assert kwargs["durable_task_id"] == task_id
        if kind != "running":
            with factory() as session:
                intent = session.get(Task, task_id).intent_revision
            cancel_task(factory, task_id, intent)
        if kind == "replaced-attempt":
            with factory() as session, begin_immediate(session):
                attempt = session.scalar(select(JobAttempt).where(JobAttempt.task_id == task_id))
                attempt.fencing_token += 1
        raise AppError(
            "synthetic failure", code="OTHER_ERROR" if kind == "other-error" else "STALE_ATTEMPT"
        )

    monkeypatch.setattr(worker, "dispatch_text", fail)
    with pytest.raises(AppError) as caught:
        worker.run_once(seeded)
    assert caught.value.code == ("OTHER_ERROR" if kind == "other-error" else "STALE_ATTEMPT")
