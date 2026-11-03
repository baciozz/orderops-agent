"""The process starts, reports FIXTURE, and only binds the Stage 0 routes."""

from decimal import Decimal

from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


def test_health_and_root() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))
    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["stage"] == "0"
    assert body["mode"] == "FIXTURE"
    assert body["live_requested"] is False
    assert body["live_block_reason"] is None
    assert Decimal(body["effective_live_budget_usd"]) == Decimal("5.00")
    assert body["schema_version"] == "1.0"
    assert health.headers["x-request-id"]
    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["task_view_stub"] == "/tasks/T-STUB/view"


def test_openapi_lists_mounted_routes() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))
    schema = client.get("/openapi.json").json()
    assert "/health" in schema["paths"]
    assert "/tasks/{task_id}/view" in schema["paths"]
    assert "/plans" in schema["paths"]
    assert "/orders/{order_id}" in schema["paths"]
