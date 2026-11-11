"""Numeric OCR review boundary; synthetic inputs are not model or business acceptance."""

from copy import deepcopy

import pytest
from app.config import Settings
from app.db.models import ReviewWorkspace
from app.errors import AppError
from app.main import create_app
from app.review import assets, reader_worker
from app.review.documents import Document
from fastapi.testclient import TestClient
from tests.test_review_sources import adopt, correct_and_confirm, start, upload


def word(text, confidence=90, box=(10, 10, 12, 20)):
    return {
        "text": text,
        "conf": str(confidence),
        "left": str(box[0]),
        "top": str(box[1]),
        "width": str(box[2]),
        "height": str(box[3]),
    }


def image_doc(text, *, numeric_check="available", low_count=0):
    return Document(
        kind="image",
        spans=[{"text": text, "locator": "合成预览", "confidence": 95, "box": [0, 0, 50, 20]}],
        ocr_size_crosscheck="checked",
        ocr_cjk_confidence="clear",
        ocr_numeric_check=numeric_check,
        ocr_numeric_low_count=low_count,
        ocr_numeric_low_regions=[[10, 10, 12, 20]] if low_count else [],
    )


def test_numeric_word_detection_is_bounded_and_handles_fullwidth_digits():
    result = reader_worker._numeric_confidence_review(
        [word("订单", 0), word("１２３", 79), word("2026-12-18", 12), word("42", 80)],
        100,
        100,
    )
    assert result == ("available", 2, [[10, 10, 12, 20], [10, 10, 12, 20]])
    assert reader_worker._numeric_confidence_review([word("无数字", 0)], 100, 100) == (
        "available",
        0,
        [],
    )
    assert reader_worker._numeric_confidence_review([word("31", "nan")], 100, 100)[0] == (
        "unavailable"
    )
    assert (
        reader_worker._numeric_confidence_review([word("31", 2, (99, 0, 5, 5))], 100, 100)[0]
        == "unavailable"
    )
    assert reader_worker._numeric_confidence_review([word("1")] * 5001, 100, 100)[0] == (
        "unavailable"
    )


@pytest.mark.parametrize("text", ["合计 31,000", "订购数量 135 件", "交付日期 2026-12-18"])
def test_low_numeric_cannot_be_adopted_by_attestation_alone(text):
    doc = image_doc(text, low_count=1)
    with pytest.raises(AppError) as failure:
        assets._require_reviewed_ocr(doc, numeric_fields_checked=True)
    assert failure.value.code == "OCR_NUMERIC_REVIEW_REQUIRED"


@pytest.mark.parametrize("text", ["总额 31000", "请按原单处理"])
def test_all_raw_images_need_explicit_original_review_even_without_detected_digits(text):
    doc = image_doc(text)
    with pytest.raises(AppError) as failure:
        assets._require_reviewed_ocr(doc)
    assert failure.value.code == "OCR_NUMERIC_REVIEW_REQUIRED"
    assets._require_reviewed_ocr(doc, numeric_fields_checked=True)


@pytest.mark.parametrize("state", [None, "unavailable"])
def test_missing_or_unavailable_numeric_metadata_cannot_be_checked_away(state):
    doc = image_doc("订单 12", numeric_check=state)
    with pytest.raises(AppError) as failure:
        assets._require_reviewed_ocr(doc, numeric_fields_checked=True)
    assert failure.value.code == "OCR_NUMERIC_REVIEW_REQUIRED"


def test_model_input_rechecks_attestation_and_preserves_raw_on_transcription():
    doc = image_doc("数量 42 件")
    asset = {
        "id": "synthetic",
        "role": "instruction",
        "confirmed": True,
        "document": doc.model_dump(mode="json"),
        "note": "脚本测试，非真人",
    }
    data = {"input": {"instruction": "合成输入"}, "assets": [asset]}
    with pytest.raises(AppError) as failure:
        assets.model_lines(data, verify=False)
    assert failure.value.code == "OCR_NUMERIC_REVIEW_REQUIRED"
    asset["numeric_fields_checked"] = True
    assert assets.model_lines(data, verify=False)[-1]["text"] == "数量 42 件"
    asset["numeric_fields_checked"] = False
    asset["corrected_text"] = "数量 43 件"
    lines = assets.model_lines(data, verify=False)
    assert lines[-1]["text"] == "数量 43 件" and lines[-1]["human"] is True
    assert asset["document"]["spans"][0]["text"] == "数量 42 件"


def test_legacy_confirmation_requires_review_without_erasing_history(
    monkeypatch, engine, database_url, session_factory
):
    # This test injects a synthetic reader result only to exercise migration.
    # Real Tesseract and Chromium checks are separate.
    doc = image_doc("数量 36 件")
    monkeypatch.setattr(assets, "read_document", lambda *args, **kwargs: (doc, b"preview"))
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        item = start(client)
        source = upload(client, item, "synthetic.png", b"synthetic-original").json()
        sid = source["source_id"]
        adopted = adopt(client, source["review"], sid, numeric_fields_checked=True)
        assert adopted.status_code == 200
        item = correct_and_confirm(client, adopted.json())
        rid = item["id"]
        with session_factory() as session, session.begin():
            row = session.get(ReviewWorkspace, rid)
            data = deepcopy(row.data)
            data["assets"][0].pop("ocr_numeric", None)
            for key in assets.NUMERIC_FIELDS:
                data["assets"][0]["document"].pop(key, None)
            data["assets"][0].pop("numeric_fields_checked")
            previous_proposal = deepcopy(data["proposal"])
            previous_confirmation = deepcopy(data["check"])
            row.data = data
        item = client.get(f"/reviews/{rid}").json()
        assert item["status"] == "CONFIRMED" and item["recheck_reason"] == "source_review"
        assert "图片文字、数值或日期" in client.get(f"/ui/reviews/{rid}").text
        blocked = client.get(f"/reviews/{rid}/export?fmt=csv")
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "REVIEW_SOURCE_REVIEW_REQUIRED"
        assert client.get(f"/reviews/{rid}/sources/{sid}/original").content == b"synthetic-original"
        corrected = client.post(
            f"/reviews/{rid}/sources/{sid}/correct",
            json={
                "revision": item["revision"],
                "text": "数量 36 件",
                "reason": "脚本对照合成样本转录，非真人审核",
            },
        )
        assert corrected.status_code == 200
        reviewed = adopt(client, corrected.json(), sid)
        assert reviewed.status_code == 200
        completed = correct_and_confirm(client, reviewed.json())
        archival = next(
            entry["previous_confirmation"]
            for entry in completed["data"]["history"]
            if entry["kind"] == "confirmation_superseded"
        )
        assert archival["proposal"] == previous_proposal
        assert archival["check"] == previous_confirmation
        assert client.get(f"/reviews/{rid}/export?fmt=csv").status_code == 200


def test_low_numeric_ui_and_api_require_transcription(monkeypatch, engine, database_url):
    doc = image_doc("合计 31 -OO0O0", low_count=1)
    monkeypatch.setattr(assets, "read_document", lambda *args, **kwargs: (doc, b"preview"))
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        source = upload(client, start(client), "synthetic.png", b"synthetic-original").json()
        item, sid = source["review"], source["source_id"]
        page = client.get(f"/ui/reviews/{item['id']}/sources/{sid}?role=instruction")
        assert "含数字的低分片段" in page.text
        assert "请对照完整原图转录后再确认" in page.text
        assert "disabled>确认这个角色与字段" in page.text
        refused = adopt(client, item, sid, numeric_fields_checked=True)
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "OCR_NUMERIC_REVIEW_REQUIRED"
