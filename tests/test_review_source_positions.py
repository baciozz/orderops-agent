"""Synthetic provenance and no-op guard regressions; no live model calls."""

import csv
import hashlib
import json
from io import BytesIO, StringIO
from pathlib import Path

import openpyxl
import pytest
from app.config import Settings
from app.main import create_app
from app.review import service
from app.review.assets import model_lines
from app.review.contracts import Proposal, Snapshot
from app.review.documents import Document, Span, read_document
from app.review.rules import check_proposal
from benchmarks.compare_readers import validate_public_request
from fastapi.testclient import TestClient
from tests.test_review_documents import pdf

FIXTURES = Path(__file__).parent / "fixtures" / "review_development" / "pdf-wrapped"


def asset(tmp_path, content, doc, *, source_id="RVS-synthetic", name="RVO-synthetic"):
    path = tmp_path / name
    path.write_bytes(content)
    return {
        "id": source_id,
        "confirmed": True,
        "role": "instruction",
        "document": doc.model_dump(mode="json"),
        "note": "已对照合成原件",
        "path": str(path),
        "storage_id": path.name,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def data_for(snapshot, *assets):
    return {
        "input": {
            "snapshot": snapshot,
            "selected_order_id": snapshot["orders"][0]["order_id"],
            "instruction": "客户要求见已核对的原件。",
        },
        "assets": list(assets),
    }


def mock_settings():
    return Settings(
        _env_file=None,
        live_enabled=True,
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:9999/v1",
        llm_model="offline-mock-NOT-a-real-model",
        price_in_per_mtok="0.10",
        price_out_per_mtok="0.20",
    )


def test_real_pdf_positions_preserve_original_lines_and_adapter_contract(tmp_path):
    source = (FIXTURES / "request.pdf").read_bytes()
    document, _ = read_document("request.pdf", source)
    snapshot = json.loads((FIXTURES / "baseline.json").read_text())
    data = data_for(snapshot, asset(tmp_path, source, document))
    lines = model_lines(data)
    assert [x["text"] for x in lines[1:]] == [s.text for s in document.spans]
    assert [(x["source_page"], x["source_extracted_line"]) for x in lines[1:]] == [
        (1, 1),
        (1, 2),
        (1, 3),
    ]
    assert lines[2]["text"].endswith("白 L") and lines[3]["text"] == "和其他行不变。"
    assert "source_page" not in lines[0]
    request = service._model_request(data, mock_settings(), "synthetic-local-review")
    model_input = json.loads(request.user_prompt)
    assert model_input["INPUT"] == lines
    assert validate_public_request(request)


def test_pdf_positions_do_not_join_gaps_pages_sources_or_human_transcription(tmp_path):
    source1 = b"first synthetic source"
    source2 = b"second synthetic source"
    doc = Document(
        kind="pdf",
        pages=2,
        spans=[
            Span(text="页一第一行", locator="PDF第1页，提取行1", page=1),
            Span(text="页一第三行", locator="PDF第1页，提取行3", page=1),
            Span(text="页二第一行", locator="PDF第2页，提取行1", page=2),
            Span(text="页码超出文档", locator="PDF第3页，提取行1", page=3),
        ],
    )
    doc2 = Document(
        kind="pdf",
        pages=1,
        spans=[
            Span(text="另一份的第一行", locator="PDF第1页，提取行1", page=1),
            Span(text="不一致的定位", locator="PDF第2页，提取行2", page=1),
        ],
    )
    snapshot = json.loads((FIXTURES / "baseline.json").read_text())
    a = asset(tmp_path, source1, doc, source_id="RVS-1", name="RVO-1")
    b = asset(tmp_path, source2, doc2, source_id="RVS-2", name="RVO-2")
    lines = model_lines(data_for(snapshot, a, b))
    assert [r["text"] for r in lines[1:]] == [
        "页一第一行",
        "页一第三行",
        "页二第一行",
        "页码超出文档",
        "另一份的第一行",
        "不一致的定位",
    ]
    assert [(r.get("source_page"), r.get("source_extracted_line")) for r in lines[1:]] == [
        (1, 1),
        (1, 3),
        (2, 1),
        (None, None),
        (1, 1),
        (None, None),
    ]
    assert [r["source_id"] for r in lines[1:]] == [
        "RVS-1",
        "RVS-1",
        "RVS-1",
        "RVS-1",
        "RVS-2",
        "RVS-2",
    ]
    a["corrected_text"] = "人工转录，不继承PDF原行号"
    human_line = model_lines(data_for(snapshot, a))[1]
    assert human_line["human"] is True
    assert "source_page" not in human_line and "source_extracted_line" not in human_line


def test_real_multipage_pdf_keeps_separate_pages_and_exact_extract_locators(tmp_path):
    content = pdf([["合成第一页甲", "合成第一页乙"], ["合成第二页丙"]])
    document, _ = read_document("two-pages.pdf", content)
    snapshot = json.loads((FIXTURES / "baseline.json").read_text())
    lines = model_lines(data_for(snapshot, asset(tmp_path, content, document)))
    assert [line["text"] for line in lines[1:]] == [span.text for span in document.spans]
    assert [(x["source_page"], x["source_extracted_line"]) for x in lines[1:]] == [
        (1, 1),
        (1, 2),
        (2, 1),
    ]
    assert all(
        line["locator"] == f"PDF第{line['source_page']}页，提取行{line['source_extracted_line']}"
        for line in lines[1:]
    )


@pytest.mark.parametrize(
    "change",
    [
        {"source_page": None},
        {"source_page": True},
        {"source_extracted_line": 0},
        {"source_extracted_line": 999},
        {"source_id": "INPUT"},
    ],
)
def test_reader_comparison_rejects_inconsistent_position_metadata(tmp_path, change):
    source = (FIXTURES / "request.pdf").read_bytes()
    document, _ = read_document("request.pdf", source)
    snapshot = json.loads((FIXTURES / "baseline.json").read_text())
    data = data_for(snapshot, asset(tmp_path, source, document))
    request = service._model_request(data, mock_settings(), "synthetic-local-review")
    payload = json.loads(request.user_prompt)
    payload["INPUT"][1].update(change)
    request = request.model_copy(update={"user_prompt": json.dumps(payload, ensure_ascii=False)})
    with pytest.raises(ValueError, match="positions"):
        validate_public_request(request)


def proposal(snapshot, *, include_change=True, include_noop=True):
    changes = []
    if include_change:
        changes.append(
            {
                "mode": "set_total",
                "sku_id": "00104-藏蓝-M",
                "to_sku": None,
                "quantity": 39,
                "unit": "件",
                "before_qty": 84,
                "after_qty": 39,
                "citations": [{"line": 2, "quote": "00104 藏蓝 M 总量改成39件"}],
            }
        )
    if include_noop:
        changes.append(
            {
                "mode": "set_total",
                "sku_id": "00104-白-L",
                "to_sku": None,
                "quantity": 45,
                "unit": "件",
                "before_qty": 45,
                "after_qty": 45,
                "citations": [{"line": 2, "quote": "白 L"}, {"line": 3, "quote": "和其他行不变。"}],
            }
        )
    return Proposal.model_validate(
        {
            "order_id": snapshot.orders[0].order_id,
            "disposition": "propose",
            "changes": changes,
            "questions": [],
            "summary": "合成测试",
        }
    )


def fixture_check(p, *, human=False):
    snapshot = Snapshot.model_validate_json((FIXTURES / "baseline.json").read_bytes())
    doc, _ = read_document("request.pdf", (FIXTURES / "request.pdf").read_bytes())
    return check_proposal(
        snapshot,
        snapshot.orders[0].order_id,
        "",
        p,
        human=human,
        source_texts=[s.text for s in doc.spans],
    )


def test_mixed_noop_remains_auditable_without_discarding_proposal():
    snapshot = Snapshot.model_validate_json((FIXTURES / "baseline.json").read_bytes())
    p = proposal(snapshot)
    original = p.model_dump(mode="json")
    result = fixture_check(p)
    assert result["status"] == "ready"
    assert len(result["rows"]) == 1 and result["rows"][0]["new_qty"] == 39
    assert any("00104-白-L" in note and "未产生数量变化" in note for note in result["warnings"])
    assert p.model_dump(mode="json") == original and len(p.changes) == 2


def test_noop_cannot_hide_invalid_citation_or_inconsistent_numbers():
    snapshot = Snapshot.model_validate_json((FIXTURES / "baseline.json").read_bytes())
    p = proposal(snapshot)
    p.changes[1].citations[0].quote = "伪造来源"
    result = fixture_check(p)
    assert result["status"] == "blocked" and any("来源无法对照" in x for x in result["issues"])
    p = proposal(snapshot)
    p.changes[1].quantity = 46
    result = fixture_check(p)
    assert result["status"] == "blocked" and any("数量语义不一致" in x for x in result["issues"])


def test_only_noop_and_same_sku_replacement_cannot_appear_ready():
    snapshot = Snapshot.model_validate_json((FIXTURES / "baseline.json").read_bytes())
    only_noop = fixture_check(proposal(snapshot, include_change=False))
    assert only_noop["status"] == "blocked"
    assert any("未产生数量变化" in x for x in only_noop["issues"])
    p = proposal(snapshot, include_noop=False)
    p.changes[0].mode = "replace_remaining"
    p.changes[0].to_sku = p.changes[0].sku_id
    p.changes[0].quantity = 5
    p.changes[0].after_qty = 79
    result = fixture_check(p)
    assert result["status"] == "blocked" and any("原SKU相同" in x for x in result["issues"])


def test_noop_warning_is_preserved_in_screen_and_both_exports(engine, database_url):
    snapshot = json.loads((FIXTURES / "baseline.json").read_text())
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        created = client.post(
            "/reviews",
            json={
                "request_key": "no-op-audit",
                "selected_order_id": snapshot["orders"][0]["order_id"],
                "instruction": "核对原件后人工输入",
                "snapshot": snapshot,
            },
        ).json()
        original = proposal(Snapshot.model_validate(snapshot)).model_dump(mode="json")
        corrected = client.post(
            f"/reviews/{created['id']}/correct",
            json={
                "revision": created["revision"],
                "proposal": original,
                "reason": "合成资料人工核对",
            },
        ).json()
        assert corrected["data"]["proposal"]["changes"] == original["changes"]
        assert (
            corrected["data"]["check"]["status"] == "ready"
            and corrected["data"]["check"]["warnings"]
        )
        html = client.get(f"/ui/reviews/{created['id']}").text
        assert "未产生数量变化" in html and "00104-白-L" in html
        confirmed = client.post(
            f"/reviews/{created['id']}/confirm", json={"revision": corrected["revision"]}
        ).json()
        assert confirmed["status"] == "CONFIRMED"
        csv_bytes = client.get(f"/reviews/{created['id']}/export?fmt=csv").content
        xlsx_bytes = client.get(f"/reviews/{created['id']}/export?fmt=xlsx").content
        csv_row = list(csv.DictReader(StringIO(csv_bytes.decode("utf-8-sig"))))[0]
        book = openpyxl.load_workbook(BytesIO(xlsx_bytes), read_only=True, data_only=True)
        values = list(book.active.values)
        book.close()
        xlsx_row = dict(zip(values[0], values[1], strict=True))
        assert "00104-白-L" in csv_row["todo"] and "未产生数量变化" in csv_row["todo"]
        assert xlsx_row["todo"] == csv_row["todo"]


def test_public_size_synthetic_zero_delta_cannot_confirm_or_export(engine, database_url):
    """W28 is a public size label; the order, values and request are synthetic."""
    snapshot = {
        "synthetic": True,
        "captured_at": "2026-11-10T00:00:00Z",
        "catalog": [
            {
                "sku_id": "00997-藏蓝-W28",
                "style": "00997",
                "color": "藏蓝",
                "size": "W28",
                "fulfillment_mode": "STOCK",
            }
        ],
        "orders": [
            {
                "order_id": "SYN-PUB-024",
                "revision": 1,
                "lines": [
                    {
                        "sku_id": "00997-藏蓝-W28",
                        "ordered_qty": 40,
                        "shipped_qty": 7,
                        "cut_qty": None,
                    }
                ],
            }
        ],
        "inventory": [{"sku_id": "00997-藏蓝-W28", "available_qty": 50}],
        "documents": [],
    }
    candidate = {
        "order_id": "SYN-PUB-024",
        "disposition": "propose",
        "questions": [],
        "summary": "人工构造合成候选",
        "changes": [
            {
                "mode": "set_total",
                "sku_id": "00997-藏蓝-W28",
                "to_sku": None,
                "quantity": 40,
                "unit": "件",
                "before_qty": 40,
                "after_qty": 40,
                "citations": [],
            }
        ],
    }
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        created = client.post(
            "/reviews",
            json={
                "request_key": "synthetic-public-size-noop",
                "selected_order_id": "SYN-PUB-024",
                "instruction": "合成 W28 总量40，已发7。",
                "snapshot": snapshot,
            },
        ).json()
        result = client.post(
            f"/reviews/{created['id']}/correct",
            json={"revision": created["revision"], "proposal": candidate, "reason": "核对合成资料"},
        ).json()
        assert result["data"]["proposal"] == candidate
        assert result["data"]["check"]["status"] == "blocked"
        assert any("未产生数量变化" in x for x in result["data"]["check"]["issues"])
        response = client.post(
            f"/reviews/{created['id']}/confirm", json={"revision": result["revision"]}
        )
        assert response.status_code == 409
        assert client.get(f"/reviews/{created['id']}/export?fmt=csv").status_code == 409
