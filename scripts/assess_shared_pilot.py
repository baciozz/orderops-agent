"""Observe shared-use gaps on a fresh loopback FIXTURE demo; never an auth implementation.

Exit 0: observations recorded, not pilot approval. See assessment.json for unmet gates.
Only synthetic client labels are used: no accounts, login sessions, tokens or grants.
"""

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import ExitStack, closing, contextmanager
from html.parser import HTMLParser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if data.get("id"):
            self.elements[data["id"]] = data


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def rows(database, statement):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute(statement).fetchall()


def dump(database):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        return "\n".join(db.iterdump())


@contextmanager
def demo(output):
    database = output / "synthetic.db"
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(ROOT),
        "OOS_DATABASE_URL": "sqlite:///" + str(database),
        "OOS_LIVE_ENABLED": "false",
        "OOS_SOURCE_STORAGE_DIR": str(output / "originals"),
        "OOS_APPROVAL_TTL_SECONDS": "2",
    }
    with (output / "init.log").open("wb") as log:
        for command in ["init-db", "seed"]:
            subprocess.run(
                [sys.executable, "-B", "-m", "app.cli", command],
                env=env,
                cwd=output,
                stdout=log,
                stderr=log,
                check=True,
                timeout=30,
            )
    with socket.socket() as sock, (output / "uvicorn.log").open("wb") as log:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                "import sys,uvicorn;from app.main import app;"
                "uvicorn.run(app,fd=int(sys.argv[1]),access_log=False)",
                str(sock.fileno()),
            ],
            env=env,
            cwd=output,
            stdout=log,
            stderr=log,
            pass_fds=(sock.fileno(),),
        )
        try:
            with httpx.Client(base_url=url, trust_env=False, timeout=10) as client:
                deadline = time.monotonic() + 15
                while True:
                    require(proc.poll() is None, "Isolated demo failed to start")
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    require(time.monotonic() < deadline, "Demo startup deadline exceeded")
                    time.sleep(0.05)
            yield url, database
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def assess(output):
    observations = []
    with demo(output) as (url, database), ExitStack() as stack:
        # Labels describe test actors only. The application must not trust them.
        clients = {
            name: stack.enter_context(
                httpx.Client(
                    base_url=url,
                    trust_env=False,
                    timeout=10,
                    headers={} if name == "anonymous" else {"X-Pilot-Test-User": name},
                )
            )
            for name in ["anonymous", "A", "B"]
        }

        def request(actor, method, path, **kwargs):
            response = clients[actor].request(method, path, **kwargs)
            entry = {
                "actor_label": actor,
                "method": method,
                "path": path,
                "status": response.status_code,
            }
            if response.is_error and response.headers.get("content-type", "").startswith(
                "application/json"
            ):
                entry["error"] = response.json().get("error")
            observations.append(entry)
            with (output / "requests.jsonl").open("a") as log:
                log.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return response

        for path in ["/orders/SO-1001", "/orders/SO-1002", "/stock", "/ui/demo/tasks", "/metrics"]:
            require(request("anonymous", "GET", path).status_code == 200, "Baseline read changed")
        plan_response = request(
            "A",
            "POST",
            "/inputs",
            json={
                "text": "C 款黑色 L 全部改藏青",
                "client_event_key": "pilot-risk-input",
                "thread_id": "pilot-risk-thread",
                "selected_order_id": "SO-1001",
            },
        )
        require(plan_response.status_code == 202, "Synthetic intake failed")
        task_id = plan_response.json()["task_id"]
        require(
            request("A", "POST", "/ui/demo/worker-once", json={}).status_code == 200,
            "Initial fixture worker failed",
        )
        require(
            request("A", "GET", f"/tasks/{task_id}").json()["status"] == "NEEDS_CLARIFICATION",
            "Risk prompt absent",
        )
        for path in [f"/tasks/{task_id}", f"/tasks/{task_id}/view", f"/ui/tasks/{task_id}"]:
            require(
                request("B", "GET", path).status_code == 200, "Baseline cross-client read changed"
            )
        page = Page(request("B", "GET", f"/ui/tasks/{task_id}").text)
        fence = page.elements["task-inputs"]
        upload = request(
            "B",
            "POST",
            "/sources",
            data={"task_id": task_id},
            files={"file": ("synthetic.txt", b"synthetic user B note", "text/plain")},
        )
        require(upload.status_code == 200, "Baseline cross-client upload changed")
        answer = request(
            "B",
            "POST",
            f"/tasks/{task_id}/answers",
            json={
                "thread_id": task_id,
                "client_event_key": "pilot-B-answer",
                "text": "只改未开裁",
                "expected_intent_revision": int(fence["data-intent-revision"]),
                "expected_input_event_id": fence["data-input-event-id"],
            },
        )
        require(answer.status_code == 202, "Baseline cross-client answer changed")
        require(
            request("B", "POST", "/ui/demo/worker-once", json={}).status_code == 200,
            "Fixture worker failed",
        )
        page = Page(request("B", "GET", f"/ui/tasks/{task_id}").text)
        actions = page.elements["plan-actions"]
        plan_url = f"/plans/{actions['data-plan-id']}"
        approve = {
            "plan_hash": actions["data-plan-hash"],
            "expected_intent_revision": int(actions["data-intent-revision"]),
        }
        before_spoof = dump(database)
        spoof = request("B", "POST", plan_url + "/approve", json={**approve, "actor_id": "A"})
        require(
            spoof.status_code == 400 and spoof.json()["error"]["code"] == "VALIDATION_ERROR",
            "Body actor override no longer rejected",
        )
        require(dump(database) == before_spoof, "Rejected override mutated database")
        first = request("anonymous", "POST", plan_url + "/approve", json=approve)
        require(first.status_code == 200, "Baseline anonymous approval changed")
        commit = {
            "plan_hash": approve["plan_hash"],
            "approval_id": first.json()["approval_id"],
            "idempotency_key": "pilot-commit-original",
        }
        time.sleep(2.15)  # Actual approval expiry, explicitly NOT session expiry.
        expired = request("B", "POST", plan_url + "/commit", json=commit)
        require(
            expired.status_code == 409 and expired.json()["error"]["code"] == "EXPIRED",
            "Approval expiry guard failed",
        )
        require(
            rows(database, "SELECT count(*) FROM operation")[0][0] == 0,
            "Expired approval wrote business operation",
        )
        renewed = request(
            "B",
            "POST",
            plan_url + "/approve",
            json=approve,
            headers={"X-Forwarded-User": "A", "X-User-ID": "A"},
        )
        require(renewed.status_code == 200, "Baseline header-marked renewal changed")
        commit["approval_id"] = renewed.json()["approval_id"]
        committed = request(
            "B",
            "POST",
            plan_url + "/commit",
            json=commit,
            headers={"Cookie": "pilot_expired_marker=not-a-session"},
        )
        require(committed.status_code == 200, "Baseline unauthenticated commit changed")
        operation_id = committed.json()["operation_id"]
        after_commit = dump(database)
        replay = request("anonymous", "POST", plan_url + "/commit", json=commit)
        require(
            replay.status_code == 200 and replay.json()["operation_id"] == operation_id,
            "Original-key replay failed",
        )
        require(dump(database) == after_commit, "Replay duplicated business")
        for path in [
            f"/tasks/{task_id}/audit",
            f"/tasks/{task_id}/download?fmt=csv",
            f"/tasks/{task_id}/download?fmt=xlsx",
        ]:
            require(request("anonymous", "GET", path).status_code == 200, "Baseline export changed")
        extra = request(
            "A",
            "POST",
            "/inputs",
            json={
                "thread_id": "pilot-cancel",
                "client_event_key": "pilot-cancel-input",
                "selected_order_id": "SO-1002",
                "text": "synthetic cancel-only task",
            },
        ).json()
        state = request("B", "GET", f"/tasks/{extra['task_id']}").json()
        cancelled = request(
            "B",
            "POST",
            f"/tasks/{extra['task_id']}/cancel",
            json={"expected_intent_revision": state["intent_revision"]},
        )
        require(
            cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED",
            "Baseline cross-client cancellation changed",
        )
        actors = rows(database, "SELECT actor_id FROM approval ORDER BY id")
        audit_actors = rows(database, "SELECT actor_id FROM audit_record WHERE action='commit'")
        require(actors and all(actor == ("demo-user",) for actor in actors), "Unexpected actor")
        require(
            audit_actors and all(actor == ("demo-user",) for actor in audit_actors),
            "Unexpected audit actor",
        )
        counts = {
            table: rows(database, f'SELECT count(*) FROM "{table}"')[0][0]
            for table in ["operation", "execution_evidence", "approval", "model_usage"]
        }
        require(counts["operation"] == counts["execution_evidence"] == 1, "Unexpected writes")
        require(counts["model_usage"] == 0, "Unexpected model usage")
        require(rows(database, "PRAGMA integrity_check") == [("ok",)], "Integrity check failed")
        require(rows(database, "PRAGMA foreign_key_check") == [], "Foreign key check failed")
        result = {
            "baseline_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "assessment_completed": True,
            "shared_pilot_ready": False,
            "identity_labels_are_authenticated": False,
            "tenant_or_role_policy_selected": False,
            "task_id": task_id,
            "operation_id": operation_id,
            "counts": counts,
            "approval_actors": actors,
            "commit_audit_actors": audit_actors,
            "existing_controls_observed": [
                "body actor field rejected without DB mutation",
                "expired approval rejected",
                "explicit renewal succeeds",
                "same commit key once",
            ],
            "unmet_gates": [
                "authenticated identity",
                "order/task/source/export authorization",
                "accountable approval and execution identities",
                "authenticated receipt replay",
            ],
            "not_testable_without_identity_layer": [
                "real login/logout and session expiry",
                "revocation during transaction",
                "re-login then authorized same-key recovery",
                "tenant isolation and role separation",
            ],
            "expired_marker_note": (
                "An invalid cookie marker was ignored. No actual session was created or expired."
            ),
            "requests": observations,
        }
        (output / "assessment.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="New isolated evidence directory"
    )
    output = parser.parse_args().output.resolve()
    require(not output.exists(), "Output must be a new directory; existing data will not be reused")
    output.mkdir(parents=True, mode=0o700)
    result = assess(output)
    print(
        json.dumps(
            {
                "assessment_completed": result["assessment_completed"],
                "shared_pilot_ready": False,
                "report": str(output / "assessment.json"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
