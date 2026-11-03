"""Task-view information structure is frozen. Behavior of EXPIRED is stage 3."""

from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient
from schemas.task_view import (
    ApprovalBlock,
    DownstreamImpact,
    LineDiff,
    TaskCard,
    TaskResultBlock,
    TaskView,
)


def test_task_view_schema_has_required_blocks() -> None:
    assert set(TaskCard.model_fields) == {
        "task_id",
        "order_id",
        "status",
        "intent_revision",
        "waiting_on",
        "mode",
    }
    assert set(LineDiff.model_fields) == {
        "sku_id",
        "old_qty",
        "new_qty",
        "delta",
        "locked_qty",
        "locked_kind",
        "unexecuted_qty",
        "reservation_delta",
        "available_qty",
    }
    assert set(DownstreamImpact.model_fields) == {
        "doc_id",
        "doc_type",
        "linked_revision",
        "status",
        "verdict",
        "before",
        "after",
        "executable",
        "reason",
    }
    assert set(ApprovalBlock.model_fields) == {
        "plan_hash",
        "generated_at",
        "expires_at",
        "approvable",
        "disabled_reason",
    }
    assert set(TaskResultBlock.model_fields) == {
        "operation_id",
        "revision",
        "verification",
        "download_url",
    }
    assert set(TaskView.model_fields) >= {
        "card",
        "dispatch",
        "line_diffs",
        "downstream",
        "sources",
        "approval",
        "result",
    }


def test_stub_view_and_missing_task() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))
    found = client.get("/tasks/T-STUB/view")
    assert found.status_code == 200
    body = found.json()
    assert body["card"]["task_id"] == "T-STUB"
    assert body["card"]["mode"] == "FIXTURE"
    assert body["card"]["waiting_on"] == "none"
    assert body["approval"]["approvable"] is False
    assert body["dispatch"] == []
    assert body["result"]["operation_id"] is None
    missing = client.get("/tasks/T-01/view")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "TASK_NOT_FOUND"
    page = client.get("/ui/tasks/T-STUB")
    assert page.status_code == 200
    assert "调度轨迹" in page.text
    assert "下游影响清单" in page.text
    assert "这不是「订单已修改」" in page.text
    assert client.get("/ui/tasks/T-01").status_code == 404
