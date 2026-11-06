"""Synthetic handlers and rendering only; no browser or IPC."""

from datetime import timedelta
from urllib.parse import quote

import pytest
from app.api import orders
from app.api.tasks import task_view_page
from app.db.models import Approval, Plan, SalesOrder, Task
from app.errors import AppError
from app.orchestration.state import utcnow
from app.orchestration.worker import run_once
from schemas.api import ApproveRequest, CommitRequest
from sqlalchemy import select

from test_approval_comparison import all_tables
from test_new_stock_entry import fields, request, submit
from test_new_stock_entry import web as web_fixture

web = web_fixture


@pytest.fixture(scope="session", autouse=True)
def _sandbox_asyncio_polling():
    yield


def approval_body(outcome, **extra):
    return ApproveRequest(
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
        **extra,
    )


def duplicate(web):
    values = fields()
    location = submit(web, values).headers["location"]
    first = run_once(web.session_factory, settings=web.settings)
    approved = orders.approve(first.plan_id, approval_body(first), request(web))
    committed = orders.commit(
        first.plan_id,
        CommitRequest(
            plan_hash=first.plan_hash,
            approval_id=approved.approval_id,
            idempotency_key="first-synthetic-commit",
        ),
        request(web),
    )
    submit(web, fields())
    outcome = run_once(web.session_factory, settings=web.settings)
    return outcome, (values, location, committed)


def test_real_duplicate_confirmation_renewal_and_separate_commit(web):
    outcome, (values, location, first) = duplicate(web)
    snapshot = all_tables(web.engine)
    html = task_view_page(outcome.task_id, request(web)).body.decode()
    assert 'id="acknowledge-duplicate"' in html
    assert f'href="/ui/demo/orders/{first.order_id}"' in html
    assert "历史计划发现" in html and "已核对仍要创建" in html
    assert all_tables(web.engine) == snapshot
    for _ in range(2):
        with pytest.raises(AppError) as exc:
            orders.approve(outcome.plan_id, approval_body(outcome), request(web))
        assert exc.value.code == "DUPLICATE_UNCONFIRMED"
        assert all_tables(web.engine) == snapshot
    approved = orders.approve(
        outcome.plan_id, approval_body(outcome, acknowledge_duplicate=True), request(web)
    )
    after = all_tables(web.engine)
    for table in snapshot:
        if table not in {"approval", "plan"}:
            assert after[table] == snapshot[table], table
    with web.session_factory() as s:
        s.get(Approval, approved.approval_id).expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
    html = task_view_page(outcome.task_id, request(web)).body.decode()
    assert 'id="renew"' in html and 'id="acknowledge-duplicate"' in html
    snapshot = all_tables(web.engine)
    with pytest.raises(AppError) as exc:
        orders.approve(outcome.plan_id, approval_body(outcome), request(web))
    assert exc.value.code == "DUPLICATE_UNCONFIRMED"
    assert all_tables(web.engine) == snapshot
    renewed = orders.approve(
        outcome.plan_id, approval_body(outcome, acknowledge_duplicate=True), request(web)
    )
    assert renewed.approval_id != approved.approval_id
    body = CommitRequest(
        plan_hash=outcome.plan_hash,
        approval_id=renewed.approval_id,
        idempotency_key="second-synthetic-commit",
    )
    committed = orders.commit(outcome.plan_id, body, request(web))
    assert committed.order_id != first.order_id
    with web.session_factory() as s:
        assert len(list(s.scalars(select(SalesOrder)))) == 4
    snapshot = all_tables(web.engine)
    assert orders.commit(outcome.plan_id, body, request(web)).operation_id == committed.operation_id
    assert submit(web, values).headers["location"] == location
    assert all_tables(web.engine) == snapshot
    for tid in (outcome.task_id, first.task_id):
        assert 'id="acknowledge-duplicate"' not in task_view_page(tid, request(web)).body.decode()


@pytest.mark.parametrize("state", ["expired", "cancelled", "stale", "hash"])
def test_confirmation_does_not_override_guards(web, state):
    outcome, _ = duplicate(web)
    with web.session_factory() as s:
        if state == "expired":
            s.get(Plan, outcome.plan_id).status = "EXPIRED"
        elif state == "cancelled":
            s.get(Task, outcome.task_id).status = "CANCELLED"
        elif state == "stale":
            s.get(Task, outcome.task_id).intent_revision += 1
        s.commit()
    if state != "hash":
        html = task_view_page(outcome.task_id, request(web)).body.decode()
        assert 'id="acknowledge-duplicate"' not in html
    if state in {"expired", "hash"}:
        body = approval_body(outcome, acknowledge_duplicate=True)
        if state == "hash":
            body.plan_hash = "old-hash"
        snapshot = all_tables(web.engine)
        with pytest.raises(AppError) as exc:
            orders.approve(outcome.plan_id, body, request(web))
        assert exc.value.code == ("HASH_MISMATCH" if state == "hash" else "NOT_APPROVED")
        assert all_tables(web.engine) == snapshot


def test_warning_uses_server_plan_and_escapes_link_and_text(web):
    outcome, _ = duplicate(web)
    evil = '"><script>alert(1)</script>/../?x=1&y=2'
    with web.session_factory() as s:
        plan = s.get(Plan, outcome.plan_id)
        plan.normalized_changes = {**plan.normalized_changes, "suspected_duplicate_of": evil}
        s.commit()
    req = request(web)
    req.scope["query_string"] = b"duplicate=untrusted-order"
    snapshot = all_tables(web.engine)
    html = task_view_page(outcome.task_id, req).body.decode()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert f'href="/ui/demo/orders/{quote(evil, safe="")}"' in html
    assert "untrusted-order" not in html
    assert all_tables(web.engine) == snapshot
    with web.session_factory() as s:
        s.get(Plan, outcome.plan_id).plan_kind = "change"
        s.commit()
    assert 'id="acknowledge-duplicate"' not in task_view_page(outcome.task_id, req).body.decode()
