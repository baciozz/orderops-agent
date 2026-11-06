"""Read-only render/handler checks and deterministic Node observer tests; no IPC/browser."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.api import demo_ui, inputs, orders
from app.api.tasks import task_view, task_view_page
from app.db.models import Approval, Operation, Task
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from schemas.api import ApproveRequest, CommitRequest, InputSubmitRequest
from schemas.enums import RuntimeMode

from test_approval_comparison import all_tables
from test_new_stock_entry import request
from test_new_stock_entry import web as web_fixture

web = web_fixture


@pytest.fixture(scope="session", autouse=True)
def _sandbox_asyncio_polling():
    yield


def test_deterministic_observer():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional; browser QA remains separate")
    subprocess.run(
        [node, "tests/js/task_status_notice.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        timeout=30,
    )


def snapshot(web, task_id):
    before = all_tables(web.engine)
    view = task_view(task_id, request(web)).model_dump(mode="json")
    html = task_view_page(task_id, request(web)).body.decode()
    baseline = json.loads(
        html.split('<script id="task-status-baseline" type="application/json">', 1)[1].split(
            "</script>", 1
        )[0]
    )
    assert baseline == {key: view[key] for key in ("card", "approval", "result")}
    assert all_tables(web.engine) == before
    assert 'id="task-status-notice" role="status" aria-live="polite"' in html
    assert '<script src="/ui/static/task-status-notice.js" defer></script>' in html
    assert f'href="/ui/tasks/{task_id}">刷新任务</a>' in html
    assert "请先保留未提交内容" in html
    return baseline, html


def test_real_projection_baseline_and_same_status_changes(web):
    task_id = inputs.submit(
        InputSubmitRequest(
            thread_id="notice",
            client_event_key="synthetic",
            selected_order_id="SO-1001",
            text=TEXT_BLACK_TO_WHITE,
        ),
        request(web),
    ).task_id
    queued, _ = snapshot(web, task_id)
    result = demo_ui.worker_once(request(web))["result"]
    ready, _ = snapshot(web, task_id)
    assert queued["card"]["status"] == "QUEUED"
    assert ready["card"]["status"] == "AWAITING_APPROVAL"
    approved = orders.approve(
        result["plan_id"],
        ApproveRequest(
            plan_hash=result["plan_hash"], expected_intent_revision=result["intent_revision"]
        ),
        request(web),
    )
    approval, _ = snapshot(web, task_id)
    assert approval["card"] == ready["card"]
    assert approval["approval"]["expires_at"] != ready["approval"]["expires_at"]
    with web.session_factory() as session, session.begin():
        session.get(Approval, approved.approval_id).status = "EXPIRED"
    expired, _ = snapshot(web, task_id)
    assert expired["card"] == ready["card"]
    assert expired["approval"]["disabled_reason"] == "EXPIRED"
    with web.session_factory() as session, session.begin():
        session.get(Approval, approved.approval_id).status = "ACTIVE"
    committed = orders.commit(
        result["plan_id"],
        CommitRequest(
            plan_hash=result["plan_hash"],
            approval_id=approved.approval_id,
            idempotency_key="notice-commit",
        ),
        request(web),
    )
    passed, _ = snapshot(web, task_id)
    with web.session_factory() as session, session.begin():
        operation = session.get(Operation, committed.operation_id)
        operation.committed_verification_pending = True
        operation.verification_manual_required = True
        operation.verification_retry_attempts = 3
    pending, _ = snapshot(web, task_id)
    assert pending["card"] == passed["card"]
    assert passed["result"]["verification"] == "passed"
    assert pending["result"]["verification"] == "pending"
    assert pending["result"]["verification_manual_required"] is True
    assert pending["result"]["verification_retry_attempts"] == 3


def test_safely_serialized_baseline_and_no_non_task_monitor(web):
    with web.session_factory() as session, session.begin():
        session.add(
            Task(id="T-SYNTHETIC", chain_id="</script><script>secret</script>", status="QUEUED")
        )
    _, html = snapshot(web, "T-SYNTHETIC")
    assert "secret" not in html
    # Deliberately hostile projected field is escaped by Jinja tojson, not marked safe manually.
    from app.api.tasks import templates

    view = task_view("T-SYNTHETIC", request(web)).model_dump(mode="json")
    view["approval"]["plan_hash"] = "</script><script>secret</script>"
    rendered = templates.get_template("task_view.html").render(view=view)
    raw = rendered.split('id="task-status-baseline" type="application/json">', 1)[1].split(
        "</script>", 1
    )[0]
    assert "<script>" not in raw and "</script>" not in raw
    assert json.loads(raw)["approval"]["plan_hash"] == view["approval"]["plan_hash"]
    web.mode_resolution = SimpleNamespace(mode=RuntimeMode.FIXTURE)
    for page in [
        task_view_page("T-STUB", request(web)),
        demo_ui.submit_page(request(web)),
    ]:
        assert "task-status-baseline" not in page.body.decode()
        assert "task-status-notice.js" not in page.body.decode()
