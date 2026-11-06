"""Fixed-sheet HTML entry uses only synchronous in-process handlers."""

from uuid import uuid4

import pytest
from app.api import demo_ui
from app.db.models import InputPayload, Task
from sqlalchemy import select

from test_approval_comparison import all_tables
from test_new_stock_entry import immediate, request
from test_new_stock_entry import web as _web

web = _web


@pytest.fixture(scope="session", autouse=True)
def _sandbox_asyncio_polling():
    yield


def fields():
    return {"text": "新建现货订单（表格）", "client_event_key": str(uuid4())}


def submit(state, values):
    return immediate(
        demo_ui.new_stock_sheet_submit(request(state, values, path="/ui/demo/new-stock-sheet"))
    )


def test_sheet_entry_normal_receipt_no_worker_and_replay(web):
    before = all_tables(web.engine)
    values = fields()
    response = submit(web, values)
    assert response.status_code == 303
    tid = response.headers["location"].rsplit("/", 1)[1]
    after = all_tables(web.engine)
    for table in before:
        if table not in {"task", "event", "input_payload", "inbox_delivery"}:
            assert before[table] == after[table], table
    with web.session_factory() as session:
        task = session.get(Task, tid)
        assert task.order_id is None
        payload = session.scalar(select(InputPayload).where(InputPayload.task_id == tid))
        assert payload.body["text"] == values["text"]
        assert payload.body["source_ids"] == []
    assert submit(web, values).headers["location"] == response.headers["location"]
    assert all_tables(web.engine) == after


@pytest.mark.parametrize(
    "override",
    [
        {"text": "新建现货订单（表格） "},
        {"text": ""},
        {"text": "白 M 30"},
        {"mode": "LIVE"},
        {"selected_order_id": "SO-1001"},
        {"source_ids": "x"},
        {"quantity": "20"},
        {"client_event_key": ""},
        {"client_event_key": "bad"},
    ],
)
def test_sheet_entry_rejects_override_before_receipt(web, override, monkeypatch):
    before = all_tables(web.engine)

    def forbidden(*args):
        raise AssertionError("Receipt must not run")

    monkeypatch.setattr(demo_ui, "receipt", forbidden)
    assert submit(web, {**fields(), **override}).status_code == 400
    assert all_tables(web.engine) == before


def test_sheet_entry_duplicate_fields_and_live_configuration(web):
    values = fields()
    assert submit(web, list(values.items()) + [("text", values["text"])]).status_code == 400
    web.settings = web.settings.model_copy(update={"live_enabled": True})
    # Deterministic activation must not require FIXTURE configuration.
    assert submit(web, values).status_code == 303
    html = demo_ui.new_stock_sheet_page(request(web)).body.decode()
    assert 'action="/ui/demo/new-stock-sheet"' in html
    assert "不调用模型" in html and "最多 3 次" in html
    assert "20.0" in html and "单独批准" in html
    assert 'href="/ui/demo/new-stock-sheet"' in demo_ui.home(request(web)).body.decode()


def test_task_page_distinguishes_sheet_activation_and_selected_scope(web):
    from app.api.tasks import task_view_page
    from app.orchestration.inbox import accept_input

    response = submit(web, fields())
    tid = response.headers["location"].rsplit("/", 1)[1]
    html = task_view_page(tid, request(web)).body.decode()
    assert "固定表格流程：确定性解析，不调用模型" in html
    assert "默认最多 3 次 worker 尝试" in html
    selected = accept_input(
        web.session_factory,
        thread_id="selected-sheet-ui",
        client_event_key="selected-sheet-ui",
        text="新建现货订单（表格）",
        selected_order_id="SO-1001",
    )
    selected_html = task_view_page(selected[1], request(web)).body.decode()
    assert "固定表格流程：确定性解析，不调用模型" not in selected_html
