"""Complete human action lists retain other edits and invalidate old confirmations."""

import copy
import html
import json
import re

import pytest
from app.review.sample import SAMPLE_SNAPSHOT
from tests.test_review_sources import client as _review_client

client = _review_client


def start_two_lines(client):
    snapshot = copy.deepcopy(SAMPLE_SNAPSHOT)
    snapshot["orders"][0]["lines"].append(dict(sku_id="00123-白-M", ordered_qty=30, shipped_qty=0))
    item = client.post(
        "/reviews",
        json=dict(
            request_key="two-human-actions",
            selected_order_id="SYN-2401",
            instruction="藏蓝M改成36件，白M再减5件；合成资料",
            snapshot=snapshot,
        ),
    ).json()
    return item


def rows():
    return [
        dict(sku_id="00123-藏蓝-M", mode="set_total", quantity="36", to_sku=""),
        dict(sku_id="00123-白-M", mode="increment", quantity="-5", to_sku=""),
    ]


def post(client, item, changes):
    return client.post(
        f"/ui/reviews/{item['id']}/correct",
        data=dict(
            revision=item["revision"],
            changes_json=json.dumps(changes),
            reason="人工对照两行，不调用模型",
        ),
    )


def test_two_row_edit_preserves_other_action_and_explicit_confirmation(client):
    item = start_two_lines(client)
    saved = post(client, item, rows())
    assert saved.status_code == 200 and "保存全部人工动作" in saved.text
    current = client.get(f"/reviews/{item['id']}").json()
    assert [(row["sku_id"], row["new_qty"]) for row in current["data"]["check"]["rows"]] == [
        ("00123-白-M", 25),
        ("00123-藏蓝-M", 36),
    ]
    confirmed = client.post(
        f"/reviews/{item['id']}/confirm", json={"revision": current["revision"]}
    ).json()
    edited = rows()
    edited[0]["quantity"] = "37"
    assert post(client, confirmed, edited).status_code == 200
    newest = client.get(f"/reviews/{item['id']}").json()
    assert newest["status"] == "NEEDS_REVIEW" and len(newest["data"]["proposal"]["changes"]) == 2
    assert [(r["new_qty"]) for r in newest["data"]["check"]["rows"]] == [25, 37]
    assert client.get(f"/reviews/{item['id']}/export").status_code == 409
    assert (
        client.post(
            f"/reviews/{item['id']}/confirm", json={"revision": confirmed["revision"]}
        ).status_code
        == 409
    )
    page = client.get(f"/ui/reviews/{item['id']}").text
    serialized = re.search(r'id="review-changes"[^>]*value="([^"]*)"', page).group(1)
    assert [row["quantity"] for row in json.loads(html.unescape(serialized))] == [37, -5]


@pytest.mark.parametrize("quantity", [1.5, True, "1.5", "--3"])
def test_browser_payload_does_not_truncate_invalid_quantity_or_replace_current_proposal(
    client, quantity
):
    item = start_two_lines(client)
    bad = rows()
    bad[0]["quantity"] = quantity
    result = post(client, item, bad)
    assert result.status_code == 400 and "人工对照两行" in result.text
    current = client.get(f"/reviews/{item['id']}").json()
    assert current["revision"] == 1 and current["data"]["proposal"] is None


def test_overlapping_actions_cannot_be_confirmed(client):
    item = start_two_lines(client)
    assert post(client, item, [rows()[0], rows()[0]]).status_code == 200
    current = client.get(f"/reviews/{item['id']}").json()
    assert current["data"]["check"]["status"] == "blocked"
    assert (
        client.post(
            f"/reviews/{item['id']}/confirm", json={"revision": current["revision"]}
        ).status_code
        == 409
    )
