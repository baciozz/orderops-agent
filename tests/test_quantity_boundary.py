"""Boolean quantities must not become executable integer business intent."""

import json
from copy import deepcopy

import pytest
from app.config import Settings
from app.db.base import Base
from app.main import create_app
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE, TEXT_NEW_ORDER, golden_fixtures
from app.services.seed import load_seed
from fastapi.testclient import TestClient
from pydantic import ValidationError
from schemas.business import BusinessEvent, ExtractionResult, LineChange, NewOrderLine
from sqlalchemy import select


@pytest.mark.parametrize("model,fields", [(LineChange, {"mode": "increment"}), (NewOrderLine, {})])
@pytest.mark.parametrize("value", [True, False])
def test_boolean_rejected_before_quantity_coercion(model, fields, value):
    with pytest.raises(ValidationError, match="不能是布尔值"):
        model.model_validate({**fields, "qty": value})
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps({**fields, "qty": value}))


@pytest.mark.parametrize("model,fields", [(LineChange, {"mode": "increment"}), (NewOrderLine, {})])
@pytest.mark.parametrize(
    "value,expected", [(None, None), (0, 0), (-2, -2), (40, 40), ("40", 40), (40.0, 40)]
)
def test_existing_quantity_compatibility(model, fields, value, expected):
    assert model.model_validate({**fields, "qty": value}).qty == expected
    assert model.model_validate(fields).qty is None


@pytest.mark.parametrize("model,fields", [(LineChange, {"mode": "increment"}), (NewOrderLine, {})])
@pytest.mark.parametrize("value", [1.5, "true", "false", [], {}])
def test_other_invalid_quantities_remain_invalid(model, fields, value):
    with pytest.raises(ValidationError):
        model.model_validate({**fields, "qty": value})


@pytest.mark.parametrize(
    "text,field", [(TEXT_BLACK_TO_WHITE, "line_changes"), (TEXT_NEW_ORDER, "new_lines")]
)
@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize("nested", [False, True])
def test_nested_extraction_boundaries(text, field, value, nested):
    payload = golden_fixtures()[text]
    target = payload["business_event"] if nested else payload
    target[field][0]["qty"] = value
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({**payload, "task_id": "synthetic", "intent_revision": 1})
    if nested:
        with pytest.raises(ValidationError):
            BusinessEvent.model_validate(target)


def snapshot(factory, names=None):
    with factory() as session:
        return {
            table.name: sorted((tuple(row) for row in session.execute(select(table))), key=repr)
            for table in Base.metadata.sorted_tables
            if names is None or table.name in names
        }


@pytest.fixture()
def http(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    app = create_app(Settings(_env_file=None, database_url=database_url, live_enabled=False))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, session_factory


def plan_body(qty):
    return {
        "order_id": "SO-1001",
        "base_revision": 1,
        "intent_revision": 1,
        "client_event_key": "quantity-boundary",
        "changes": [
            {
                "mode": "replace_remaining",
                "from_sku": "A-BLACK-M",
                "to_sku": "A-WHITE-M",
                "qty": qty,
            }
        ],
    }


@pytest.mark.parametrize("value", [True, False])
def test_http_plan_boolean_rejected_without_any_database_mutation(http, value):
    client, factory = http
    before = snapshot(factory)
    response = client.post("/plans", json=plan_body(value))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"]["errors"][0]["loc"] == ["body", "changes", 0, "qty"]
    assert snapshot(factory) == before


@pytest.mark.parametrize("value", [40, "40", 40.0])
def test_http_plan_valid_quantity_control(http, value):
    client, _ = http
    response = client.post("/plans", json=plan_body(value))
    assert response.status_code == 200
    plan = response.json()
    assert plan["status"] == "VALIDATED"
    assert plan["requires_human_approval"] is True
    view = client.get(f"/tasks/{plan['task_id']}/view").json()
    assert next(row for row in view["line_diffs"] if row["sku_id"] == "A-WHITE-M")["new_qty"] == 40


# Dispatch may persist failed-task diagnostics, but must never produce business intent.
BUSINESS_TABLES = {
    "sales_order",
    "order_revision",
    "order_line",
    "stock_balance",
    "stock_movement",
    "downstream_change_draft",
    "purchase_proposal",
    "reservation",
    "shipment_record",
    "production_order",
    "purchase_order",
    "downstream_link",
    "doc_revision",
    "plan",
    "approval",
    "operation",
    "audit_record",
}


@pytest.mark.parametrize(
    "text,field", [(TEXT_BLACK_TO_WHITE, "line_changes"), (TEXT_NEW_ORDER, "new_lines")]
)
@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize("nested", [False, True])
def test_http_dispatch_bad_parser_quantity_cannot_create_plan(
    http, monkeypatch, text, field, value, nested
):
    client, factory = http
    library = deepcopy(golden_fixtures())
    target = library[text]["business_event"] if nested else library[text]
    target[field][0]["qty"] = value
    monkeypatch.setattr("app.orchestration.dispatch.golden_fixtures", lambda: library)
    assert BUSINESS_TABLES <= {table.name for table in Base.metadata.sorted_tables}
    before = snapshot(factory, BUSINESS_TABLES)
    response = client.post("/dispatch", json={"text": text, "client_event_key": "bad-quantity"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "FAILED"
    assert data["error_code"] == "SCHEMA_INVALID"
    assert data["plan_id"] is None
    assert data["requires_human_approval"] is False
    assert snapshot(factory, BUSINESS_TABLES) == before


@pytest.mark.parametrize(
    "text,field,qty", [(TEXT_BLACK_TO_WHITE, "line_changes", 40), (TEXT_NEW_ORDER, "new_lines", 30)]
)
@pytest.mark.parametrize("representation", [int, str, float])
def test_http_dispatch_valid_quantity_control(http, monkeypatch, text, field, qty, representation):
    client, _ = http
    library = golden_fixtures()
    library[text][field][0]["qty"] = representation(qty)
    library[text]["business_event"][field][0]["qty"] = representation(qty)
    monkeypatch.setattr("app.orchestration.dispatch.golden_fixtures", lambda: library)
    response = client.post("/dispatch", json={"text": text, "client_event_key": "valid-quantity"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "VALIDATED"
    assert data["plan_id"] is not None
    assert data["requires_human_approval"] is True


@pytest.mark.parametrize("text,qty", [(TEXT_BLACK_TO_WHITE, 40), (TEXT_NEW_ORDER, 30)])
def test_valid_integer_http_journeys_require_approval_and_commit(http, text, qty):
    client, factory = http
    # Planning advances intent_revision, but no committed lines or inventory.
    before = snapshot(factory, {"order_revision", "order_line", "stock_balance"})
    response = client.post("/dispatch", json={"text": text, "client_event_key": "valid-journey"})
    assert response.status_code == 200
    plan = response.json()
    assert plan["status"] == "VALIDATED"
    assert snapshot(factory, set(before)) == before
    url = f"/plans/{plan['plan_id']}"
    approved = client.post(
        f"{url}/approve",
        json={"plan_hash": plan["plan_hash"], "expected_intent_revision": plan["intent_revision"]},
    )
    assert approved.status_code == 200
    committed = client.post(
        f"{url}/commit",
        json={
            "plan_hash": plan["plan_hash"],
            "approval_id": approved.json()["approval_id"],
            "idempotency_key": "valid-journey-commit",
        },
    )
    assert committed.status_code == 200
    result = committed.json()
    assert result["status"] == "SUCCEEDED"
    order = client.get(f"/orders/{result['order_id']}").json()
    assert next(row for row in order["lines"] if row["sku_id"] == "A-WHITE-M")["ordered_qty"] == qty
    if text == TEXT_BLACK_TO_WHITE:
        assert order["current_revision"] == 2
        assert (
            next(row for row in order["lines"] if row["sku_id"] == "A-BLACK-M")["ordered_qty"] == 60
        )
    else:
        assert order["current_revision"] == 1
