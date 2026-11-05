"""Browser-facing routes exercise the existing inbox and kernel with synthetic data."""

from decimal import Decimal
from html.parser import HTMLParser

import pytest
from app.adapters.live_model import LiveLLMClient
from app.config import Settings
from app.db.models import Approval, InboxDelivery, Task
from app.main import create_app
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from app.services.seed import load_seed
from fastapi.testclient import TestClient
from sqlalchemy import func, select


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.buttons = {}
        self.inputs = {}
        self.actions = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "button" and attrs.get("id"):
            self.buttons[attrs["id"]] = attrs
        if tag == "input" and attrs.get("name"):
            self.inputs[attrs["name"]] = attrs.get("value", "")
        if attrs.get("id") == "plan-actions":
            self.actions = attrs


@pytest.fixture()
def web(session_factory, database_url, monkeypatch):
    load_seed(session_factory, database_url=database_url)

    def forbidden(*args, **kwargs):
        pytest.fail("The demo UI must never construct a live model client")

    monkeypatch.setattr(LiveLLMClient, "__init__", forbidden)
    app = create_app(Settings(_env_file=None, database_url=database_url))
    with TestClient(base_url="http://127.0.0.1", app=app) as client:
        yield client, session_factory, app


def submit(client, **overrides):
    page = client.get("/ui/demo/submit")
    assert page.status_code == 200
    fields = {**Page(page.text).inputs, "text": TEXT_BLACK_TO_WHITE, **overrides}
    response = client.post("/ui/demo/submit", data=fields, follow_redirects=False)
    assert response.status_code == 303, response.text
    url = response.headers["location"]
    task_id = url.rsplit("/", 1)[1]
    return task_id, url, fields


def test_home_form_and_static_assets(web):
    client, _, _ = web
    for url in ("/ui", "/ui/demo"):
        response = client.get(url)
        assert response.status_code == 200
        assert "演示" in response.text and "非生产" in response.text
        assert 'href="/ui/demo/orders/SO-1001"' in response.text
        assert TEXT_BLACK_TO_WHITE in response.text
    page = client.get("/ui/demo/submit")
    fields = Page(page.text).inputs
    assert fields["selected_order_id"] == "SO-1001"
    assert fields["thread_id"] == "demo-ui"
    assert (
        fields["client_event_key"]
        != Page(client.get("/ui/demo/submit").text).inputs["client_event_key"]
    )
    assert client.get("/ui/static/demo.css").status_code == 200
    assert client.get("/ui/static/demo.js").status_code == 200


def test_submit_queues_and_reuses_inbox_idempotency(web):
    client, factory, _ = web
    assert "暂无任务" in client.get("/ui/demo/tasks").text
    task_id, url, fields = submit(client)
    page = client.get(url)
    assert page.status_code == 200 and "QUEUED" in page.text
    assert "disabled" in Page(page.text).buttons["approve"]
    assert "disabled" in Page(page.text).buttons["commit"]
    repeated = client.post("/ui/demo/submit", data=fields, follow_redirects=False)
    assert repeated.headers["location"] == url
    with factory() as session:
        assert session.get(Task, task_id).order_id == "SO-1001"
        assert session.scalar(select(func.count()).select_from(Task)) == 1
        assert session.scalar(select(func.count()).select_from(InboxDelivery)) == 1
    recent = client.get("/ui/demo/tasks")
    assert recent.status_code == 200
    assert url in recent.text and "QUEUED" in recent.text and "intent_revision" in recent.text


def test_worker_approve_commit_and_read_result(web):
    client, factory, _ = web
    task_id, url, _ = submit(client, actor_id="client-forgery")
    response = client.post("/ui/demo/worker-once")
    assert response.status_code == 200
    result = response.json()["result"]
    assert response.json()["mode"] == "FIXTURE"
    assert result["task_id"] == task_id and result["status"] == "AWAITING_APPROVAL"
    page = client.get(url)
    parsed = Page(page.text)
    assert "disabled" not in parsed.buttons["approve"]
    assert "disabled" in parsed.buttons["commit"]
    assert "调度轨迹" in page.text and "下游影响清单" in page.text
    assert "<table>" in page.text and "A-WHITE-M" in page.text
    context = parsed.actions
    assert context["data-plan-id"] == result["plan_id"]
    assert context["data-plan-hash"] == result["plan_hash"]
    assert int(context["data-intent-revision"]) == result["intent_revision"]
    approval = client.post(
        f"/plans/{context['data-plan-id']}/approve",
        json={
            "plan_hash": context["data-plan-hash"],
            "expected_intent_revision": int(context["data-intent-revision"]),
        },
    )
    assert approval.status_code == 200, approval.text
    approval_id = approval.json()["approval_id"]
    with factory() as session:
        assert session.get(Approval, approval_id).actor_id == "demo-user"
    approved_page = Page(client.get(url).text)
    assert approved_page.actions["data-approval-id"] == approval_id
    assert "disabled" not in approved_page.buttons["commit"]
    commit_body = {
        "approval_id": approval_id,
        "plan_hash": context["data-plan-hash"],
        "idempotency_key": f"demo-commit-{task_id}-{approval_id}",
    }
    committed = client.post(f"/plans/{result['plan_id']}/commit", json=commit_body)
    assert committed.status_code == 200, committed.text
    assert committed.json()["revision"] == 2
    replay = client.post(f"/plans/{result['plan_id']}/commit", json=commit_body)
    assert replay.json()["operation_id"] == committed.json()["operation_id"]
    final = client.get(url)
    assert "SUCCEEDED" in final.text
    assert committed.json()["operation_id"] in final.text
    assert "revision</dt><dd>2" in final.text
    assert "核验</dt><dd>passed" in final.text
    assert "disabled" in Page(final.text).buttons["approve"]
    assert "disabled" in Page(final.text).buttons["commit"]
    assert 'data-approval-id=""' in final.text
    assert "A-WHITE-M" in client.get("/ui/demo/orders/SO-1001").text


def test_stub_has_disabled_approval(web):
    client, _, _ = web
    response = client.get("/ui/tasks/T-STUB")
    assert response.status_code == 200
    assert "disabled" in Page(response.text).buttons["approve"]
    assert "disabled" in Page(response.text).buttons["commit"]


def test_order_page_reuses_order_reader(web):
    client, _, _ = web
    response = client.get("/ui/demo/orders/SO-1001")
    assert response.status_code == 200
    assert "current_revision" in response.text and "intent_revision" in response.text
    order = client.get("/orders/SO-1001").json()
    for line in order["lines"]:
        assert line["sku_id"] in response.text
    assert "shipped（已发）" in response.text and "cut（已裁）" in response.text
    assert client.get("/ui/demo/orders/missing").status_code == 404


def test_worker_forces_fixture_with_live_process_settings(web, monkeypatch):
    client, _, app = web
    app.state.settings = app.state.settings.model_copy(
        update={
            "live_enabled": True,
            "llm_provider": "openai_compatible",
            "llm_model": "synthetic-test",
            "llm_base_url": "http://127.0.0.1:9999/v1",
            "price_in_per_mtok": Decimal("0"),
            "price_out_per_mtok": Decimal("0"),
        }
    )

    def forbidden():
        pytest.fail("Demo worker must use app settings with live explicitly disabled")

    monkeypatch.setattr("app.orchestration.dispatch.get_settings", forbidden)
    task_id, _, _ = submit(client)
    result = client.post("/ui/demo/worker-once").json()["result"]
    assert result["task_id"] == task_id and result["status"] == "AWAITING_APPROVAL"
    assert client.get(f"/tasks/{task_id}/view").json()["card"]["mode"] == "FIXTURE"


def test_idle_worker_and_unknown_fixture_are_explicit(web):
    client, _, _ = web
    idle = client.post("/ui/demo/worker-once")
    assert idle.status_code == 200 and idle.json()["result"] is None
    task_id, url, _ = submit(client, text="这条合成文本没有匹配 fixture")
    result = client.post("/ui/demo/worker-once").json()["result"]
    assert result["task_id"] == task_id and result["status"] == "FAILED"
    assert result["error_code"] == "FIXTURE_NOT_FOUND"
    assert "disabled" in Page(client.get(url).text).buttons["approve"]


def test_form_errors_preserve_values_and_do_not_queue(web):
    client, factory, _ = web
    for text, order in (
        ("", "SO-1001"),
        (TEXT_BLACK_TO_WHITE, "missing"),
        (TEXT_BLACK_TO_WHITE, ""),
    ):
        response = client.post(
            "/ui/demo/submit",
            data={"text": text, "selected_order_id": order},
        )
        assert response.status_code in (400, 404)
        assert 'role="alert"' in response.text
        assert Page(response.text).inputs["selected_order_id"] == order
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0


@pytest.mark.parametrize("existing_file", [False, True])
def test_fresh_database_has_friendly_empty_task_list(tmp_path, existing_file):
    path = tmp_path / "empty.db"
    if existing_file:
        path.touch()
    app = create_app(Settings(_env_file=None, database_url=f"sqlite:///{path}"))
    with TestClient(base_url="http://127.0.0.1", app=app) as client:
        assert client.get("/ui").status_code == 200
        response = client.get("/ui/demo/tasks")
        assert response.status_code == 200 and "暂无任务" in response.text
        assert client.post("/ui/demo/worker-once").json()["error"]["code"] == "DEMO_NOT_READY"
        assert "disabled" in Page(client.get("/ui/tasks/T-STUB").text).buttons["approve"]
    assert path.exists() == existing_file
