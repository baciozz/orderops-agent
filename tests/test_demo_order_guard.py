"""Real demo POST handler and SQLite snapshots; no HTTP transport, IPC or model."""

import pytest
from app.api import demo_ui, inputs, orders
from app.db.models import Approval, SalesOrder, Task
from app.orchestration.fixtures import (
    TEXT_ALL_NAVY,
    TEXT_BLACK_TO_WHITE,
    TEXT_ONLY_UNCUT,
    golden_fixtures,
)
from schemas.api import ApproveRequest, InputSubmitRequest, PlanChangeRequest

from test_approval_comparison import all_tables
from test_new_stock_entry import immediate, request
from test_new_stock_entry import web as web_fixture

web = web_fixture


@pytest.fixture(scope="session", autouse=True)
def _sandbox_asyncio_polling():
    yield


def post(web, text=TEXT_BLACK_TO_WHITE, order="SO-1001", **fields):
    return immediate(
        demo_ui.submit(
            request(
                web,
                {
                    "text": text,
                    "selected_order_id": order,
                    "thread_id": "guard",
                    "client_event_key": "guard",
                    **fields,
                },
                path="/ui/demo/submit",
            )
        )
    )


def prior_approvals(web):
    """Real kernel approvals for both seeded orders, without changing fixtures."""
    approvals = []
    for order, sku in [("SO-1001", "A-BLACK-M"), ("SO-1002", "B-BLUE-L")]:
        preview = orders.create_plan(
            PlanChangeRequest.model_validate(
                {
                    "order_id": order,
                    "base_revision": 1,
                    "intent_revision": 1,
                    "client_event_key": f"prior-{order}",
                    "changes": [{"mode": "cancel_remaining", "from_sku": sku}],
                }
            ),
            request(web),
        )
        approved = orders.approve(
            preview.plan_id,
            ApproveRequest(
                plan_hash=preview.plan_hash,
                expected_intent_revision=preview.intent_revision,
            ),
            request(web),
        )
        approvals.append(approved.approval_id)
    with web.session_factory() as session:
        assert all(session.get(Approval, aid).status == "ACTIVE" for aid in approvals)
    return approvals


@pytest.mark.parametrize("text", [TEXT_BLACK_TO_WHITE, TEXT_ALL_NAVY, TEXT_ONLY_UNCUT])
@pytest.mark.parametrize("live", [False, True])
def test_known_wrong_order_rejected_before_receipt_with_zero_changes(web, monkeypatch, text, live):
    prior_approvals(web)
    web.settings = web.settings.model_copy(update={"live_enabled": live})
    before = all_tables(web.engine)

    def forbidden(*args):
        raise AssertionError("Wrong-order example must never reach durable receipt")

    monkeypatch.setattr(demo_ui, "receipt", forbidden)
    response = post(web, text, " SO-1002 ")
    assert response.status_code == 400
    html = response.body.decode()
    assert "DEMO_EXAMPLE_ORDER_MISMATCH" in html and "未提交" in html
    assert 'value="SO-1002"' in html and text in html
    assert all_tables(web.engine) == before


def test_guard_uses_all_shipped_change_declarations():
    expected = {
        text: payload["business_event"]["order_id"]
        for text, payload in golden_fixtures().items()
        if isinstance(payload.get("business_event"), dict)
        and payload["business_event"]["event_type"] == "change_order"
    }
    assert demo_ui._CHANGE_EXAMPLE_ORDERS == expected
    assert set(expected) == {TEXT_BLACK_TO_WHITE, TEXT_ALL_NAVY, TEXT_ONLY_UNCUT}


@pytest.mark.parametrize(
    "text,status",
    [
        (TEXT_BLACK_TO_WHITE, "AWAITING_APPROVAL"),
        (TEXT_ALL_NAVY, "NEEDS_CLARIFICATION"),
        (TEXT_ONLY_UNCUT, "AWAITING_APPROVAL"),
    ],
)
@pytest.mark.parametrize("live", [False, True])
def test_correct_order_original_worker_and_replay(web, text, status, live):
    approvals = prior_approvals(web)
    before = all_tables(web.engine)
    web.settings = web.settings.model_copy(update={"live_enabled": live})
    response = post(web, text, " SO-1001 ")
    assert response.status_code == 303
    tid = response.headers["location"].rsplit("/", 1)[1]
    with web.session_factory() as session:
        assert session.get(Task, tid).order_id == "SO-1001"
        assert session.get(Approval, approvals[0]).status == "REVOKED"
        assert session.get(Approval, approvals[1]).status == "ACTIVE"
    received = all_tables(web.engine)
    assert received["sales_order"] != before["sales_order"]
    assert post(web, text, "SO-1001").headers["location"] == response.headers["location"]
    assert all_tables(web.engine) == received
    outcome = demo_ui.worker_once(request(web))["result"]
    assert outcome["task_id"] == tid and outcome["status"] == status
    after = all_tables(web.engine)
    assert post(web, text).headers["location"] == response.headers["location"]
    assert all_tables(web.engine) == after
    assert after["operation"] == before["operation"]


@pytest.mark.parametrize(
    "text",
    [
        TEXT_BLACK_TO_WHITE + "。",
        " " + TEXT_ONLY_UNCUT,
        TEXT_BLACK_TO_WHITE.replace("40", "41"),
        "SO-1002 黑M换白M四十件",
    ],
)
def test_unknown_or_edited_text_keeps_durable_failure_behavior(web, text):
    approvals = prior_approvals(web)
    with web.session_factory() as session:
        before_intent = session.get(SalesOrder, "SO-1002").intent_revision
    response = post(web, text, "SO-1002")
    assert response.status_code == 303
    with web.session_factory() as session:
        assert session.get(Approval, approvals[0]).status == "ACTIVE"
        assert session.get(Approval, approvals[1]).status == "REVOKED"
        assert session.get(SalesOrder, "SO-1002").intent_revision == before_intent + 1
    outcome = demo_ui.worker_once(request(web))["result"]
    assert outcome["status"] == "FAILED" and outcome["error_code"] == "FIXTURE_NOT_FOUND"


@pytest.mark.parametrize(
    "order,status,code",
    [
        ("", 400, "VALIDATION_ERROR"),
        ("missing", 404, "NOT_FOUND"),
        ('"><script>alert(1)</script>', 404, "NOT_FOUND"),
    ],
)
def test_missing_nonexistent_orders_and_safe_errors(web, order, status, code):
    before = all_tables(web.engine)
    response = post(web, order=order, client_event_key='"><script>alert(2)</script>')
    assert response.status_code == status
    html = response.body.decode()
    assert code in html
    assert "<script>alert(" not in html
    alert = html.split('role="alert">')[1].split("</p>")[0]
    assert "alert(" not in alert
    assert all_tables(web.engine) == before


def test_read_only_page_boundary_and_escaping(web):
    before = all_tables(web.engine)
    html = demo_ui.submit_page(request(web)).body.decode()
    for text in (
        "精确示例",
        "固定对应 SO-1001",
        "标点",
        "数量",
        "审批失效",
        "解析失败",
        "不解析任意文本",
        "不限制其他 worker",
        'id="selected-order"',
        'id="change-text"',
        'id="event-key"',
        'id="thread"',
    ):
        assert text in html
    response = post(web, "</textarea><script>alert(3)</script>", "")
    assert "<script>alert(3)</script>" not in response.body.decode()
    assert "&lt;/textarea&gt;" in response.body.decode()
    assert all_tables(web.engine) == before


def test_direct_api_initially_unbound_late_binding_unchanged(web):
    receipt = inputs.submit(
        InputSubmitRequest(
            thread_id="api",
            client_event_key="api",
            text=TEXT_ONLY_UNCUT,
        ),
        request(web),
    )
    with web.session_factory() as session:
        assert session.get(Task, receipt.task_id).order_id is None
    outcome = demo_ui.worker_once(request(web))["result"]
    assert outcome["task_id"] == receipt.task_id
    assert outcome["status"] == "AWAITING_APPROVAL" and outcome["order_id"] == "SO-1001"


def test_wrong_order_reused_key_rejected_but_api_replay_stays_key_first(web):
    response = post(web)
    assert response.status_code == 303
    before = all_tables(web.engine)
    rejected = post(web, TEXT_ONLY_UNCUT, "SO-1002")
    assert rejected.status_code == 400
    assert "DEMO_EXAMPLE_ORDER_MISMATCH" in rejected.body.decode()
    assert all_tables(web.engine) == before
    replay = inputs.submit(
        InputSubmitRequest(
            thread_id="guard",
            client_event_key="guard",
            text=TEXT_ONLY_UNCUT,
            selected_order_id="SO-1002",
        ),
        request(web),
    )
    assert response.headers["location"] == f"/ui/tasks/{replay.task_id}"
    assert all_tables(web.engine) == before


@pytest.mark.parametrize("text", [TEXT_BLACK_TO_WHITE, TEXT_ALL_NAVY, TEXT_ONLY_UNCUT])
@pytest.mark.parametrize("reuse_key", [False, True])
def test_nonexistent_mismatch_never_reaches_receipt_even_with_reused_key(
    web, monkeypatch, text, reuse_key
):
    prior_approvals(web)
    response = post(web) if reuse_key else None
    before = all_tables(web.engine)

    def forbidden(*args):
        raise AssertionError("Nonexistent wrong-order example must never reach receipt")

    monkeypatch.setattr(demo_ui, "receipt", forbidden)
    rejected = post(web, text, "missing-order")
    assert rejected.status_code == 404
    assert "NOT_FOUND：订单不存在" in rejected.body.decode()
    assert all_tables(web.engine) == before
    if reuse_key:
        # Ordinary API still resolves the same key before nonexistent-order lookup.
        replay = inputs.submit(
            InputSubmitRequest(
                thread_id="guard",
                client_event_key="guard",
                text=text,
                selected_order_id="missing-order",
            ),
            request(web),
        )
        assert response.headers["location"] == f"/ui/tasks/{replay.task_id}"
        assert all_tables(web.engine) == before
