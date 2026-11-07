"""Real HTTP/HTML journeys: follow visible questions and XLSX answer hints."""

from html.parser import HTMLParser
from io import BytesIO
from uuid import uuid4

from app.db.models import Operation, SalesOrder
from app.orchestration.fixtures import TEXT_ALL_NAVY, TEXT_ONLY_UNCUT
from openpyxl import Workbook
from sqlalchemy import func, select
from tests import test_link_audit as support
from tests.test_http_storage_failure import real_http

seeded = support.seeded


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def element(self, identity):
        return next(attrs for _, attrs in self.tags if attrs.get("id") == identity)

    def field(self, name):
        return next(attrs.get("value", "") for _, attrs in self.tags if attrs.get("name") == name)


def page(client, task_id):
    response = client.get(f"/ui/tasks/{task_id}")
    assert response.status_code == 200
    return response.text, Page(response.text)


def worker(client, task_id):
    response = client.post("/ui/demo/worker-once", json={})
    assert response.status_code == 200 and response.json()["result"]["task_id"] == task_id
    return client.get(f"/tasks/{task_id}/view").json()


def answer(client, task_id, text, source_ids=None):
    _, html = page(client, task_id)
    fence = html.element("task-inputs")
    body = {
        "thread_id": task_id,
        "client_event_key": str(uuid4()),
        "text": text,
        "source_ids": source_ids or [],
        "expected_intent_revision": int(fence["data-intent-revision"]),
        "expected_input_event_id": fence["data-input-event-id"],
    }
    response = client.post(f"/tasks/{task_id}/answers", json=body)
    assert response.status_code == 202, response.text
    return body


def approve_commit_replay(client, task_id, database_url, factory):
    _, html = page(client, task_id)
    actions = html.element("plan-actions")
    url = f"/plans/{actions['data-plan-id']}"
    with factory() as session:
        before = session.scalar(select(func.count()).select_from(Operation))
    approved = client.post(
        url + "/approve",
        json={
            "plan_hash": actions["data-plan-hash"],
            "expected_intent_revision": int(actions["data-intent-revision"]),
            "acknowledge_duplicate": True,
        },
    )
    assert approved.status_code == 200, approved.text
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Operation)) == before
    body = {
        "plan_hash": actions["data-plan-hash"],
        "approval_id": approved.json()["approval_id"],
        "idempotency_key": "journey-commit",
    }
    committed = client.post(url + "/commit", json=body)
    assert committed.status_code == 200, committed.text
    view = client.get(f"/tasks/{task_id}/view").json()
    assert view["result"]["verification"] == "partial"
    assert view["result"]["verification_details"]["order_graph"] == "inconclusive"
    assert view["result"]["verification_details"]["global_completeness"] == "not_assessed"
    assert not view["result"]["verification_details"]["authorizes_business_action"]
    before_replay = support.dump(database_url)
    assert (
        client.post(url + "/commit", json=body).json()["operation_id"]
        == committed.json()["operation_id"]
    )
    assert support.dump(database_url) == before_replay
    return committed.json()


def test_text_risk_question_is_visible_and_disappears_after_current_answer(
    seeded, database_url, tmp_path
):
    with real_http(database_url, tmp_path) as (client, _):
        entry = Page(client.get("/ui/demo/submit").text)
        response = client.post(
            "/ui/demo/submit",
            data={
                "text": TEXT_ALL_NAVY,
                "selected_order_id": "SO-1001",
                "client_event_key": entry.field("client_event_key"),
                "thread_id": entry.field("thread_id"),
            },
        )
        assert response.status_code == 303
        task_id = response.headers["location"].split("/")[-1]
        view = worker(client, task_id)
        assert view["card"]["status"] == "NEEDS_CLARIFICATION"
        assert not view["approval"]["approvable"]
        assert len(view["questions"]) == 1
        assert "120 件不能改色" in view["questions"][0] and "180" in view["questions"][0]
        html, parsed = page(client, task_id)
        assert view["questions"][0] in html
        assert parsed.element("answer-text")["placeholder"] == "例如：只改未开裁"
        with seeded() as session:
            assert session.get(SalesOrder, "SO-1001").current_revision == 1
            assert session.scalar(select(func.count()).select_from(Operation)) == 0
        old_body = answer(client, task_id, TEXT_ONLY_UNCUT)
        queued = client.get(f"/tasks/{task_id}/view").json()
        assert queued["card"]["status"] == "QUEUED" and queued["questions"] == []
        before_stale = support.dump(database_url)
        stale = client.post(
            f"/tasks/{task_id}/answers", json={**old_body, "client_event_key": str(uuid4())}
        )
        assert stale.status_code == 409
        assert support.dump(database_url) == before_stale
        current = worker(client, task_id)
        assert current["card"]["status"] == "AWAITING_APPROVAL" and current["questions"] == []
        result = approve_commit_replay(client, task_id, database_url, seeded)
        assert result["order_id"] == "SO-1001" and result["revision"] == 2


def test_excel_user_follows_stage_hints_recovers_bad_upload_and_resolves_shortage(
    seeded, database_url, tmp_path
):
    with real_http(database_url, tmp_path) as (client, _):
        entry = Page(client.get("/ui/demo/new-stock-sheet").text)
        response = client.post(
            "/ui/demo/new-stock-sheet",
            data={
                "text": "新建现货订单（表格）",
                "client_event_key": entry.field("client_event_key"),
            },
        )
        assert response.status_code == 303
        task_id = response.headers["location"].split("/")[-1]
        assert worker(client, task_id)["card"]["status"] == "NEEDS_CLARIFICATION"
        _, first = page(client, task_id)
        assert first.element("answer-text")["placeholder"] == "按附件新建订单"
        bad = client.post(
            "/sources",
            data={"task_id": task_id, "client_upload_key": "bad"},
            files={
                "file": (
                    "invalid.xlsx",
                    b"invalid",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert 400 <= bad.status_code < 500 and bad.json()["error"]["code"]
        book = Workbook()
        sheet = book.active
        sheet.title = "订单"
        for row in [
            ["客户", "款", "颜色", "尺码", "数量"],
            ["华东服饰", "A", "白", "M", 30],
            ["华东服饰", "B", "蓝", "L", 25],
        ]:
            sheet.append(row)
        stream = BytesIO()
        book.save(stream)
        raw = stream.getvalue()
        uploaded = client.post(
            "/sources",
            data={"task_id": task_id, "client_upload_key": "good"},
            files={
                "file": (
                    "synthetic.xlsx",
                    raw,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert uploaded.status_code == 200, uploaded.text
        answer(
            client,
            task_id,
            first.element("answer-text")["placeholder"],
            [uploaded.json()["source_id"]],
        )
        rejected = worker(client, task_id)
        assert not rejected["approval"]["approvable"] and "缺口 5" in rejected["questions"][0]
        html, correction = page(client, task_id)
        assert "数量修正时可不勾选" in html
        assert correction.element("answer-text")["placeholder"] == "蓝 L 改成 20"
        answer(client, task_id, correction.element("answer-text")["placeholder"])
        current = worker(client, task_id)
        assert current["card"]["status"] == "AWAITING_APPROVAL"
        assert {line["sku_id"]: line["new_qty"] for line in current["line_diffs"]} == {
            "A-WHITE-M": 30,
            "B-BLUE-L": 20,
        }
        html, _ = page(client, task_id)
        assert "原表第 3 行数量 25 → 本次回答 20" in html
        assert "历史缺货建议（旧意图，非当前采购指令）" in html
        result = approve_commit_replay(client, task_id, database_url, seeded)
        assert result["revision"] == 1
