"""Advisory similarity must never suppress new intent or replace idempotency."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from app.config import Settings
from app.db.models import ReviewWorkspace
from app.main import create_app
from app.review import service
from app.review.contracts import CreateReview
from fastapi.testclient import TestClient
from scripts.accept_checklist import business_counts
from tests.test_review_workspace import input_body, proposal


@pytest.fixture
def client(database_url, session_factory):
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)),
        base_url="http://127.0.0.1",
    ) as value:
        yield value


def create(client, key, text="00123藏蓝M，总量改为37件，谢谢。", **extra):
    response = client.post("/reviews", json=input_body(request_key=key, instruction=text, **extra))
    assert response.status_code == 200
    return response.json()


def test_surface_hint_keeps_both_tasks_and_allows_explicit_new_change(
    client, database_url, session_factory
):
    from pathlib import Path

    database = Path(database_url.removeprefix("sqlite:///"))
    before = business_counts(database)
    first = create(client, "first", "00123藏蓝M，再加12件，谢谢。")
    second = create(client, "second", "00123 藏蓝 M，再加 12 件，谢谢！")
    assert first["id"] != second["id"]
    assert second["possible_duplicates"] == [{"id": first["id"], "status": "DRAFT"}]
    html = client.get("/ui/reviews/" + second["id"]).text
    assert "相似请求提示" in html and "若是新的追加或修改，继续核对即可" in html
    assert create(client, "second", "00123 藏蓝 M，再加 12 件，谢谢！")["id"] == second["id"]
    assert (
        client.post(
            "/reviews", json=input_body(request_key="second", instruction="不同需求")
        ).status_code
        == 409
    )
    payload = proposal()
    payload["changes"][0].update(mode="increment", quantity=12, after_qty=62)
    corrected = client.post(
        f"/reviews/{second['id']}/correct",
        json={
            "revision": second["revision"],
            "proposal": payload,
            "reason": "确认是另一条追加需求",
        },
    ).json()
    assert corrected["data"]["check"]["status"] == "ready"
    assert (
        client.post(
            f"/reviews/{second['id']}/confirm", json={"revision": corrected["revision"]}
        ).status_code
        == 200
    )
    unchanged = client.get("/reviews/" + first["id"]).json()
    assert unchanged["status"] == "DRAFT" and unchanged["revision"] == 1
    assert unchanged["data"]["proposal"] is None
    assert business_counts(database) == before
    with session_factory() as session:
        assert session.query(ReviewWorkspace).count() == 2


@pytest.mark.parametrize("difference", ["quantity", "sign", "order", "snapshot", "cancelled"])
def test_unrelated_or_changed_facts_do_not_get_duplicate_hint(client, difference):
    first = create(client, "first", "00123藏蓝M再加12件")
    text = "00123藏蓝M再加12件"
    extra = {}
    if difference == "quantity":
        text = "00123藏蓝M再加13件"
    elif difference == "sign":
        text = "00123藏蓝M再加-12件"
    elif difference == "order":
        snapshot = deepcopy(input_body()["snapshot"])
        snapshot["orders"][0]["order_id"] = "SYN-ANOTHER"
        extra = {"snapshot": snapshot, "selected_order_id": "SYN-ANOTHER"}
    elif difference == "snapshot":
        snapshot = deepcopy(input_body()["snapshot"])
        snapshot["inventory"][0]["available_qty"] = 24
        extra = {"snapshot": snapshot}
    else:
        assert (
            client.post(
                f"/reviews/{first['id']}/cancel", json={"revision": first["revision"]}
            ).status_code
            == 200
        )
    second = create(client, "second", text, **extra)
    assert second["possible_duplicates"] == []


def upload(client, item, content):
    response = client.post(
        f"/reviews/{item['id']}/sources",
        data={"revision": item["revision"], "key": "file"},
        files={"file": ("opaque.txt", content.encode())},
    )
    assert response.status_code == 200
    result = response.json()
    return result["review"], result["source_id"]


def adopt(client, item, source):
    response = client.post(
        f"/reviews/{item['id']}/sources/{source}/confirm",
        json={
            "revision": item["revision"],
            "role": "instruction",
            "fields_checked": True,
            "context_complete": True,
        },
    )
    assert response.status_code == 200
    return response.json()


def test_files_compare_adopted_content_not_placeholder_or_new_source_ids(client):
    first = create(client, "first", "客户要求见附件")
    first, sid1 = upload(client, first, "00123藏蓝M总量37件")
    first = adopt(client, first, sid1)
    second = create(client, "second", "客户要求见附件")
    assert not second["possible_duplicates"]
    second, sid2 = upload(client, second, "00123藏蓝M总量37件")
    assert sid1 != sid2 and not second["possible_duplicates"]
    second = adopt(client, second, sid2)
    assert second["possible_duplicates"][0]["id"] == first["id"]
    third = create(client, "third", "客户要求见附件")
    third, sid3 = upload(client, third, "00123藏蓝M总量38件")
    assert not adopt(client, third, sid3)["possible_duplicates"]


def test_concurrent_new_keys_remain_independent_and_refresh_is_read_only(session_factory):
    def start(key):
        return service.create(
            session_factory, CreateReview.model_validate(input_body(request_key=key))
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(start, ["first", "second"]))
    assert first != second
    with session_factory() as session:
        before = {
            row.id: (row.revision, deepcopy(row.data)) for row in session.query(ReviewWorkspace)
        }
    assert service.view(session_factory, first)["possible_duplicates"][0]["id"] == second
    assert service.view(session_factory, second)["possible_duplicates"][0]["id"] == first
    with session_factory() as session:
        assert before == {
            row.id: (row.revision, row.data) for row in session.query(ReviewWorkspace)
        }
