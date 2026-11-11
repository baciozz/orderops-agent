"""CJK uncertainty and optional OCR data profiles; synthetic data only."""

import hashlib
import os
import shutil
from io import BytesIO
from pathlib import Path

import pytest
from app.config import Settings
from app.db.models import ReviewWorkspace
from app.errors import AppError
from app.main import create_app
from app.review import assets, reader_worker, service
from app.review.contracts import CreateReview
from app.review.documents import Document, read_document
from app.review.sample import SAMPLE_SNAPSHOT
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

FONT = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
FAST_CHI = "a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730"
BEST_CHI = "4fef2d1306c8e87616d4d3e4c6c67faf5d44be3342290cf8f2f0f6e3aa7e735b"


def word(text, confidence=90, box=(10, 10, 12, 20)):
    return {
        "text": text,
        "conf": str(confidence),
        "left": str(box[0]),
        "top": str(box[1]),
        "width": str(box[2]),
        "height": str(box[3]),
    }


def synthetic_jpeg():
    if not FONT.exists():
        pytest.skip("synthetic Chinese image needs a local font")
    image = Image.new("RGB", (1800, 370), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(FONT), 44)
    for y, text in (
        (35, "模拟资料：仅供系统测试"),
        (120, "订单 SYN-FMT-JPG，款号00123 藏蓝 M"),
        (205, "总订购量改成51件；已发9件保留。"),
    ):
        draw.text((35, y), text, font=font, fill="black")
    output = BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


@pytest.fixture()
def tessdata(request):
    directory = request.config.getoption("--ocr-data-dir")
    if not directory or not shutil.which("tesseract"):
        pytest.skip("actual Chinese OCR requires tesseract and --ocr-data-dir")
    return Path(directory)


def test_low_cjk_scores_flag_without_guessing_text():
    state, count, boxes = reader_worker._cjk_confidence_review(
        [word("00123", 0), word("藏", 0), word("赣", 19), word("M", 0)], 100, 100
    )
    assert (state, count, boxes) == ("review", 2, [[10, 10, 12, 20], [10, 10, 12, 20]])
    assert reader_worker._cjk_confidence_review([word("蓝", 50)], 100, 100) == ("clear", 0, [])
    # Absence of a flag never claims the text is correct.
    assert reader_worker._cjk_confidence_review([word("赣", 95)], 100, 100) == ("clear", 0, [])


def test_invalid_or_unbounded_cjk_metadata_fails_closed():
    assert reader_worker._cjk_confidence_review([word("蓝", "nan")], 100, 100)[0] == "unavailable"
    assert (
        reader_worker._cjk_confidence_review([word("蓝", 2, (99, 0, 5, 5))], 100, 100)[0]
        == "unavailable"
    )
    assert reader_worker._cjk_confidence_review([word("蓝")] * 5001, 100, 100)[0] == "unavailable"
    doc = Document(kind="image", spans=[], ocr_cjk_confidence="unavailable")
    with pytest.raises(AppError) as failure:
        assets._require_reviewed_ocr(doc, cjk_low_confidence_checked=True)
    assert failure.value.code == "OCR_TEXT_REVIEW_REQUIRED"


def test_model_lines_need_explicit_review_or_transcription():
    doc = Document(
        kind="image",
        spans=[{"text": "00123 藏蓝 M", "locator": "预览"}],
        ocr_cjk_confidence="review",
        ocr_cjk_low_count=1,
        ocr_cjk_low_regions=[[10, 10, 12, 20]],
        ocr_numeric_check="available",
    )
    asset = {
        "id": "synthetic",
        "role": "instruction",
        "confirmed": True,
        "document": doc.model_dump(mode="json"),
        "note": "已填写说明",
    }
    data = {"input": {"instruction": "合成输入"}, "assets": [asset]}
    with pytest.raises(AppError) as failure:
        assets.model_lines(data, verify=False)
    assert failure.value.code == "OCR_TEXT_REVIEW_REQUIRED"
    asset["cjk_low_confidence_checked"] = True
    asset["numeric_fields_checked"] = True
    assert assets.model_lines(data, verify=False)[-1]["text"] == "00123 藏蓝 M"
    asset["cjk_low_confidence_checked"] = False
    asset["corrected_text"] = "00123 藏蓝 M"
    assert assets.model_lines(data, verify=False)[-1]["human"] is True
    assert asset["document"]["spans"][0]["text"] == "00123 藏蓝 M"


def test_legacy_image_is_not_claimed_numeric_checked():
    doc = Document.model_validate({"kind": "image", "spans": [{"text": "旧OCR", "locator": "图"}]})
    assert doc.ocr_cjk_confidence is None
    with pytest.raises(AppError) as failure:
        assets._require_reviewed_ocr(doc)
    assert failure.value.code == "OCR_NUMERIC_REVIEW_REQUIRED"


def test_best_profile_rejects_existing_mismatch_before_network(tmp_path, monkeypatch):
    from scripts import prepare_ocr

    (tmp_path / "chi_sim.traineddata").write_bytes(b"different")
    before = set(tmp_path.iterdir())

    def forbidden(*args, **kwargs):
        pytest.fail("network must not be reached on a profile mismatch")

    monkeypatch.setattr(prepare_ocr.httpx, "Client", forbidden)
    with pytest.raises(ValueError, match="不符"):
        prepare_ocr.prepare(tmp_path.resolve(), "best")
    assert set(tmp_path.iterdir()) == before
    assert (tmp_path / "chi_sim.traineddata").read_bytes() == b"different"


def test_actual_fast_jpeg_warns_and_preserves_original_on_transcription(
    tessdata, session_factory, database_url, tmp_path
):
    if hashlib.sha256((tessdata / "chi_sim.traineddata").read_bytes()).hexdigest() != FAST_CHI:
        pytest.skip("this regression uses the pinned fast language data")
    content = synthetic_jpeg()
    doc, _ = read_document("customer.jpg", content, ocr_data_dir=tessdata)
    text = "".join("".join(span.text.split()) for span in doc.spans)
    if "藏赣" not in text:
        pytest.skip("this engine/font did not reproduce the synthetic fast OCR error")
    assert doc.ocr_cjk_confidence == "review" and doc.ocr_cjk_low_count >= 1
    assert doc.ocr_language_sha256["chi_sim"] == FAST_CHI
    rid = service.create(
        session_factory,
        CreateReview.model_validate(
            {
                "request_key": "cjk-color-jpeg",
                "selected_order_id": "SYN-2401",
                "instruction": "请核对合成原件。",
                "snapshot": SAMPLE_SNAPSHOT,
            }
        ),
    )
    sid, _ = assets.upload(
        session_factory,
        rid,
        revision=1,
        key="original",
        filename="customer.jpg",
        data=content,
        database_url=database_url,
        storage_dir=tmp_path / "originals",
        ocr_data_dir=tessdata,
    )
    item = service.view(session_factory, rid)
    with pytest.raises(AppError) as failure:
        assets.confirm(
            session_factory,
            rid,
            sid,
            assets.AssetConfirmation(
                revision=item["revision"],
                role="instruction",
                fields_checked=True,
                context_complete=True,
                note="待核对",
            ),
        )
    assert failure.value.code == "OCR_TEXT_REVIEW_REQUIRED"
    settings = Settings(_env_file=None, database_url=database_url, ocr_data_dir=tessdata)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        page = client.get(f"/ui/reviews/{rid}/sources/{sid}")
        assert "低分中文预览区域" in page.text and "cjk_low_confidence_checked" in page.text
    assets.correct(
        session_factory,
        rid,
        sid,
        assets.AssetCorrection(
            revision=item["revision"],
            text=(
                "模拟资料：仅供系统测试\n订单 SYN-FMT-JPG，款号00123 藏蓝 M\n"
                "总订购量改成51件；已发9件保留。"
            ),
            reason="脚本对照合成底稿转录；不是独立真人",
        ),
    )
    item = service.view(session_factory, rid)
    assets.confirm(
        session_factory,
        rid,
        sid,
        assets.AssetConfirmation(
            revision=item["revision"],
            role="instruction",
            fields_checked=True,
            context_complete=True,
            note="合成测试转录",
        ),
    )
    with session_factory() as session:
        stored = session.get(ReviewWorkspace, rid).data["assets"][0]
        assert assets.original(stored) == content
        assert "藏赣" in "".join(
            "".join(span["text"].split()) for span in stored["document"]["spans"]
        )
        assert stored["corrected_text"].find("藏蓝") >= 0


def test_actual_best_jpeg_and_explicit_visual_acknowledgement(
    session_factory, database_url, tmp_path
):
    directory = os.environ.get("TEST_BEST_OCR_DATA_DIR")
    if not directory or not shutil.which("tesseract"):
        pytest.skip("optional actual best OCR needs TEST_BEST_OCR_DATA_DIR")
    tessdata = Path(directory)
    if hashlib.sha256((tessdata / "chi_sim.traineddata").read_bytes()).hexdigest() != BEST_CHI:
        pytest.skip("this regression uses pinned best language data")
    content = synthetic_jpeg()
    doc, _ = read_document("customer.jpg", content, ocr_data_dir=tessdata)
    text = "".join("".join(span.text.split()) for span in doc.spans)
    assert "藏蓝" in text and doc.ocr_language_sha256["chi_sim"] == BEST_CHI
    assert doc.ocr_cjk_confidence == "review" and not doc.ocr_size_conflicts
    rid = service.create(
        session_factory,
        CreateReview.model_validate(
            {
                "request_key": "best-jpeg",
                "selected_order_id": "SYN-2401",
                "instruction": "请核对合成原件。",
                "snapshot": SAMPLE_SNAPSHOT,
            }
        ),
    )
    sid, _ = assets.upload(
        session_factory,
        rid,
        revision=1,
        key="original",
        filename="customer.jpg",
        data=content,
        database_url=database_url,
        storage_dir=tmp_path / "originals",
        ocr_data_dir=tessdata,
    )
    item = service.view(session_factory, rid)
    with pytest.raises(AppError) as failure:
        assets.confirm(
            session_factory,
            rid,
            sid,
            assets.AssetConfirmation(
                revision=item["revision"],
                role="instruction",
                fields_checked=True,
                context_complete=True,
                note="待核对",
            ),
        )
    assert failure.value.code == "OCR_TEXT_REVIEW_REQUIRED"
    assets.confirm(
        session_factory,
        rid,
        sid,
        assets.AssetConfirmation(
            revision=item["revision"],
            role="instruction",
            fields_checked=True,
            context_complete=True,
            cjk_low_confidence_checked=True,
            numeric_fields_checked=True,
            note="脚本模拟对照生成底稿确认；不是独立真人",
        ),
    )
    with session_factory() as session:
        data = session.get(ReviewWorkspace, rid).data
        assert data["assets"][0]["cjk_low_confidence_checked"] is True
        assert data["assets"][0]["numeric_fields_checked"] is True
        assert data["history"][-1]["cjk_low_confidence_checked"] is True
        assert assets.original(data["assets"][0]) == content
        assert "藏" in "".join(line["text"] for line in assets.model_lines(data))
