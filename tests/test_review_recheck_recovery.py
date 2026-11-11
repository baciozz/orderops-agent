"""Synthetic upgrade/recovery regressions; no live model or production data."""

import csv
from copy import deepcopy
from io import BytesIO, StringIO
from pathlib import Path

import openpyxl
import pytest
from app.config import Settings
from app.db.models import ReviewWorkspace
from app.main import create_app
from app.review.contracts import Snapshot
from app.review.rules import scope
from fastapi.testclient import TestClient
from tests.test_review_documents import pdf
from tests.test_review_sources import adopt, correct_and_confirm, start, upload
from tests.test_review_workspace import input_body, proposal


@pytest.fixture()
def client(engine, database_url):
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        yield client


def legacy_noop(client, sessions):
    created = client.post("/reviews", json=input_body(request_key="legacy-noop")).json()
    candidate = proposal()
    candidate["changes"][0].update(quantity=50, after_qty=50, citations=[])
    with sessions() as session, session.begin():
        row = session.get(ReviewWorkspace, created["id"])
        raw = deepcopy(row.data)
        snapshot = Snapshot.model_validate(raw["input"]["snapshot"])
        raw.update(
            proposal=candidate,
            human=True,
            confirmed_revision=row.revision,
            confirmed_at="2026-11-09T00:00:00Z",
            check={
                "status": "ready",
                "issues": [],
                "rows": [],
                "downstream": [],
                "scope": scope(snapshot, snapshot.orders[0].revision),
                "source_map": {
                    "1": {
                        "line": 1,
                        "text": raw["input"]["instruction"],
                        "source_id": "INPUT",
                        "locator": "客户文字第1行",
                        "human": False,
                    }
                },
            },
        )
        row.data = raw
        row.status = "CONFIRMED"
        row.revision += 1
    return created["id"], candidate


def test_legacy_noop_recheck_and_explicit_human_no_change(client, session_factory):
    review_id, legacy_proposal = legacy_noop(client, session_factory)
    item = client.get(f"/reviews/{review_id}").json()
    assert item["status"] == "CONFIRMED" and item["recheck_required"] is True
    assert item["recheck_reason"] == "rules_or_context"
    assert item["data"]["check"]["status"] == "blocked"
    assert any("未产生数量变化" in x for x in item["data"]["check"]["issues"])
    with session_factory() as session:
        persisted = session.get(ReviewWorkspace, review_id)
        assert persisted.status == "CONFIRMED" and persisted.data["check"]["status"] == "ready"
        assert persisted.data["proposal"] == legacy_proposal
        legacy_check = deepcopy(persisted.data["check"])
        legacy_revision = persisted.data["confirmed_revision"]
    page = client.get(f"/ui/reviews/{review_id}").text
    assert "历史确认需重新核对" in page and "未产生数量变化" in page
    assert "下载 CSV 清单" not in page and "下载 XLSX 清单" not in page
    for fmt in ("csv", "xlsx"):
        response = client.get(f"/reviews/{review_id}/export?fmt={fmt}")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "REVIEW_RECHECK_REQUIRED"
        assert "重新核对" in response.json()["error"]["message"]
    assert (
        client.post(
            f"/reviews/{review_id}/confirm", json={"revision": item["revision"] - 1}
        ).status_code
        == 409
    )

    # Nothing is silently converted; explicit checkbox, nonblank reason and
    # the current revision are all required before a separate confirmation.
    for payload in (
        {
            "revision": str(item["revision"]),
            "no_change_reason": "   ",
            "acknowledge_no_change": "yes",
        },
        {
            "revision": str(item["revision"]),
            "no_change_reason": "已核对",
            "acknowledge_no_change": "no",
        },
    ):
        assert client.post(f"/ui/reviews/{review_id}/no-change", data=payload).status_code == 400
    stale = client.post(
        f"/ui/reviews/{review_id}/no-change",
        data={
            "revision": str(item["revision"] - 1),
            "no_change_reason": "已核对",
            "acknowledge_no_change": "yes",
        },
    )
    assert stale.status_code == 409
    response = client.post(
        f"/ui/reviews/{review_id}/no-change",
        data={
            "revision": str(item["revision"]),
            "no_change_reason": "原需求确认维持原量",
            "acknowledge_no_change": "yes",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    revised = client.get(f"/reviews/{review_id}").json()
    assert revised["status"] == "NEEDS_REVIEW" and revised["data"]["human"] is True
    assert revised["data"]["proposal"]["disposition"] == "no_change"
    assert revised["data"]["proposal"]["changes"] == []
    assert revised["data"]["history"][-1]["reason"] == "原需求确认维持原量"
    assert revised["data"]["history"][-1]["previous_proposal"] == legacy_proposal
    assert client.get(f"/reviews/{review_id}/export?fmt=csv").status_code == 409
    assert (
        client.post(
            f"/reviews/{review_id}/confirm", json={"revision": revised["revision"]}
        ).status_code
        == 200
    )
    confirmed = client.get(f"/reviews/{review_id}").json()
    archival = next(
        h["previous_confirmation"]
        for h in confirmed["data"]["history"]
        if h["kind"] == "confirmation_superseded"
    )
    assert archival["proposal"] == legacy_proposal and archival["check"] == legacy_check
    assert archival["human"] is True and archival["confirmed_revision"] == legacy_revision
    assert archival["confirmed_at"] == "2026-11-09T00:00:00Z"
    assert archival["workspace_revision"] == item["revision"]
    assert archival["input"] == item["data"]["input"]
    for fmt in ("csv", "xlsx"):
        response = client.get(f"/reviews/{review_id}/export?fmt={fmt}")
        assert response.status_code == 200
        if fmt == "csv":
            row = list(csv.DictReader(StringIO(response.content.decode("utf-8-sig"))))[0]
        else:
            book = openpyxl.load_workbook(BytesIO(response.content), read_only=True, data_only=True)
            values = list(book.active.values)
            book.close()
            row = dict(zip(values[0], values[1], strict=True))
        assert "无变更" in row["todo"] and "未执行订单" in row["state"]


def test_missing_source_retains_distinct_recovery_code(client, session_factory):
    item = start(client)
    content = "00123藏蓝M总量改成36件，已经发出的保持。".encode()
    result = upload(client, item, "synthetic.txt", content).json()
    item = adopt(client, result["review"], result["source_id"]).json()
    item = correct_and_confirm(client, item)
    with session_factory() as session:
        original = Path(session.get(ReviewWorkspace, item["id"]).data["assets"][0]["path"])
    original.write_bytes(b"changed synthetic source")
    view = client.get(f"/reviews/{item['id']}").json()
    assert view["recheck_required"] and view["recheck_reason"] == "source"
    page = client.get(f"/ui/reviews/{item['id']}").text
    assert "恢复来源" in page and "下载 CSV 清单" not in page
    for fmt in ("csv", "xlsx"):
        response = client.get(f"/reviews/{item['id']}/export?fmt={fmt}")
        assert (
            response.status_code == 409
            and response.json()["error"]["code"] == "REVIEW_SOURCE_UNAVAILABLE"
        )
    restored = client.post(
        f"/reviews/{item['id']}/sources/{result['source_id']}/restore",
        data={"revision": view["revision"], "key": "restore-synthetic"},
        files={"file": ("synthetic.txt", content)},
    )
    assert restored.status_code == 200 and not restored.json()["recheck_required"]
    assert client.get(f"/reviews/{item['id']}/export?fmt=csv").status_code == 200


def test_new_warnings_on_legacy_confirmed_mixed_proposal_require_recheck(client, session_factory):
    body = input_body(request_key="legacy-mixed")
    snapshot = body["snapshot"]
    snapshot["orders"][0]["lines"].append(
        {"sku_id": "00123-白-M", "ordered_qty": 30, "shipped_qty": 0, "cut_qty": None}
    )
    created = client.post("/reviews", json=body).json()
    candidate = proposal()
    candidate["changes"].append(
        {
            "mode": "set_total",
            "sku_id": "00123-白-M",
            "to_sku": None,
            "quantity": 30,
            "unit": "件",
            "before_qty": 30,
            "after_qty": 30,
            "citations": [],
        }
    )
    corrected = client.post(
        f"/reviews/{created['id']}/correct",
        json={"revision": created["revision"], "proposal": candidate, "reason": "合成资料核对"},
    ).json()
    assert corrected["data"]["check"]["warnings"]
    with session_factory() as session, session.begin():
        row = session.get(ReviewWorkspace, created["id"])
        raw = deepcopy(row.data)
        raw["check"].pop("warnings")
        raw["confirmed_revision"] = row.revision
        row.data = raw
        row.status = "CONFIRMED"
        row.revision += 1
    view = client.get(f"/reviews/{created['id']}").json()
    assert view["recheck_required"] and view["data"]["check"]["warnings"]
    assert client.get(f"/reviews/{created['id']}/export?fmt=csv").status_code == 409
    corrected = client.post(
        f"/reviews/{created['id']}/correct",
        json={
            "revision": view["revision"],
            "proposal": candidate,
            "reason": "已复核包含零变化项的合成提案",
        },
    ).json()
    assert corrected["status"] == "NEEDS_REVIEW" and corrected["data"]["check"]["warnings"]
    assert (
        client.post(
            f"/reviews/{created['id']}/confirm", json={"revision": corrected["revision"]}
        ).status_code
        == 200
    )
    for fmt in ("csv", "xlsx"):
        response = client.get(f"/reviews/{created['id']}/export?fmt={fmt}")
        assert response.status_code == 200
        if fmt == "csv":
            exported = list(csv.DictReader(StringIO(response.content.decode("utf-8-sig"))))[0]
        else:
            book = openpyxl.load_workbook(BytesIO(response.content), read_only=True, data_only=True)
            values = list(book.active.values)
            book.close()
            exported = dict(zip(values[0], values[1], strict=True))
        assert "未产生数量变化" in exported["todo"]


@pytest.mark.parametrize("disposition", ["propose", "no_change"])
def test_old_pdf_source_map_without_new_positions_remains_valid(
    client, session_factory, disposition
):
    item = start(client)
    content = pdf([["合成订单 00123 藏蓝 M 总量改成36件"]])
    uploaded = upload(client, item, "synthetic.pdf", content).json()
    item = adopt(client, uploaded["review"], uploaded["source_id"]).json()
    candidate = proposal()
    if disposition == "no_change":
        candidate.update(disposition="no_change", changes=[], summary="人工确认无需变更")
    corrected = client.post(
        f"/reviews/{item['id']}/correct",
        json={"revision": item["revision"], "proposal": candidate, "reason": "已复核合成 PDF"},
    ).json()
    confirmed = client.post(
        f"/reviews/{item['id']}/confirm", json={"revision": corrected["revision"]}
    ).json()
    assert confirmed["status"] == "CONFIRMED"
    with session_factory() as session, session.begin():
        row = session.get(ReviewWorkspace, item["id"])
        raw = deepcopy(row.data)
        assert any("source_page" in line for line in raw["check"]["source_map"].values())
        for line in raw["check"]["source_map"].values():
            line.pop("source_page", None)
            line.pop("source_extracted_line", None)
        raw["check"].pop("warnings", None)
        row.data = raw
    view = client.get(f"/reviews/{item['id']}").json()
    assert view["status"] == "CONFIRMED" and not view["recheck_required"]
    assert (
        client.post(
            f"/reviews/{item['id']}/confirm", json={"revision": corrected["revision"]}
        ).status_code
        == 200
    )
    for fmt in ("csv", "xlsx"):
        assert client.get(f"/reviews/{item['id']}/export?fmt={fmt}").status_code == 200


def test_no_change_with_unresolved_questions_is_never_confirmable_or_exportable(
    client, session_factory
):
    item = start(client)
    candidate = proposal()
    candidate.update(
        disposition="no_change",
        changes=[],
        questions=["客户是取消订单还是保持原样？"],
        summary="需要客户明确意图",
    )
    item = client.post(
        f"/reviews/{item['id']}/correct",
        json={"revision": item["revision"], "proposal": candidate, "reason": "合成测试"},
    ).json()
    assert item["data"]["check"]["status"] == "clarify"
    assert item["data"]["check"]["issues"] == candidate["questions"]
    assert (
        client.post(
            f"/reviews/{item['id']}/confirm", json={"revision": item["revision"]}
        ).status_code
        == 409
    )
    # Simulate a previous version which allowed this confirmation.
    with session_factory() as session, session.begin():
        row = session.get(ReviewWorkspace, item["id"])
        raw = deepcopy(row.data)
        raw["check"]["status"] = "no_change"
        raw["confirmed_revision"] = row.revision
        raw["confirmed_at"] = "2026-11-09T00:00:00Z"
        row.data = raw
        row.status = "CONFIRMED"
        row.revision += 1
    old = client.get(f"/reviews/{item['id']}").json()
    assert old["recheck_required"] and old["data"]["check"]["status"] == "clarify"
    for fmt in ("csv", "xlsx"):
        result = client.get(f"/reviews/{item['id']}/export?fmt={fmt}")
        assert result.status_code == 409
        assert result.json()["error"]["code"] == "REVIEW_RECHECK_REQUIRED"
