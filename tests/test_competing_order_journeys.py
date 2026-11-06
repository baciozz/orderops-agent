"""Real HTTP competition: stale approval rejection, stock limits, and replanning."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from app.db.models import ModelUsage, Operation, Reservation
from sqlalchemy import func, select
from tests import test_demo_clarification_journeys as journey
from tests import test_link_audit as support
from tests.test_http_storage_failure import real_http

seeded = support.seeded


def stock(client):
    return next(row for row in client.get("/stock").json()["rows"] if row["sku_id"] == "B-BLUE-L")


def plan(client, order_id, qty):
    order = client.get(f"/orders/{order_id}").json()
    changes = (
        [{"mode": "replace_remaining", "from_sku": "A-BLACK-M", "to_sku": "B-BLUE-L", "qty": qty}]
        if order_id == "SO-1001"
        else [{"mode": "increment", "sku_id": "B-BLUE-L", "qty": qty}]
    )
    return client.post(
        "/plans",
        json={
            "order_id": order_id,
            "base_revision": order["current_revision"],
            "intent_revision": order["intent_revision"],
            "changes": changes,
            "client_event_key": str(uuid4()),
        },
    )


def approve(client, planned):
    response = client.post(
        f"/plans/{planned['plan_id']}/approve",
        json={
            "plan_hash": planned["plan_hash"],
            "expected_intent_revision": planned["intent_revision"],
        },
    )
    assert response.status_code == 200, response.text
    return f"/plans/{planned['plan_id']}/commit", {
        "plan_hash": planned["plan_hash"],
        "approval_id": response.json()["approval_id"],
        "idempotency_key": str(uuid4()),
    }


def count(factory):
    with factory() as session:
        return session.scalar(select(func.count()).select_from(Operation))


@pytest.mark.parametrize("failed_commit_first", [False, True], ids=["active", "revoked"])
def test_stock_change_vetoes_stale_approval_and_reapproval_then_replanning_succeeds(
    seeded, database_url, tmp_path, failed_commit_first
):
    with real_http(database_url, tmp_path) as (client, _):
        assert stock(client)["available"] == 20
        first = plan(client, "SO-1001", 15).json()
        second = plan(client, "SO-1002", 15).json()
        first_url, first_body = approve(client, first)
        second_url, second_body = approve(client, second)
        winner = client.post(first_url, json=first_body)
        assert winner.status_code == 200
        remaining = stock(client)
        assert remaining["reserved"] == 25 and remaining["available"] == 5
        if failed_commit_first:
            assert client.post(second_url, json=second_body).status_code == 409
        before = support.dump(database_url)
        rejected = client.post(
            f"/plans/{second['plan_id']}/approve",
            json={
                "plan_hash": second["plan_hash"],
                "expected_intent_revision": second["intent_revision"],
            },
        )
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "STALE_DEPENDENCY"
        assert "重新规划" in rejected.json()["error"]["message"]
        assert support.dump(database_url) == before  # No unusable approval or other writes.
        html, page = journey.page(client, second["task_id"])
        assert "disabled" in page.element("approve")
        assert page.element("stale-plan-guidance")
        assert "核对当前订单与库存" in html and "重新规划" in html
        assert client.post(second_url, json=second_body).status_code == 409
        assert stock(client) == remaining and count(seeded) == 1
        insufficient = plan(client, "SO-1002", 15)
        assert (
            insufficient.status_code == 422 and insufficient.json()["error"]["code"] == "SHORTAGE"
        )
        assert "缺口 10" in insufficient.json()["error"]["message"]
        assert stock(client) == remaining and count(seeded) == 1
        recovered = plan(client, "SO-1002", 5)
        assert recovered.status_code == 200
        recovery_url, recovery_body = approve(client, recovered.json())
        completed = client.post(recovery_url, json=recovery_body)
        assert completed.status_code == 200
        final = stock(client)
        assert (final["on_hand"], final["reserved"], final["available"]) == (30, 30, 0)
        assert count(seeded) == 2
        with seeded() as session:
            assert (
                session.scalar(
                    select(func.sum(Reservation.qty)).where(
                        Reservation.warehouse_id == "W1", Reservation.sku_id == "B-BLUE-L"
                    )
                )
                == final["reserved"]
            )
            assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0
        before = support.dump(database_url)
        assert (
            client.post(first_url, json=first_body).json()["operation_id"]
            == winner.json()["operation_id"]
        )
        assert (
            client.post(recovery_url, json=recovery_body).json()["operation_id"]
            == completed.json()["operation_id"]
        )
        assert support.dump(database_url) == before


def test_two_simultaneous_order_commits_do_not_oversell_shared_stock(
    seeded, database_url, tmp_path
):
    with real_http(database_url, tmp_path) as (client, _):
        plans = {order: plan(client, order, 15).json() for order in ["SO-1001", "SO-1002"]}
        requests = {order: approve(client, planned) for order, planned in plans.items()}
        barrier = Barrier(2)

        def submit(order):
            with httpx.Client(base_url=client.base_url, trust_env=False, timeout=15) as own:
                barrier.wait(10)
                url, body = requests[order]
                response = own.post(url, json=body)
                return order, response

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, plans))
        assert sorted(response.status_code for _, response in outcomes) == [200, 409]
        loser = next(order for order, response in outcomes if response.status_code == 409)
        assert stock(client)["available"] == 5 and count(seeded) == 1
        fresh = plan(client, loser, 5)
        assert fresh.status_code == 200
        url, body = approve(client, fresh.json())
        assert client.post(url, json=body).status_code == 200
        assert stock(client)["available"] == 0 and stock(client)["reserved"] == 30
        assert count(seeded) == 2
        view = client.get(f"/tasks/{fresh.json()['task_id']}/view").json()
        assert view["result"]["verification"] == "partial"
        assert view["result"]["verification_details"]["global_completeness"] == "not_assessed"


def test_independent_new_intent_blocks_old_task_and_explains_next_step(
    seeded, database_url, tmp_path
):
    with real_http(database_url, tmp_path) as (client, _):

        def create():
            page = journey.Page(client.get("/ui/demo/submit").text)
            response = client.post(
                "/ui/demo/submit",
                data={
                    "selected_order_id": "SO-1001",
                    "text": "SO-1001 把未发的 40 件黑色 M 改成白色 M",
                    "client_event_key": page.field("client_event_key"),
                    "thread_id": page.field("thread_id"),
                },
            )
            assert response.status_code == 303
            return response.headers["location"].split("/")[-1]

        first = create()
        planned = journey.worker(client, first)
        _, page = journey.page(client, first)
        actions = page.element("plan-actions")
        first_plan = {
            "plan_id": actions["data-plan-id"],
            "plan_hash": actions["data-plan-hash"],
            "intent_revision": planned["card"]["intent_revision"],
        }
        first_url, first_body = approve(client, first_plan)
        stock_before = client.get("/stock").json()["rows"]
        second = create()
        assert client.post(first_url, json=first_body).status_code == 409
        assert count(seeded) == 0 and client.get("/stock").json()["rows"] == stock_before
        html, old_page = journey.page(client, first)
        assert old_page.element("stale-plan-guidance")
        assert "已经完成时不要重复执行" in html and 'href="/ui/demo/tasks"' in html
        journey.worker(client, second)
        journey.approve_commit_replay(client, second, database_url, seeded)
        before = support.dump(database_url)
        assert client.post(first_url, json=first_body).status_code == 409
        assert support.dump(database_url) == before and count(seeded) == 1
