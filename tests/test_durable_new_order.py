"""Default FIXTURE ingestion through synchronous endpoints, not HTTP transport."""

from types import SimpleNamespace

import pytest
from app.api.inputs import get_task, submit
from app.api.orders import approve, commit
from app.config import Settings
from app.db.base import Base
from app.orchestration.fixtures import TEXT_NEW_ORDER
from app.orchestration.recovery import durable_commit
from app.orchestration.worker import run_once
from app.services.kernel import consistency_problems, downstream_problems
from app.services.seed import load_seed
from schemas.api import ApproveRequest, CommitRequest, InputSubmitRequest
from starlette.requests import Request

BUSINESS_TABLES = {
    "sales_order",
    "order_revision",
    "order_line",
    "reservation",
    "stock_balance",
    "stock_movement",
    "production_order",
    "purchase_order",
    "doc_revision",
    "downstream_link",
    "shipment_record",
    "operation",
}


def snapshot(engine):
    with engine.connect() as connection:
        return {
            table.name: [
                dict(row)
                for row in connection.execute(
                    table.select().order_by(*table.primary_key.columns)
                ).mappings()
            ]
            for table in Base.metadata.sorted_tables
        }


def business(state):
    return {name: state[name] for name in BUSINESS_TABLES}


@pytest.fixture()
def ingestion(engine, session_factory, database_url, monkeypatch):
    load_seed(session_factory, database_url=database_url)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url=database_url, live_enabled=False
    )

    def forbid_live(*args, **kwargs):
        pytest.fail("Default FIXTURE ingestion must not construct a live client")

    monkeypatch.setattr("app.orchestration.dispatch.LiveLLMClient", forbid_live)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    session_factory=session_factory,
                    settings=settings,
                )
            ),
        }
    )
    request.state.request_id = "synthetic-new-stock"
    request.state.correlation_id = "synthetic-new-stock"
    return request, settings


def assert_receipt_replay(body, receipt, request, engine, factory, settings):
    before = snapshot(engine)
    repeated = submit(body, request)
    assert (repeated.event_id, repeated.task_id) == (receipt.event_id, receipt.task_id)
    assert snapshot(engine) == before
    assert run_once(factory, settings=settings) is None
    assert snapshot(engine) == before


def test_new_stock_receipt_worker_approval_commit_and_replay(
    ingestion,
    engine,
    session_factory,
):
    request, settings = ingestion
    body = InputSubmitRequest(
        thread_id="new-stock",
        client_event_key="create-one",
        text=TEXT_NEW_ORDER,
    )
    before = snapshot(engine)
    receipt = submit(body, request)
    received = snapshot(engine)
    assert receipt.status == "QUEUED"
    assert submit(body, request) == receipt
    assert snapshot(engine) == received
    assert business(received) == business(before)
    original_event = next(row for row in received["event"] if row["id"] == receipt.event_id)
    original_payload = received["input_payload"][0]
    assert original_payload["body"] == {
        "text": TEXT_NEW_ORDER,
        "source_ids": [],
        "input_sequence": 1,
    }

    # No injected extraction or custom fixtures: exercise the shipped exact parser.
    outcome = run_once(session_factory, settings=settings)
    assert outcome.task_id == receipt.task_id and outcome.status == "AWAITING_APPROVAL"
    view = get_task(receipt.task_id, request)
    extraction = view["extraction"]
    assert extraction["customer_candidate"] == "华东服饰"
    assert extraction["business_event"]["event_type"] == "new_order"
    for lines in (extraction["new_lines"], extraction["business_event"]["new_lines"]):
        assert [(line["sku_id"], line["qty"]) for line in lines] == [("A-WHITE-M", 30)]
    assert len(view["plans"]) == 1
    planned = snapshot(engine)
    assert business(planned) == business(before)
    assert planned["approval"] == []
    assert_receipt_replay(body, receipt, request, engine, session_factory, settings)

    approval = approve(
        outcome.plan_id,
        ApproveRequest(
            plan_hash=outcome.plan_hash,
            expected_intent_revision=outcome.intent_revision,
        ),
        request,
    )
    approved = snapshot(engine)
    assert len(approved["approval"]) == 1
    assert business(approved) == business(before)
    commit_body = CommitRequest(
        approval_id=approval.approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key="commit-new-stock",
    )
    committed = commit(outcome.plan_id, commit_body, request)
    after = snapshot(engine)
    order_id = committed.order_id
    new_orders = [row for row in after["sales_order"] if row["id"] == order_id]
    assert len(new_orders) == 1
    order = new_orders[0]
    assert order["current_revision"] == committed.revision == 1
    assert order["status"] == "OPEN"
    assert (
        next(row for row in after["customer"] if row["id"] == order["customer_id"])["code"] == "HD"
    )
    revisions = [row for row in after["order_revision"] if row["order_id"] == order_id]
    assert len(revisions) == 1 and revisions[0]["revision_no"] == 1
    revision_id = revisions[0]["id"]
    lines = [row for row in after["order_line"] if row["revision_id"] == revision_id]
    assert [(row["sku_id"], row["ordered_qty"], row["fulfillment_mode"]) for row in lines] == [
        ("A-WHITE-M", 30, "STOCK"),
    ]
    reservations = [row for row in after["reservation"] if row["order_id"] == order_id]
    assert [(row["warehouse_id"], row["sku_id"], row["qty"]) for row in reservations] == [
        ("W1", "A-WHITE-M", 30),
    ]
    # Compare complete existing rows, not just totals that could hide cross-order damage.
    for table, field, value in (
        ("sales_order", "id", order_id),
        ("order_revision", "order_id", order_id),
        ("order_line", "revision_id", revision_id),
        ("reservation", "order_id", order_id),
    ):
        assert [row for row in after[table] if row[field] != value] == before[table]
    for table in (
        "production_order",
        "purchase_order",
        "doc_revision",
        "downstream_link",
        "shipment_record",
    ):
        assert after[table] == before[table]
    expected_balances = [dict(row) for row in before["stock_balance"]]
    balance = next(
        row
        for row in expected_balances
        if (row["warehouse_id"], row["sku_id"]) == ("W1", "A-WHITE-M")
    )
    old_revision = balance["revision"]
    balance["reserved"] += 30
    balance["revision"] += 1
    assert after["stock_balance"] == expected_balances
    movements = [
        row for row in after["stock_movement"] if row["operation_id"] == committed.operation_id
    ]
    assert len(movements) == 1
    movement = movements[0]
    assert (
        movement["warehouse_id"],
        movement["sku_id"],
        movement["delta_on_hand"],
        movement["delta_reserved"],
        movement["before_revision"],
        movement["after_revision"],
    ) == (
        "W1",
        "A-WHITE-M",
        0,
        30,
        old_revision,
        old_revision + 1,
    )
    assert [row for row in after["stock_movement"] if row not in movements] == before[
        "stock_movement"
    ]
    assert len(after["operation"]) == 1
    assert after["operation"][0]["status"] == "COMMITTED"
    assert get_task(receipt.task_id, request)["status"] == "SUCCEEDED"
    assert commit(outcome.plan_id, commit_body, request) == committed
    assert snapshot(engine) == after
    replay = durable_commit(
        session_factory,
        plan_id=outcome.plan_id,
        approval_id=approval.approval_id,
        plan_hash=outcome.plan_hash,
        idempotency_key=commit_body.idempotency_key,
    )
    assert replay.replayed and replay.operation_id == committed.operation_id
    assert snapshot(engine) == after
    assert_receipt_replay(body, receipt, request, engine, session_factory, settings)
    assert next(row for row in after["event"] if row["id"] == receipt.event_id) == original_event
    payload = next(row for row in after["input_payload"] if row["event_id"] == receipt.event_id)
    assert payload["task_id"] == original_payload["task_id"]
    assert {key: payload["body"][key] for key in original_payload["body"]} == original_payload[
        "body"
    ]
    assert len(after["job_attempt"]) == len(after["inbox_delivery"]) == 1
    assert after["inbox_delivery"][0]["attempts"] == 1
    assert after["model_usage"] == []
    with session_factory() as session:
        assert consistency_problems(session) == downstream_problems(session) == []


def test_near_match_new_stock_quantity_fails_without_plan_or_live_fallback(
    ingestion,
    engine,
    session_factory,
):
    request, settings = ingestion
    before = snapshot(engine)
    body = InputSubmitRequest(
        thread_id="new-stock",
        client_event_key="quantity-31",
        text="新建订单 华东服饰 A 款白 M 31",
    )
    receipt = submit(body, request)
    outcome = run_once(session_factory, settings=settings)
    assert outcome.task_id == receipt.task_id and outcome.status == "FAILED"
    view = get_task(receipt.task_id, request)
    assert view["error_code"] == "FIXTURE_NOT_FOUND"
    assert view["extraction"] is None and view["plans"] == []
    after = snapshot(engine)
    assert business(after) == business(before)
    assert after["approval"] == after["model_usage"] == []
    assert len(after["job_attempt"]) == len(after["inbox_delivery"]) == 1
    assert after["inbox_delivery"][0]["attempts"] == 1
    assert after["input_payload"][0]["body"]["text"] == body.text
    assert_receipt_replay(body, receipt, request, engine, session_factory, settings)
