"""Task HTML history from actual durable upload, shortage and correction."""

import pytest
from app.api.tasks import task_view_page
from app.orchestration.worker import run_once
from tests.test_bounded_sheet_durable import respond, shortage
from tests.test_bounded_sheet_durable import sheet_env as _sheet_env
from tests.test_durable_new_order import snapshot

sheet_env = _sheet_env


@pytest.fixture(scope="session", autouse=True)
def _sandbox_asyncio_polling():
    yield


def test_durable_shortage_current_then_historical_in_html(sheet_env, engine, session_factory):
    request, settings = sheet_env
    request.app.state.engine = engine
    original, _, initial, rejected = shortage(request, session_factory, settings)
    proposals = snapshot(engine)["purchase_proposal"]
    before = snapshot(engine)
    html = task_view_page(original.task_id, request).body.decode()
    assert "当前输入的缺货建议（尚未采购）" in html
    assert "历史缺货建议（旧意图，非当前采购指令）" not in html
    assert "原缺货 5" in html
    assert rejected.plan_id in html and initial.event_id in html
    assert proposals[0]["id"] in html
    assert snapshot(engine) == before

    answer = respond(request, original.task_id, "蓝 L 改成 20")
    before = snapshot(engine)
    html = task_view_page(original.task_id, request).body.decode()
    assert "历史缺货建议（旧意图，非当前采购指令）" in html
    assert "当前输入的缺货建议（尚未采购）" not in html
    assert "原缺货 5" in html
    assert snapshot(engine) == before

    current = run_once(session_factory, settings=settings)
    assert current.status == "AWAITING_APPROVAL"
    before = snapshot(engine)
    html = task_view_page(original.task_id, request).body.decode()
    assert "历史缺货建议（旧意图，非当前采购指令）" in html
    assert "当前输入的缺货建议（尚未采购）" not in html
    assert "原缺货 5" in html
    assert "原表第 3 行数量 25 → 本次回答 20" in html
    assert answer.event_id in html and "蓝 L 改成 20" in html
    assert snapshot(engine) == before
    assert before["purchase_proposal"] == proposals
