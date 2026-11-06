"""Persistent failure guidance from real handlers; no server, browser, or IPC."""

import pytest
from app.api import demo_ui, inputs, orders
from app.api.tasks import task_view, task_view_page
from app.db.models import Operation, SalesOrder, Task
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from schemas.api import ApproveRequest, CommitRequest, InputSubmitRequest
from sqlalchemy import select

from test_approval_comparison import all_tables
from test_new_stock_entry import request
from test_new_stock_entry import web as web_fixture

web = web_fixture


@pytest.fixture(scope="session", autouse=True)
def _sandbox_asyncio_polling():
    yield


def create(web, text):
    return inputs.submit(
        InputSubmitRequest(
            thread_id="failure-guidance",
            client_event_key="synthetic",
            selected_order_id="SO-1001",
            text=text,
        ),
        request(web),
    ).task_id


def render(web, task_id):
    snapshot = all_tables(web.engine)
    api_before = task_view(task_id, request(web)).model_dump(mode="json")
    html = task_view_page(task_id, request(web)).body.decode()
    assert task_view_page(task_id, request(web)).body.decode() == html
    assert all_tables(web.engine) == snapshot
    assert task_view(task_id, request(web)).model_dump(mode="json") == api_before
    return html


def panel(html):
    return html.split('id="task-failure-guidance">', 1)[1].split("</section>", 1)[0]


def test_real_unknown_fixture_survives_reload_and_links_to_exact_examples(web):
    text = TEXT_BLACK_TO_WHITE.replace("40", "41")
    task_id = create(web, text)
    assert 'id="task-failure-guidance"' not in render(web, task_id)
    result = demo_ui.worker_once(request(web))
    assert result["result"]["status"] == "FAILED"
    html = render(web, task_id)
    guidance = panel(html)
    assert "FIXTURE_NOT_FOUND" in guidance and "未匹配" in guidance
    assert "不会因此改为调用 live" in guidance
    assert "未找到此任务的关联执行记录" in guidance
    assert 'href="/ui/demo/submit"' in guidance
    assert 'href="/ui/demo/new-stock"' in guidance
    assert "新提交标识" in guidance
    assert TEXT_BLACK_TO_WHITE in demo_ui.submit_page(request(web)).body.decode()
    assert text not in html
    for control in ("clarification-answer", "cancel-task", "source-upload"):
        assert f'id="{control}"' not in html
    with web.session_factory() as s:
        assert s.get(Task, task_id).error_code == "FIXTURE_NOT_FOUND"
        assert s.get(SalesOrder, "SO-1001").current_revision == 1
        assert list(s.scalars(select(Operation))) == []


@pytest.mark.parametrize(
    "code",
    [
        None,
        "",
        "UNRECOGNIZED",
        "<img src=x onerror=alert(1)>",
        "FIXTURE_NOT_FOUND<script>",
        "FIXTURE_NOT_FOUND\n",
    ],
)
def test_unknown_and_malformed_error_codes_never_echo_database_text(web, code):
    task_id = create(web, "unknown private input")
    demo_ui.worker_once(request(web))
    with web.session_factory() as s:
        task = s.get(Task, task_id)
        task.error_code = code
        task.result_ref = "<script>private-result-secret</script>"
        s.commit()
    html = render(web, task_id)
    guidance = panel(html)
    assert "安全说明" in guidance and "维护人员" in guidance
    assert "private-result-secret" not in html and "onerror" not in html
    assert "<script>" not in guidance and "FIXTURE_NOT_FOUND" not in guidance
    assert "创建新任务" not in guidance


@pytest.mark.parametrize("manual", [False, True])
def test_committed_pending_semantics_and_failed_record_guard(web, manual):
    task_id = create(web, TEXT_BLACK_TO_WHITE)
    result = demo_ui.worker_once(request(web))["result"]
    approved = orders.approve(
        result["plan_id"],
        ApproveRequest(
            plan_hash=result["plan_hash"], expected_intent_revision=result["intent_revision"]
        ),
        request(web),
    )
    committed = orders.commit(
        result["plan_id"],
        CommitRequest(
            plan_hash=result["plan_hash"],
            approval_id=approved.approval_id,
            idempotency_key="guidance-commit",
        ),
        request(web),
    )
    with web.session_factory() as s:
        op = s.get(Operation, committed.operation_id)
        op.committed_verification_pending = True
        op.verification_manual_required = manual
        s.commit()
    html = render(web, task_id)
    assert 'id="task-failure-guidance"' not in html
    assert "业务已提交，请勿重复提交" in html
    if manual:
        assert "自动核验已停止，需维护人员人工处理" in html
    # Synthetic corruption/late failure: never infer no execution from status or ref.
    with web.session_factory() as s:
        task = s.get(Task, task_id)
        task.status = "FAILED"
        task.error_code = "FIXTURE_NOT_FOUND"
        s.commit()
    html = render(web, task_id)
    assert "已找到关联执行记录" in panel(html)
    assert "业务已提交，请勿重复提交" in html
    assert "创建新任务" not in panel(html)
    with web.session_factory() as s:
        s.get(Task, task_id).result_ref = "private overwritten failure text"
        s.commit()
    html = render(web, task_id)
    assert "已找到关联执行记录" in panel(html)
    assert "private overwritten" not in html
    assert "未找到此任务" not in panel(html)
    assert "创建新任务" not in panel(html)
    with web.session_factory() as s:
        assert s.get(SalesOrder, "SO-1001").current_revision == 2
        assert len(list(s.scalars(select(Operation)))) == 1


@pytest.mark.parametrize("status", ["CANCELLED", "SUPERSEDED", "RECONCILING", "SUCCEEDED"])
def test_other_statuses_do_not_inherit_failure_guidance(web, status):
    task_id = create(web, "unmatched synthetic input")
    demo_ui.worker_once(request(web))
    with web.session_factory() as s:
        s.get(Task, task_id).status = status
        s.commit()
    assert 'id="task-failure-guidance"' not in render(web, task_id)
