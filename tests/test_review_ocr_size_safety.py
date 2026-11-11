"""Synthetic OCR and source-boundary regressions; no models or heldout data."""

import copy
import shutil
import subprocess
import time
from collections import defaultdict
from io import BytesIO
from pathlib import Path

import pytest
from app.config import Settings
from app.db.models import ReviewWorkspace
from app.db.session import begin_immediate
from app.errors import AppError
from app.main import create_app
from app.review import assets, reader_worker, service
from app.review.contracts import CreateReview
from app.review.documents import Document, read_document
from app.review.sample import SAMPLE_SNAPSHOT
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

FONT = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")


def word(text, confidence=90, box=(10, 10, 12, 20)):
    left, top, width, height = box
    return {
        "text": text,
        "conf": str(confidence),
        "left": str(left),
        "top": str(top),
        "width": str(width),
        "height": str(height),
    }


def image(text, size=40):
    if not FONT.exists():
        pytest.skip("synthetic OCR image needs a local CJK font")
    picture = Image.new("RGB", (1500, 140), "white")
    ImageDraw.Draw(picture).text(
        (30, 30), text, font=ImageFont.truetype(str(FONT), size), fill="black"
    )
    stream = BytesIO()
    picture.save(stream, format="PNG")
    return stream.getvalue()


def multiline(lines, size=40):
    if not FONT.exists():
        pytest.skip("synthetic OCR image needs a local CJK font")
    picture = Image.new("RGB", (1500, 260), "white")
    draw = ImageDraw.Draw(picture)
    font = ImageFont.truetype(str(FONT), size)
    for index, line in enumerate(lines):
        draw.text((30, 20 + index * 75), line, font=font, fill="black")
    stream = BytesIO()
    picture.save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture()
def tessdata(request):
    directory = request.config.getoption("--ocr-data-dir")
    if not directory:
        pytest.skip("actual Chinese OCR requires --ocr-data-dir")
    return Path(directory)


def test_overlapping_different_size_candidate_flags_without_rewriting():
    primary = [word("上", 52, (10, 10, 20, 35))]
    alternate = [word("L", 91, (12, 10, 18, 35))]
    original = copy.deepcopy(primary)
    conflicts = reader_worker._size_disagreements(primary, alternate, 100, 100)
    assert [item.model_dump() for item in conflicts] == [
        {"box": [12, 10, 18, 35], "primary": "上", "alternate": "L", "reason": "disagreement"}
    ]
    assert primary == original


def test_same_low_confidence_ambiguous_glyph_is_not_accepted_as_agreement():
    primary = [word("上", 21)]
    alternate = [word("上", 21)]
    conflicts = reader_worker._size_disagreements(primary, alternate, 100, 100)
    assert len(conflicts) == 1
    assert conflicts[0].reason == "uncertain_glyph" and conflicts[0].alternate is None
    assert conflicts[0].primary == "上"


def test_matched_known_size_does_not_trigger_a_spurious_disagreement():
    assert reader_worker._size_disagreements([word("XL")], [word("XL")], 100, 100) == []


def test_labeled_and_punctuated_size_tokens_are_compared_without_replacement():
    for original, alternative in (("尺码上", "尺码L"), ("上,", "L,")):
        conflicts = reader_worker._size_disagreements(
            [word(original, 49)], [word(alternative, 95)], 100, 100
        )
        assert len(conflicts) == 1
        assert conflicts[0].primary == original and conflicts[0].alternate == alternative
        assert conflicts[0].reason == "disagreement"
    assert not reader_worker._size_disagreements([word("尺码L")], [word("尺码L")], 100, 100)
    assert not reader_worker._size_disagreements([word("L,")], [word("L,")], 100, 100)


def test_low_scoring_mixed_size_quantity_or_order_code_requires_review():
    for text in ("LV/37", "SYN-123"):
        conflicts = reader_worker._size_disagreements([word(text, 54)], [word(text, 54)], 100, 100)
        assert len(conflicts) == 1 and conflicts[0].reason == "uncertain_mixed"
        assert conflicts[0].primary == text and conflicts[0].alternate is None
    # It is a review threshold, not a claim that high confidence is correct.
    assert not reader_worker._size_disagreements(
        [word("SYN-123", 90)], [word("SYN-123", 90)], 100, 100
    )


def test_misaligned_candidate_and_invalid_coordinates_fail_conservatively():
    omitted = reader_worker._size_disagreements(
        [word("蓝", box=(10, 10, 10, 20))], [word("L", box=(50, 10, 10, 20))], 100, 100
    )
    assert len(omitted) == 1 and omitted[0].primary == "" and omitted[0].alternate == "L"
    with pytest.raises(ValueError):
        reader_worker._size_disagreements([word("上")], [word("L", box=(95, 0, 20, 20))], 100, 100)
    with pytest.raises(ValueError):
        reader_worker._size_disagreements([word("上")], [word("L", confidence="nan")], 100, 100)


def test_legacy_document_still_loads_but_needs_numeric_review():
    old = {"kind": "image", "spans": [{"text": "旧原始OCR", "locator": "原图第1行"}]}
    doc = Document.model_validate(old)
    assert doc.ocr_size_crosscheck is None and doc.ocr_size_conflicts == []
    with pytest.raises(AppError) as failure:
        assets._require_reviewed_ocr(doc)
    assert failure.value.code == "OCR_NUMERIC_REVIEW_REQUIRED"


def test_flagged_ocr_cannot_enter_model_lines_without_transcription():
    doc = Document.model_validate(
        {
            "kind": "image",
            "spans": [{"text": "藏蓝 上 37件", "locator": "预览", "box": [1, 1, 50, 20]}],
            "ocr_size_crosscheck": "checked",
            "ocr_size_conflicts": [{"box": [10, 1, 10, 20], "primary": "上", "alternate": "L"}],
        }
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
    assert failure.value.code == "OCR_SIZE_REVIEW_REQUIRED"
    asset["corrected_text"] = "藏蓝 L 37件"
    lines = assets.model_lines(data, verify=False)
    assert lines[-1]["text"] == "藏蓝 L 37件" and lines[-1]["human"] is True
    assert asset["document"]["spans"][0]["text"] == "藏蓝 上 37件"


@pytest.mark.parametrize("size", ["XS", "S", "M", "L", "XL", "XXL", "XXXL"])
def test_actual_ocr_different_sizes_either_preserves_size_or_requires_review(tessdata, size):
    raw = image(f"合成订单 00123 蓝色 {size} 数量 37 件")
    doc, preview = read_document("synthetic.png", raw, ocr_data_dir=tessdata)
    assert preview and doc.ocr_size_crosscheck == "checked"
    tokens = " ".join(span.text for span in doc.spans).split()
    assert size in tokens or doc.ocr_size_conflicts
    if doc.ocr_size_conflicts:
        with pytest.raises(AppError) as failure:
            assets._require_reviewed_ocr(doc)
        assert failure.value.code == "OCR_SIZE_REVIEW_REQUIRED"
    assert any("不代表" in warning for warning in doc.warnings)


def test_actual_ocr_missing_secondary_language_requires_review(tessdata, tmp_path):
    only_chinese = tmp_path / "chi-only"
    only_chinese.mkdir()
    shutil.copyfile(tessdata / "chi_sim.traineddata", only_chinese / "chi_sim.traineddata")
    doc, _ = read_document(
        "synthetic.png", image("合成订单 00123 蓝色 M 数量 37 件"), ocr_data_dir=only_chinese
    )
    assert doc.ocr_size_crosscheck == "unavailable"
    with pytest.raises(AppError) as failure:
        assets._require_reviewed_ocr(doc)
    assert failure.value.code == "OCR_SIZE_REVIEW_REQUIRED"


def test_actual_multiline_crosscheck_compares_all_lines_not_only_the_last(tessdata):
    clean, _ = read_document(
        "synthetic.png",
        multiline(("模拟资料 仅供测试", "订单 SYN-A 00123 蓝色 M", "总量改成 37 件")),
        ocr_data_dir=tessdata,
    )
    if not any(" M" in span.text for span in clean.spans[:-1]):
        pytest.skip("this OCR build did not read the middle-line M for this synthetic image")
    assert not any(item.alternate == "M" and not item.primary for item in clean.ocr_size_conflicts)
    ambiguous, _ = read_document(
        "synthetic.png",
        multiline(("模拟资料 仅供测试", "SYN-TEST 00123 藏蓝 L 总量改成37件", "其他行不变"), 48),
        ocr_data_dir=tessdata,
    )
    if any(" 上 " in span.text for span in ambiguous.spans[:-1]):
        assert any(item.primary == "上" for item in ambiguous.ocr_size_conflicts)


def test_actual_ocr_merged_size_quantity_low_score_requires_review(tessdata):
    doc, _ = read_document("synthetic.png", image("尺码L/37件"), ocr_data_dir=tessdata)
    if any("LV/37" in span.text for span in doc.spans):
        assert any(
            item.primary == "LV/37" and item.reason == "uncertain_mixed"
            for item in doc.ocr_size_conflicts
        )
        with pytest.raises(AppError) as failure:
            assets._require_reviewed_ocr(doc)
        assert failure.value.code == "OCR_SIZE_REVIEW_REQUIRED"
    else:
        pytest.skip("this OCR build did not reproduce the merged synthetic token")


def test_actual_primary_ocr_is_preserved_when_secondary_times_out(tessdata, tmp_path, monkeypatch):
    real = reader_worker.subprocess.run

    def with_timeout(args, *a, **kw):
        if "chi_sim+eng" in args:
            raise subprocess.TimeoutExpired(args, 8)
        return real(args, *a, **kw)

    monkeypatch.setattr(reader_worker.subprocess, "run", with_timeout)
    doc = reader_worker.image_document(
        image("合成订单 00123 蓝色 M 数量 37 件"), tmp_path / "preview.jpg", 0, tessdata
    )
    assert doc.spans and doc.ocr_size_crosscheck == "unavailable"
    actual = real(
        [
            "/usr/bin/tesseract",
            str(tmp_path / "preview.jpg"),
            "stdout",
            "-l",
            "chi_sim",
            "--psm",
            "6",
            "--tessdata-dir",
            str(tessdata),
            "-c",
            "tessedit_create_tsv=1",
        ],
        capture_output=True,
        check=True,
        timeout=25,
    )
    grouped = defaultdict(list)
    for row in reader_worker._ocr_words(actual.stdout):
        grouped[(row["block_num"], row["par_num"], row["line_num"])].append(row["text"])
    assert [span.text for span in doc.spans] == [" ".join(line) for line in grouped.values()]
    with pytest.raises(AppError):
        assets._require_reviewed_ocr(doc)


def test_bounded_child_returns_primary_result_after_slow_secondary_ocr(
    tessdata, tmp_path, monkeypatch
):
    # The real primary OCR runs in read_document's subprocess; only the second
    # tesseract invocation is delayed to exercise the timeout/fail-closed path.
    from app.review import documents

    binary = tmp_path / "bin"
    binary.mkdir()
    wrapper = binary / "tesseract"
    wrapper.write_text(
        "#!/usr/bin/python3\nimport os, sys, time\n"
        "if 'chi_sim+eng' in sys.argv: time.sleep(10)\n"
        "os.execv('/usr/bin/tesseract', ['/usr/bin/tesseract', *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o700)
    real = documents.subprocess.run

    def inject_binary(*args, **kwargs):
        kwargs["env"]["PATH"] = f"{binary}:/usr/local/bin:/usr/bin:/bin"
        return real(*args, **kwargs)

    monkeypatch.setattr(documents.subprocess, "run", inject_binary)
    started = time.monotonic()
    doc, _ = read_document(
        "synthetic.png", image("合成订单 00123 蓝色 M 数量 37 件"), ocr_data_dir=tessdata
    )
    assert 8 <= time.monotonic() - started < 55
    assert doc.spans and doc.ocr_size_crosscheck == "unavailable"
    with pytest.raises(AppError) as failure:
        assets._require_reviewed_ocr(doc)
    assert failure.value.code == "OCR_SIZE_REVIEW_REQUIRED"


def test_actual_ocr_disagreement_requires_transcription_and_preserves_original(
    tessdata, session_factory, database_url, tmp_path
):
    content = image("SYN-TEST 00123 藏蓝 L 总量改成37件", 48)
    doc, _ = read_document("synthetic.png", content, ocr_data_dir=tessdata)
    if not doc.ocr_size_conflicts:
        pytest.skip("this OCR build did not reproduce the synthetic ambiguity")
    assert any("上" in span.text for span in doc.spans)
    original_document = doc.model_dump(mode="json")
    rid = service.create(
        session_factory,
        CreateReview.model_validate(
            {
                "request_key": "ocr-size-regression",
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
        filename="synthetic.png",
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
                note="已核对，但尚未转录",
            ),
        )
    assert failure.value.code == "OCR_SIZE_REVIEW_REQUIRED"
    assets.correct(
        session_factory,
        rid,
        sid,
        assets.AssetCorrection(
            revision=item["revision"],
            text="SYN-TEST 00123 藏蓝 L 总量改成37件",
            reason="测试脚本模拟对照合成图片后转录，非独立真人",
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
            note="测试脚本模拟转录核对",
        ),
    )
    with session_factory() as session:
        data = session.get(ReviewWorkspace, rid).data
        assert assets.public_data(data)["assets"][0]["document"] == original_document
        assert assets.original(data["assets"][0]) == content
        lines = assets.model_lines(data)
    assert lines[-1]["human"] is True and "藏蓝 L" in lines[-1]["text"]


@pytest.mark.parametrize("legacy_field", ["missing", "null"])
def test_legacy_image_reupload_reuses_old_reading_and_ui_explains_recheck(
    tessdata, session_factory, database_url, tmp_path, legacy_field
):
    content = image("合成订单 00123 蓝色 M 数量 37 件")
    rid = service.create(
        session_factory,
        CreateReview.model_validate(
            {
                "request_key": f"legacy-image-{legacy_field}",
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
        key="old-upload",
        filename="synthetic.png",
        data=content,
        database_url=database_url,
        storage_dir=tmp_path / "originals",
        ocr_data_dir=tessdata,
    )
    with session_factory() as session, begin_immediate(session):
        row = session.get(ReviewWorkspace, rid)
        data = copy.deepcopy(row.data)
        data["assets"][0]["reader_version"] = "bounded-sources-v1"
        old_doc = data["assets"][0]["document"]
        old_doc.pop("ocr_size_conflicts", None)
        if legacy_field == "missing":
            old_doc.pop("ocr_size_crosscheck", None)
        else:
            old_doc["ocr_size_crosscheck"] = None
        old_doc["warnings"] = ["OCR分数不是正确率，请逐项核对原图。"]
        row.data = data
    item = service.view(session_factory, rid)
    same_id, reused = assets.upload(
        session_factory,
        rid,
        revision=item["revision"],
        key="new-upload-key",
        filename="synthetic.png",
        data=content,
        database_url=database_url,
        storage_dir=tmp_path / "originals",
        ocr_data_dir=tessdata,
    )
    assert reused and same_id == sid
    with session_factory() as session:
        retained = session.get(ReviewWorkspace, rid).data["assets"][0]
        assert retained["reader_version"] == "bounded-sources-v1"
        assert retained["document"].get("ocr_size_crosscheck") is None
        assert "ocr_size_conflicts" not in retained["document"]
    settings = Settings(_env_file=None, database_url=database_url, ocr_data_dir=tessdata)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        page = client.get(f"/ui/reviews/{rid}/sources/{sid}")
        assert page.status_code == 200
        assert "尚未进行新的尺码字符交叉核对" in page.text
        assert "重复上传同名同内容会复用旧原件" in page.text
        assert "结果仍须逐项核对" in page.text
        item = service.view(session_factory, rid)
        assets.correct(
            session_factory,
            rid,
            sid,
            assets.AssetCorrection(
                revision=item["revision"],
                text="合成订单 00123 蓝色 M 数量 37 件",
                reason="测试脚本模拟对照原图转录，非独立真人",
            ),
        )
        corrected = client.get(f"/ui/reviews/{rid}/sources/{sid}")
        assert corrected.status_code == 200
        assert "当前显示人工转录" in corrected.text
        assert "这是旧版OCR结果，尚未进行新的尺码字符交叉核对" not in corrected.text
        assert "原始自动读取结果" in corrected.text
