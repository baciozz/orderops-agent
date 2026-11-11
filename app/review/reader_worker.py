"""A bounded file reader child. No HTTP, model clients, DB or dataset labels."""

import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import warnings
from collections import defaultdict
from io import BytesIO, StringIO
from pathlib import Path
from typing import Literal

from app.errors import AppError
from app.review.documents import (
    MAX_FILE_BYTES,
    MAX_PAGES,
    MAX_PIXELS,
    MAX_SIDE,
    MAX_TEXT,
    Document,
    OCRSizeConflict,
    Span,
    csv_document,
    invalid,
    text_document,
    xlsx_document,
)

_SIZE_TOKENS = frozenset({"XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"})
_UNCERTAIN_GLYPHS = frozenset({"上", "!", "I", "l", "1", "|", "丨"})
_CJK = re.compile(r"[\u3400-\u9fff]")
_CJK_REVIEW_THRESHOLD = 50.0
_NUMERIC_REVIEW_THRESHOLD = 80.0
_EDGE_PUNCTUATION = " \t,，.。;；:：()（）[]【】"
_LABELED_SIZE = re.compile(
    r"^(?:尺码|尺碼|码数|碼數|size)\s*[:：]?\s*(XXXS|XXS|XS|S|M|L|XL|XXL|XXXL)$",
    re.IGNORECASE,
)
_LABELED_UNCERTAIN = re.compile(
    r"^(?:尺码|尺碼|码数|碼數|size)\s*[:：]?\s*([上!Il1|丨])$", re.IGNORECASE
)


def _size_candidate(text: str) -> str | None:
    cleaned = text.strip(_EDGE_PUNCTUATION)
    if cleaned in _SIZE_TOKENS:
        return cleaned
    labeled = _LABELED_SIZE.fullmatch(cleaned)
    return labeled.group(1).upper() if labeled else None


def _uncertain_reason(text: str) -> Literal["uncertain_glyph", "uncertain_mixed"] | None:
    cleaned = text.strip(_EDGE_PUNCTUATION)
    if cleaned in _UNCERTAIN_GLYPHS or _LABELED_UNCERTAIN.fullmatch(cleaned):
        return "uncertain_glyph"
    # A size, quantity and delimiter may be merged into one OCR word, e.g.
    # "LV/37". This also conservatively flags low-scoring order/style codes.
    if re.search(r"[XSMLxsml]", cleaned) and re.search(r"[0-9]", cleaned):
        return "uncertain_mixed"
    return None


def _word_box(row: dict, width: int, height: int) -> tuple[int, int, int, int] | None:
    try:
        left, top, w, h = (int(row[name]) for name in ("left", "top", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return None
    if left < 0 or top < 0 or w <= 0 or h <= 0 or left + w > width or top + h > height:
        return None
    return left, top, w, h


def _overlap_fraction(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    intersection = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(
        0, min(ay + ah, by + bh) - max(ay, by)
    )
    return intersection / min(aw * ah, bw * bh)


def _ocr_words(output: bytes) -> list[dict]:
    rows = csv.DictReader(StringIO(output.decode("utf-8")), delimiter="\t")
    if not {"level", "text", "conf", "left", "top", "width", "height"} <= set(
        rows.fieldnames or []
    ):
        raise ValueError("invalid OCR TSV")
    return [row for row in rows if row["level"] == "5" and row["text"].strip()]


def _cjk_confidence_review(
    words: list[dict], width: int, height: int
) -> tuple[Literal["clear", "review", "unavailable"], int, list[list[int]]]:
    """Identify low-scoring CJK tokens without inferring or replacing text."""
    if not words or len(words) > 5000:
        return "unavailable", 0, []
    count = 0
    regions: list[list[int]] = []
    try:
        for word in words:
            if not _CJK.search(word["text"]):
                continue
            confidence = float(word["conf"])
            box = _word_box(word, width, height)
            if not math.isfinite(confidence) or box is None:
                return "unavailable", 0, []
            if confidence < _CJK_REVIEW_THRESHOLD:
                count += 1
                if len(regions) < 25:
                    regions.append(list(box))
    except (KeyError, TypeError, ValueError):
        return "unavailable", 0, []
    return ("review" if count else "clear"), count, regions


def _numeric_confidence_review(
    words: list[dict], width: int, height: int
) -> tuple[Literal["available", "unavailable"], int, list[list[int]]]:
    """Find low-score OCR words containing digits without assigning field meanings."""
    if not words or len(words) > 5000:
        return "unavailable", 0, []
    count = 0
    regions: list[list[int]] = []
    try:
        for word in words:
            if not any(char.isdecimal() for char in word["text"]):
                continue
            confidence = float(word["conf"])
            box = _word_box(word, width, height)
            if not math.isfinite(confidence) or box is None:
                return "unavailable", 0, []
            if confidence < _NUMERIC_REVIEW_THRESHOLD:
                count += 1
                if len(regions) < 25:
                    regions.append(list(box))
    except (KeyError, TypeError, ValueError):
        return "unavailable", 0, []
    return "available", count, regions


def _language_digests(data_dir: Path | None) -> dict[str, str]:
    """Identify explicitly configured, bounded regular data files when possible."""
    if data_dir is None:
        return {}
    result = {}
    for name in ("chi_sim", "chi_sim_vert", "eng"):
        path = data_dir / f"{name}.traineddata"
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 20_000_000:
                continue
            with path.open("rb") as stream:
                result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError:
            continue
    return result


def _size_disagreements(
    primary: list[dict], alternate: list[dict], width: int, height: int
) -> list[OCRSizeConflict]:
    """Flag overlapping disagreeing candidate glyphs without selecting an answer.

    A confidence threshold only decides when to request human review; it does
    not certify the alternate or the absence of errors. Limit work for dense
    images and ask for transcription when the comparison cannot be bounded.
    """
    if not primary or not alternate or len(primary) > 5000 or len(alternate) > 5000:
        raise ValueError("OCR word comparison unavailable")
    candidates = []
    for row in alternate:
        candidate = _size_candidate(row["text"])
        if candidate is None:
            continue
        confidence = float(row["conf"])
        if not math.isfinite(confidence):
            raise ValueError("invalid alternate OCR confidence")
        if confidence >= 80:
            candidates.append(row)
    if len(candidates) > 100:
        raise ValueError("too many OCR size candidates to compare safely")
    primary_boxes = [(_word_box(row, width, height), row) for row in primary]
    if any(box is None for box, _ in primary_boxes):
        raise ValueError("invalid primary OCR region")
    conflicts = []
    for row in candidates:
        box = _word_box(row, width, height)
        if box is None:
            raise ValueError("invalid alternate OCR region")
        matches = [
            word["text"]
            for primary_box, word in primary_boxes
            if primary_box is not None and _overlap_fraction(box, primary_box) >= 0.6
        ]
        if any(_size_candidate(text) == _size_candidate(row["text"]) for text in matches):
            continue
        conflicts.append(
            OCRSizeConflict(box=list(box), primary=" ".join(matches)[:120], alternate=row["text"])
        )
        if len(conflicts) > 25:
            raise ValueError("too many OCR disagreements to show safely")
    # A second recognizer may repeat the same error. A low-scoring isolated
    # glyph that can be confused with a size needs review even without a
    # disagreement; this does not assert that the glyph actually is a size.
    for box, word in primary_boxes:
        reason = _uncertain_reason(word["text"])
        if reason is None:
            continue
        confidence = float(word["conf"])
        if not math.isfinite(confidence):
            raise ValueError("invalid primary OCR confidence")
        if confidence >= 80:
            continue
        assert box is not None
        if any(
            _overlap_fraction(box, (item.box[0], item.box[1], item.box[2], item.box[3])) >= 0.6
            for item in conflicts
        ):
            continue
        conflicts.append(OCRSizeConflict(box=list(box), primary=word["text"], reason=reason))
        if len(conflicts) > 25:
            raise ValueError("too many OCR uncertainties to show safely")
    return conflicts


def pdf_document(content: bytes) -> Document:
    from pypdf import PdfReader

    if not content.startswith(b"%PDF-"):
        raise invalid("文件不是PDF，请检查导出格式")
    reader = PdfReader(BytesIO(content), strict=True)
    if reader.is_encrypted:
        raise invalid("不支持加密PDF，请本机解密后再上传")
    if not 1 <= len(reader.pages) <= MAX_PAGES:
        raise invalid("PDF限1–20页，请按订单拆分", "DOCUMENT_SIZE_LIMIT")
    spans, unreadable = [], []
    for page_number, page in enumerate(reader.pages, 1):
        text = page.extract_text(extraction_mode="layout", layout_mode_space_vertically=False)
        if not text or not text.strip():
            unreadable.append(page_number)
        for line_number, line in enumerate((text or "").splitlines(), 1):
            if line.strip():
                spans.append(
                    Span(
                        text=line,
                        locator=f"PDF第{page_number}页，提取行{line_number}",
                        page=page_number,
                    )
                )
        if sum(len(span.text) for span in spans) > MAX_TEXT:
            raise invalid("PDF文字超过60000字符上限，未截断资料", "DOCUMENT_SIZE_LIMIT")
    notes = ["PDF提取顺序可能与视觉表格不同；需打开原页确认关键字段，不伪造字段坐标。"]
    if unreadable:
        notes.append(
            "无可读取文本的页："
            + "、".join(map(str, unreadable))
            + "；可能空白或扫描页，请改传PNG/JPEG或补充可核对文字，不能默默忽略。"
        )
    return Document(
        kind="pdf", pages=len(reader.pages), spans=spans, warnings=notes, confirmable=not unreadable
    )


def image_document(content: bytes, preview: Path, rotation: int, data_dir: Path | None) -> Document:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with Image.open(BytesIO(content)) as original:
        if original.format not in {"PNG", "JPEG"} or getattr(original, "n_frames", 1) != 1:
            raise invalid("图片只支持单帧PNG/JPEG")
        if original.width * original.height > MAX_PIXELS or max(original.size) > MAX_SIDE:
            raise invalid("图片限800万像素、最长边6000像素，请缩小图像", "DOCUMENT_SIZE_LIMIT")
        image = ImageOps.exif_transpose(original).convert("RGB")
        if rotation:
            image = image.rotate(rotation, expand=True)
        image.save(preview, format="JPEG", quality=95)
    executable = shutil.which("tesseract")
    if executable is None:
        raise invalid("未安装本机Tesseract OCR，请按输入支持说明安装后重试", "OCR_NOT_CONFIGURED")
    extra = ["--tessdata-dir", str(data_dir)] if data_dir else []
    languages = subprocess.run(
        [executable, "--list-langs", *extra], capture_output=True, timeout=5, check=False
    ).stdout.decode("utf-8", errors="replace")
    if not re.search(r"(?m)^chi_sim$", languages):
        raise invalid(
            "缺少chi_sim OCR语言数据；请运行本地prepare_ocr步骤，不会用英文结果猜中文",
            "OCR_LANGUAGE_MISSING",
        )
    result = subprocess.run(
        [
            executable,
            str(preview),
            "stdout",
            "-l",
            "chi_sim",
            "--psm",
            "6",
            *extra,
            "-c",
            "tessedit_create_tsv=1",
        ],
        capture_output=True,
        timeout=25,
        check=False,
    )
    if result.returncode or len(result.stdout) > 2_000_000:
        raise invalid("OCR未能完成，请旋转、提高清晰度或提供文字", "OCR_READ_FAILED")
    primary_words = _ocr_words(result.stdout)
    cjk_check, cjk_count, cjk_regions = _cjk_confidence_review(
        primary_words, image.width, image.height
    )
    numeric_check, numeric_count, numeric_regions = _numeric_confidence_review(
        primary_words, image.width, image.height
    )
    grouped = defaultdict(list)
    for row in primary_words:
        grouped[(row["block_num"], row["par_num"], row["line_num"])].append(row)
    spans = []
    for number, line_words in enumerate(grouped.values(), 1):
        left = min(int(row["left"]) for row in line_words)
        top = min(int(row["top"]) for row in line_words)
        right = max(int(row["left"]) + int(row["width"]) for row in line_words)
        bottom = max(int(row["top"]) + int(row["height"]) for row in line_words)
        spans.append(
            Span(
                text=" ".join(row["text"] for row in line_words),
                locator=f"OCR校正预览第{number}行，"
                f"区域({left},{top},{right - left},{bottom - top})",
                box=[left, top, right - left, bottom - top],
                confidence=min(float(row["conf"]) for row in line_words),
            )
        )
    if not spans or sum(len(span.text) for span in spans) > MAX_TEXT:
        raise invalid("没有可靠可读文字或文字超限；请提供清晰完整图片或人工文字", "OCR_READ_FAILED")
    version = (
        subprocess.run([executable, "--version"], capture_output=True, timeout=5, check=False)
        .stdout.decode("utf-8", errors="replace")
        .splitlines()[0]
    )
    notes = [
        "实际从图像像素读取；区域坐标对应下面的EXIF/旋转校正预览，不是未校正原图。",
        "OCR分数不是正确率；请逐项核对款色码、数量、上下文和是否裁切，不清楚时先纠正。",
    ]
    if any((span.confidence or 0) < 80 for span in spans):
        notes.append("存在低OCR分数行，不能直接采用关键数量；请对照原图修正。")
    if cjk_check == "review":
        notes.append(
            f"发现{cjk_count}个低分中文片段；请逐一对照原图核对，错误或不确定时先完整转录。"
            "分数和未提示都不能保证文字正确。"
        )
    elif cjk_check == "unavailable":
        notes.append("低分中文区域无法可靠核对；请对照原图完整转录后再确认。")
    if numeric_check == "unavailable":
        notes.append("数字低分检查不可用，请对照原图完整转录后再确认。")
    elif numeric_count:
        notes.append(
            f"发现{numeric_count}个含数字的低分片段，请对照原图完整转录后再确认。"
            "未提示不代表数字、日期或缺失内容正确。"
        )
    # This alternate profile is only a bounded disagreement detector. Its text
    # never replaces or augments the primary OCR supplied to the application.
    crosscheck: Literal["checked", "unavailable"] = "unavailable"
    conflicts: list[OCRSizeConflict] = []
    if re.search(r"(?m)^eng$", languages):
        try:
            alternate = subprocess.run(
                [
                    executable,
                    str(preview),
                    "stdout",
                    "-l",
                    "chi_sim+eng",
                    "--psm",
                    "6",
                    *extra,
                    "-c",
                    "tessedit_create_tsv=1",
                ],
                capture_output=True,
                timeout=8,
                check=False,
            )
            if alternate.returncode or len(alternate.stdout) > 2_000_000:
                raise ValueError("alternate OCR failed")
            conflicts = _size_disagreements(
                primary_words, _ocr_words(alternate.stdout), image.width, image.height
            )
            crosscheck = "checked"
        except (OSError, UnicodeError, ValueError, KeyError, subprocess.TimeoutExpired):
            pass
    if crosscheck == "unavailable":
        notes.append("尺码字符交叉核对不可用；请对照原图完整转录后再确认，不能猜测尺寸。")
    elif conflicts:
        notes.append(
            "尺码相近字符存在不同读取或低分歧义，候选也可能有误；"
            "请对照原图完整转录后再确认。系统不会自动合并或替换OCR结果。"
        )
        notes.extend(
            (
                f"预览区域{tuple(item.box)}：主结果“{item.primary or '未读取'}”，"
                f"另一候选“{item.alternate}”；必须人工核对。"
                if item.reason == "disagreement"
                else f"预览区域{tuple(item.box)}：主结果“{item.primary}”分数低且可能与款码、"
                "尺码或数量混淆；不代表它一定是尺码，必须对照原图核对。"
            )
            for item in conflicts
        )
    else:
        notes.append("交叉核对未发现指定字符冲突不代表识别正确；仍须逐项对照原图。")
    return Document(
        kind="image",
        spans=spans,
        warnings=notes,
        image_width=image.width,
        image_height=image.height,
        rotation=rotation,
        ocr_version=version,
        ocr_language_sha256=_language_digests(data_dir),
        ocr_size_crosscheck=crosscheck,
        ocr_size_conflicts=conflicts,
        ocr_cjk_confidence=cjk_check,
        ocr_cjk_low_count=cjk_count,
        ocr_cjk_low_regions=cjk_regions,
        ocr_numeric_check=numeric_check,
        ocr_numeric_low_count=numeric_count,
        ocr_numeric_low_regions=numeric_regions,
    )


def main() -> None:
    # Limits apply inside a fresh Python child, without unsafe preexec_fn in the web process.
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (35, 35))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
    source, output, preview = map(Path, sys.argv[1:4])
    suffix, encoding, rotation = sys.argv[4:7]
    try:
        content = source.read_bytes()
        if len(content) > MAX_FILE_BYTES:
            raise invalid("文件超过大小上限", "DOCUMENT_SIZE_LIMIT")
        if suffix in {".txt", ".md"}:
            doc = text_document(content, markdown=suffix == ".md")
        elif suffix == ".csv":
            doc = csv_document(content, encoding=encoding)
        elif suffix == ".xlsx":
            doc = xlsx_document(content)
        elif suffix == ".pdf":
            doc = pdf_document(content)
        else:
            doc = image_document(
                content, preview, int(rotation), Path(sys.argv[7]) if len(sys.argv) > 7 else None
            )
        data = doc.model_dump(mode="json")
    except AppError as exc:
        data = {"error": {"code": exc.code, "message": exc.message}}
    except Exception:
        data = {
            "error": {
                "code": "DOCUMENT_READ_FAILED",
                "message": "读取失败，未采用不完整结果；请重新导出静态文件或提供人工文字",
            }
        }
    output.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
