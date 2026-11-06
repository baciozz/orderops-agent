"""Synchronous coroutine stepping, real fixture kernel and pure HTML; no IPC/browser."""

from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from app.api import demo_ui
from app.api.browser_security import browser_request_error
from app.api.tasks import task_view_page
from app.config import Settings
from app.db.models import Approval, Reservation, SalesOrder, Task
from app.errors import AppError
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE, TEXT_NEW_ORDER
from app.orchestration.inbox import accept_input
from app.orchestration.worker import run_once
from app.services.kernel import approve_plan, commit_plan
from app.services.seed import load_seed
from sqlalchemy import select
from starlette.requests import Request

from test_approval_comparison import all_tables


@pytest.fixture(scope="session", autouse=True)
def _sandbox_asyncio_polling():
    yield


def immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("Unexpected async I/O")


@pytest.fixture()
def web(engine, session_factory, database_url, monkeypatch):
    load_seed(session_factory, database_url=database_url)
    state = SimpleNamespace(
        engine=engine,
        session_factory=session_factory,
        settings=Settings(_env_file=None, database_url=database_url),
    )

    async def inline(func, *args):
        return func(*args)

    def forbidden(*args, **kwargs):
        raise AssertionError("No network or live model")

    monkeypatch.setattr(demo_ui, "run_in_threadpool", inline)
    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("app.adapters.live_model.LiveLLMClient.__init__", forbidden)
    return state


def request(state, fields=None, *, origin="http://127.0.0.1", path="/ui/demo/new-stock"):
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1"),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"origin", origin.encode()),
        ],
        "app": SimpleNamespace(state=state),
    }
    req = Request(scope)
    req._body = urlencode(fields or {}).encode()
    req.state.request_id = "synthetic"
    req.state.correlation_id = "synthetic"
    return req


def fields():
    return {"text": TEXT_NEW_ORDER, "client_event_key": str(uuid4())}


def submit(state, values):
    req = request(state, values)
    # This is the same global boundary that create_app runs before body consumption.
    error = browser_request_error(req)
    if error:
        raise error
    return immediate(demo_ui.new_stock_submit(req))


def approve(factory, outcome):
    return approve_plan(
        factory,
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
    )


def test_first_input_existing_approval_and_complete_new_order(web):
    factory = web.session_factory
    accept_input(
        factory,
        thread_id="old",
        client_event_key="old",
        text=TEXT_BLACK_TO_WHITE,
        selected_order_id="SO-1001",
    )
    old = run_once(factory, settings=web.settings)
    oldapproval = approve(factory, old)
    before = all_tables(web.engine)
    values = fields()
    response = submit(web, values)
    assert response.status_code == 303
    tid = response.headers["location"].rsplit("/", 1)[1]
    after = all_tables(web.engine)
    for table in before:
        if table not in {"task", "event", "input_payload", "inbox_delivery"}:
            assert before[table] == after[table], table
    with factory() as s:
        assert s.get(Task, tid).order_id is None
        assert s.get(Approval, oldapproval.approval_id).status == "ACTIVE"
    assert "新单尚未生成" in task_view_page(tid, request(web)).body.decode()
    outcome = demo_ui.worker_once(request(web))["result"]
    assert outcome["task_id"] == tid and outcome["status"] == "AWAITING_APPROVAL"
    outcome = SimpleNamespace(**outcome)
    approval = approve(factory, outcome)  # Explicit synthetic human decision.
    args = dict(
        plan_id=outcome.plan_id,
        approval_id=approval.approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="synthetic-new-stock",
        verification_settings=web.settings,
    )
    committed = commit_plan(factory, **args)  # Separate explicit synthetic commit.
    with factory() as s:
        order = s.get(SalesOrder, committed.order_id)
        assert order.status == "OPEN" and order.current_revision == 1
        reservations = list(s.scalars(select(Reservation).where(Reservation.order_id == order.id)))
        assert [(r.sku_id, r.warehouse_id, r.qty) for r in reservations] == [
            ("A-WHITE-M", "W1", 30)
        ]
        assert len(list(s.scalars(select(SalesOrder)))) == 3
    snapshot = all_tables(web.engine)
    assert submit(web, values).headers["location"] == response.headers["location"]
    assert commit_plan(factory, **args).operation_id == committed.operation_id
    assert all_tables(web.engine) == snapshot
    duplicate = submit(web, fields())
    assert duplicate.headers["location"] != response.headers["location"]
    outcome2 = run_once(factory, settings=web.settings)
    assert outcome2.status == "AWAITING_APPROVAL"
    snapshot = all_tables(web.engine)
    with pytest.raises(AppError) as exc:
        approve(factory, outcome2)
    assert exc.value.code == "DUPLICATE_UNCONFIRMED"
    assert all_tables(web.engine) == snapshot


@pytest.mark.parametrize(
    "override",
    [
        {"text": TEXT_BLACK_TO_WHITE},
        {"text": TEXT_NEW_ORDER.replace("30", "31")},
        {"text": ""},
        {"text": TEXT_NEW_ORDER + " "},
        {"mode": "LIVE"},
        {"selected_order_id": "SO-1001"},
        {"selected_order_id": ""},
        {"order_id": "SO-1001"},
        {"source_ids": "x"},
        {"quantity": "31"},
        {"acknowledge_duplicate": "true"},
        {"client_event_key": ""},
        {"client_event_key": '"><script>alert(1)</script>'},
    ],
)
def test_rejections_before_inbox_zero_writes(web, override, monkeypatch):
    snapshot = all_tables(web.engine)

    def forbidden(*args):
        raise AssertionError("Must reject before receipt")

    monkeypatch.setattr(demo_ui, "receipt", forbidden)
    response = submit(web, {**fields(), **override})
    assert response.status_code == 400
    assert "<script>alert(1)</script>" not in response.body.decode()
    assert all_tables(web.engine) == snapshot


def test_duplicate_fields_live_and_cross_origin(web):
    snapshot = all_tables(web.engine)
    values = fields()
    response = submit(web, list(values.items()) + [("text", TEXT_NEW_ORDER)])
    assert response.status_code == 400
    req = request(web, values, origin="https://evil.example")
    assert browser_request_error(req).code == "CROSS_ORIGIN_REQUEST"
    web.settings = web.settings.model_copy(update={"live_enabled": True})
    assert submit(web, values).status_code == 400
    assert "disabled" in demo_ui.new_stock_page(request(web)).body.decode()
    assert all_tables(web.engine) == snapshot


def test_form_navigation_actions_and_old_entry(web):
    from html.parser import HTMLParser

    class Page(HTMLParser):
        def __init__(self, html):
            super().__init__()
            self.tags = []
            self.feed(html)

        def handle_starttag(self, tag, attrs):
            self.tags.append((tag, dict(attrs)))

    first = demo_ui.new_stock_page(request(web))
    html = first.body.decode()
    page = Page(html)
    form = [attrs for tag, attrs in page.tags if tag == "form"][0]
    assert form["action"] == "/ui/demo/new-stock" and form["method"] == "post"
    textarea = [attrs for tag, attrs in page.tags if tag == "textarea"][0]
    assert "readonly" in textarea
    assert "selected_order_id" not in html and TEXT_NEW_ORDER in html
    assert "后续共享任务页" in html and "不永久锁定" in html

    def keys(body):
        return [a["value"] for t, a in Page(body).tags if t == "input"]

    assert keys(html) != keys(demo_ui.new_stock_page(request(web)).body.decode())
    assert 'href="/ui/demo/new-stock"' in demo_ui.home(request(web)).body.decode()
    original = {
        "text": TEXT_BLACK_TO_WHITE,
        "selected_order_id": "SO-1001",
        "thread_id": "demo-ui",
        "client_event_key": "legacy",
    }
    response = immediate(demo_ui.submit(request(web, original, path="/ui/demo/submit")))
    assert response.status_code == 303
    with web.session_factory() as s:
        assert s.get(Task, response.headers["location"].rsplit("/", 1)[1]).order_id == "SO-1001"


def test_new_post_is_registered_under_global_origin_boundary(web):
    from app.main import create_app

    app = create_app(web.settings)
    assert "post" in app.openapi()["paths"]["/ui/demo/new-stock"]
    dispatch = app.user_middleware[0].kwargs["dispatch"]
    snapshot = all_tables(web.engine)

    async def forbidden(request):
        raise AssertionError("Cross-origin must not reach body or handler")

    req = request(web, fields(), origin="https://evil.example")
    response = immediate(dispatch(req, forbidden))
    assert response.status_code == 403
    assert b"CROSS_ORIGIN_REQUEST" in response.body
    assert all_tables(web.engine) == snapshot
    app.state.engine.dispose()
