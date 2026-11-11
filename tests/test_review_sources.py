"""File upload, source confirmation, immutable readback and real export recovery."""

import csv
import json
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from app.config import Settings
from app.db.models import ReviewWorkspace
from app.main import create_app
from app.review import service
from app.review.mapping import suggested_columns
from fastapi.testclient import TestClient
from tests.test_review_documents import book, pdf
from tests.test_review_workspace import configured, input_body, model, proposal, response


@pytest.fixture()
def client(engine, database_url):
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        yield client


def start(client, *, draft=False):
    if draft:
        response = client.post("/reviews/drafts", json={"request_key": uuid4().hex})
    else:
        response = client.post("/reviews", json=input_body(request_key=uuid4().hex))
    assert response.status_code == 200, response.text
    return response.json()


def upload(client, item, name, data, *, key=None, **options):
    response = client.post(
        f"/reviews/{item['id']}/sources",
        data={"revision": item["revision"], "key": key or uuid4().hex, **options},
        files={"file": (name, data)},
    )
    return response


def adopt(client, item, source_id, role="instruction", *, mapping=None, **overrides):
    return client.post(
        f"/reviews/{item['id']}/sources/{source_id}/confirm",
        json={
            "revision": item["revision"],
            "role": role,
            "mapping": mapping,
            "fields_checked": True,
            "context_complete": True,
            "note": "合成验收已核对",
            **overrides,
        },
    )


def correct_and_confirm(client, item):
    response = client.post(
        f"/reviews/{item['id']}/correct",
        json={
            "revision": item["revision"],
            "proposal": proposal(),
            "reason": "人工核对合成输入，不冒充模型理解",
        },
    )
    assert response.status_code == 200
    item = response.json()
    assert item["data"]["check"]["status"] == "ready", item["data"]["check"]
    response = client.post(f"/reviews/{item['id']}/confirm", json={"revision": item["revision"]})
    assert response.status_code == 200
    return response.json()


def test_upload_role_confirmation_file_hash_and_export_recovery(client, session_factory):
    item = start(client)
    content = "00123藏蓝M总量改成36件，已经发出的保持。".encode()
    uploaded = upload(client, item, "opaque.txt", content, key="same-key")
    assert uploaded.status_code == 200
    result = uploaded.json()
    item = result["review"]
    sid = result["source_id"]
    asset = item["data"]["assets"][0]
    assert not asset["confirmed"] and asset["role"] == "unknown"
    assert "path" not in asset and "/tmp/" not in json.dumps(item)
    blocked = client.post(
        f"/reviews/{item['id']}/correct",
        json={"revision": item["revision"], "proposal": proposal(), "reason": "尚未确认新文件"},
    ).json()
    assert blocked["data"]["check"]["status"] == "blocked"
    item = blocked
    rejected = adopt(client, item, sid, fields_checked=False)
    assert rejected.status_code == 422
    item = adopt(client, item, sid).json()
    assert client.get(f"/reviews/{item['id']}/sources/{sid}/original").content == content
    item = correct_and_confirm(client, item)
    revision = item["revision"]
    same = upload(client, item, "opaque.txt", content, key="different-key").json()
    assert same["duplicate"] and same["source_id"] == sid
    assert same["review"]["revision"] == revision and same["review"]["status"] == "CONFIRMED"
    assert upload(client, item, "opaque.txt", b"changed", key="same-key").status_code == 422
    with session_factory() as session:
        raw = session.get(ReviewWorkspace, item["id"]).data["assets"][0]
        original = Path(raw["path"])
        assert original.stat().st_mode & 0o077 == 0
        original.write_bytes(b"changed synthetic fault")
    blocked = client.get(f"/reviews/{item['id']}/export")
    assert (
        blocked.status_code == 409
        and blocked.json()["error"]["code"] == "REVIEW_SOURCE_UNAVAILABLE"
    )
    assert not client.get(f"/reviews/{item['id']}").json()["data"]["assets"][0]["available"]
    wrong = client.post(
        f"/reviews/{item['id']}/sources/{sid}/restore",
        data={"revision": revision, "key": "restore"},
        files={"file": ("opaque.txt", b"wrong")},
    )
    assert wrong.status_code == 422
    restored = client.post(
        f"/reviews/{item['id']}/sources/{sid}/restore",
        data={"revision": revision, "key": "restore"},
        files={"file": ("opaque.txt", content)},
    )
    assert restored.status_code == 200 and restored.json()["status"] == "CONFIRMED"
    csv_data = client.get(f"/reviews/{item['id']}/export").content.decode("utf-8-sig")
    rows = list(csv.DictReader(StringIO(csv_data)))
    assert len(rows) == 1 and rows[0]["new_qty"] == "36"
    assert sid in rows[0]["instruction_source"] and "人工提案" in rows[0]["instruction_source"]


def test_csv_baseline_inventory_and_fulfillment_import_are_order_independent(client):
    item = start(client, draft=True)
    supplied = [
        ("inventory.csv", "inventory", "款号,颜色,尺码,可用库存\n00123,藏蓝,M,25\n00123,白,M,30\n"),
        (
            "orders.csv",
            "orders",
            "订单号,版本,款号,颜色,尺码,履约方式,数量\nSYN-A,1,00123,藏蓝,M,STOCK,50\n",
        ),
        (
            "shipped.csv",
            "fulfillment",
            "订单号,版本,款号,颜色,尺码,已发量\nSYN-A,1,00123,藏蓝,M,10\n",
        ),
    ]
    for name, role, text in supplied:
        uploaded = upload(client, item, name, text.encode()).json()
        item, sid = uploaded["review"], uploaded["source_id"]
        asset = next(a for a in item["data"]["assets"] if a["id"] == sid)
        spec = {
            "table_id": "T1",
            "columns": suggested_columns(asset["document"]["tables"][0]["rows"][0]),
        }
        adopted = adopt(client, item, sid, role, mapping=spec)
        assert adopted.status_code == 200, adopted.text
        item = adopted.json()
    applied = client.post(
        f"/reviews/{item['id']}/sources/apply-baseline",
        json={"revision": item["revision"], "captured_at": "2026-11-09", "synthetic": True},
    )
    assert applied.status_code == 200, applied.text
    item = applied.json()
    snapshot = item["data"]["input"]["snapshot"]
    assert item["data"]["input"]["selected_order_id"] is None
    assert snapshot["catalog"][0]["style"] == "00123" and len(snapshot["catalog"]) == 2
    assert snapshot["orders"][0]["lines"][0]["shipped_qty"] == 10
    wrong = client.post(
        f"/reviews/{item['id']}/select-order",
        json={"revision": item["revision"], "order_id": "wrong"},
    )
    assert wrong.status_code == 422
    selected = client.post(
        f"/reviews/{item['id']}/select-order",
        json={"revision": item["revision"], "order_id": "SYN-A"},
    )
    assert selected.status_code == 200


def test_new_attachment_invalidates_confirmation_and_unreadable_exclusion_is_explicit(
    client, session_factory
):
    item = correct_and_confirm(client, start(client))
    uploaded = upload(client, item, "new.txt", "新的合成要求，总量改为35。".encode()).json()
    item, sid = uploaded["review"], uploaded["source_id"]
    assert item["status"] == "DRAFT" and item["data"]["proposal"] is None
    assert client.get(f"/reviews/{item['id']}/export").status_code == 409
    with session_factory() as session:
        Path(session.get(ReviewWorkspace, item["id"]).data["assets"][0]["path"]).unlink()
    excluded = adopt(
        client, item, sid, "excluded", note="此文件误传且已丢失，明确不采用；未覆盖范围保留"
    )
    assert excluded.status_code == 200
    item = correct_and_confirm(client, excluded.json())
    assert "明确未采用" in item["data"]["check"]["scope"]


def test_same_filename_different_content_requires_relationship_not_silent_replacement(client):
    item = start(client)
    first = upload(client, item, "same.txt", "总量改成36件".encode()).json()
    item = adopt(client, first["review"], first["source_id"], note="").json()
    second = upload(client, item, "same.txt", "总量改成35件".encode()).json()
    item = second["review"]
    sid = second["source_id"]
    assert len(item["data"]["assets"]) == 2
    refused = adopt(client, item, sid, note="")
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "SOURCE_CONFLICT"
    accepted = adopt(client, item, sid, note="两个要求存在冲突，需追问客户，不能默认采用新文件")
    assert accepted.status_code == 200


def test_manual_transcription_preserves_raw_pdf_and_exact_source_identity(client):
    item = start(client)
    uploaded = upload(client, item, "opaque.pdf", pdf([["模拟订单 00123 藏蓝 M 数量36"]])).json()
    item, sid = uploaded["review"], uploaded["source_id"]
    original_doc = item["data"]["assets"][0]["document"]
    changed = client.post(
        f"/reviews/{item['id']}/sources/{sid}/correct",
        json={
            "revision": item["revision"],
            "text": "人工核对，00123藏蓝M总量36件",
            "reason": "核对原PDF后补充明确数量语义",
        },
    )
    assert changed.status_code == 200
    item = changed.json()
    asset = item["data"]["assets"][0]
    assert asset["document"] == original_doc and not asset["confirmed"]
    item = adopt(client, item, sid).json()
    confirmed = correct_and_confirm(client, item)
    refs = confirmed["data"]["check"]["source_map"].values()
    assert any(
        ref["source_id"] == sid and ref["human"] and "人工转录" in ref["locator"] for ref in refs
    )


def test_machine_citations_use_real_file_locations_and_fabrication_is_blocked(
    session_factory, database_url, engine
):
    from app.review import assets
    from app.review.contracts import CreateReview, ExtractRequest

    item_id = service.create(session_factory, CreateReview.model_validate(input_body()))
    settings = configured(database_url)
    sid, _ = assets.upload(
        session_factory,
        item_id,
        revision=1,
        key="file",
        filename="opaque.pdf",
        data=pdf([["模拟客户要求：总数36件"]]),
        database_url=database_url,
        storage_dir=None,
    )
    item = service.view(session_factory, item_id)
    assets.confirm(
        session_factory,
        item_id,
        sid,
        assets.AssetConfirmation(
            revision=item["revision"],
            role="instruction",
            fields_checked=True,
            context_complete=True,
        ),
    )
    payload = proposal()
    payload["changes"][0]["citations"] = [{"line": 2, "quote": "总数36件"}]
    client_model = model(session_factory, settings, lambda _: response(payload))
    current = service.view(session_factory, item_id)
    service.extract(
        session_factory,
        settings,
        item_id,
        ExtractRequest(
            revision=current["revision"], request_key="model", acknowledge_transmission=True
        ),
        client=client_model,
    )
    current = service.view(session_factory, item_id)
    assert current["data"]["check"]["status"] == "ready"
    assert current["data"]["check"]["source_map"]["2"]["source_id"] == sid
    assert "PDF第1页" in current["data"]["check"]["source_map"]["2"]["locator"]


@pytest.mark.parametrize(
    "name,content", [("bad.csv", b"\xff\xfe\x00"), ("bad.xlsx", b"corrupt"), ("bad.xlsm", b"macro")]
)
def test_failed_attachment_preserves_current_input_and_allows_replacement(client, name, content):
    item = start(client)
    before = client.get(f"/reviews/{item['id']}").json()
    failed = upload(client, item, name, content)
    assert failed.status_code == 422
    assert client.get(f"/reviews/{item['id']}").json() == before
    assert upload(client, item, "fixed.csv", "款号,数量\n00123,36\n".encode()).status_code == 200


def test_html_source_role_mapping_and_restore_pages_are_usable(client):
    item = start(client, draft=True)
    uploaded = upload(
        client,
        item,
        "opaque.xlsx",
        book([["款号", "颜色", "尺码", "数量"], ["00123", "藏蓝", "M", 36]], extra_sheet=True),
    ).json()
    item, sid = uploaded["review"], uploaded["source_id"]
    page = client.get(f"/ui/reviews/{item['id']}/sources/{sid}?role=orders")
    assert page.status_code == 200 and "列映射" in page.text and "00123" in page.text
    assert "说明" in page.text and "恢复缺失的相同原件" in page.text
    error = client.post(
        f"/ui/reviews/{item['id']}/sources/{sid}/confirm",
        data={
            "revision": item["revision"],
            "role": "orders",
            "table_id": "T1",
            "column_style": "invalid",
            "fields_checked": "yes",
            "context_complete": "yes",
            "note": "保留合成说明",
        },
    )
    assert error.status_code == 422 and "保留合成说明" in error.text
    assert client.get(f"/ui/reviews/{item['id']}").status_code == 200
