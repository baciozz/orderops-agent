"""Immutable local originals, explicit roles and task-scoped confirmation."""

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ReviewWorkspace
from app.db.session import begin_immediate
from app.errors import AppError
from app.review import service
from app.review.contracts import RevisionRequest, Snapshot
from app.review.documents import Document, invalid, read_document, text_document
from app.review.mapping import ROLES, MappingSpec, build_snapshot, mapped_records
from app.services.source_files import read_original
from app.services.source_storage import new_original, prepare_source_storage

MAX_ASSETS = 6
READER_VERSION = "bounded-sources-v4-numeric-review"
NUMERIC_FIELDS = ("ocr_numeric_check", "ocr_numeric_low_count", "ocr_numeric_low_regions")


class AssetConfirmation(RevisionRequest):
    role: str
    mapping: MappingSpec | None = None
    fields_checked: bool
    context_complete: bool
    cjk_low_confidence_checked: bool = False
    numeric_fields_checked: bool = False
    note: str = Field(default="", max_length=2000)


class AssetCorrection(RevisionRequest):
    text: str = Field(min_length=1, max_length=60000)
    reason: str = Field(min_length=1, max_length=2000)


class BaselineConfirmation(RevisionRequest):
    captured_at: str = Field(min_length=1, max_length=100)
    synthetic: bool


class OrderSelection(RevisionRequest):
    order_id: str = Field(min_length=1, max_length=100)


def _asset(data: dict, asset_id: str) -> dict:
    found = next((item for item in data.get("assets", []) if item["id"] == asset_id), None)
    if found is None:
        raise AppError("该任务没有这份原件", code="NOT_FOUND", http_status=404)
    return found


def original(asset: dict, *, preview: bool = False) -> bytes:
    prefix = "preview_" if preview else ""
    if preview and not asset.get("preview_path"):
        raise AppError("没有图片预览", code="NOT_FOUND", http_status=404)
    return read_original(
        asset[prefix + "path"],
        asset[prefix + "storage_id"],
        asset[prefix + "size"],
        asset[prefix + "sha256"],
    )


def content(
    sessions: sessionmaker[Session], review_id: str, asset_id: str, *, preview: bool = False
) -> tuple[bytes, str, str]:
    with sessions() as session:
        asset = _asset(service._row(session, review_id).data, asset_id)
        data = original(asset, preview=preview)
        suffix = ".jpg" if preview else Path(asset["filename"]).suffix.lower()
        media = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".pdf": "application/pdf",
        }.get(suffix, "application/octet-stream")
        return data, media, suffix


def effective_document(asset: dict) -> Document:
    if not asset.get("corrected_text"):
        raw = dict(asset["document"])
        numeric = asset.get("ocr_numeric")
        if numeric is not None:
            if not isinstance(numeric, dict) or set(numeric) != set(NUMERIC_FIELDS):
                raise invalid(
                    "数字复核数据不完整，请对照原图完整转录。", "OCR_NUMERIC_REVIEW_REQUIRED"
                )
            if any(key in raw and raw[key] != numeric[key] for key in NUMERIC_FIELDS):
                raise invalid(
                    "数字复核数据不一致，请对照原图完整转录。", "OCR_NUMERIC_REVIEW_REQUIRED"
                )
            raw.update(numeric)
        return Document.model_validate(raw)
    doc = text_document(asset["corrected_text"].encode(), markdown=True)
    doc.kind = "human_transcription"
    for span in doc.spans:
        span.locator = "人工转录，" + span.locator + "（对照原件；无自动字段坐标）"
    for table in doc.tables:
        table.locators = [["人工转录，" + ref for ref in row] for row in table.locators]
    doc.warnings = ["这是人工纠正/转录，原OCR/文本层读取结果仍保留；不冒充自动识别准确率。"]
    return doc


def _require_reviewed_ocr(
    document: Document,
    *,
    cjk_low_confidence_checked: bool = False,
    numeric_fields_checked: bool = False,
) -> None:
    """Do not adopt a new image with a known or unchecked glyph ambiguity.

    A saved correction becomes a separately marked transcription. An old image
    without numeric cues must be read in a new task or transcribed, preserving
    its original and history; it cannot silently bypass the new review.
    """
    if document.kind == "image" and (
        document.ocr_size_crosscheck == "unavailable" or document.ocr_size_conflicts
    ):
        raise invalid(
            "尺码相近字符存在读取分歧，或交叉核对不可用；请对照原图完整转录后再确认，"
            "不会自动猜测或替换。",
            "OCR_SIZE_REVIEW_REQUIRED",
        )
    if document.kind == "image" and document.ocr_cjk_confidence == "unavailable":
        raise invalid(
            "低分中文区域无法可靠核对，请对照原图完整转录后再确认。",
            "OCR_TEXT_REVIEW_REQUIRED",
        )
    if (
        document.kind == "image"
        and document.ocr_cjk_confidence == "review"
        and not cjk_low_confidence_checked
    ):
        raise invalid(
            "请逐一对照原图核对低分中文；有误或不确定时先完整转录，确认无误后才能勾选。",
            "OCR_TEXT_REVIEW_REQUIRED",
        )
    if document.kind == "image" and document.ocr_numeric_check is None:
        raise invalid(
            "旧图片尚未进行数字低分检查；请新建任务重读，或对照原图完整转录后确认。",
            "OCR_NUMERIC_REVIEW_REQUIRED",
        )
    if document.kind == "image" and document.ocr_numeric_check == "unavailable":
        raise invalid(
            "数字低分检查不可用；请对照原图完整转录后再确认。",
            "OCR_NUMERIC_REVIEW_REQUIRED",
        )
    if document.kind == "image" and document.ocr_numeric_low_count:
        raise invalid(
            "含数字的低分片段可能涉及数量、金额、日期或编号；请对照原图完整转录后再确认。",
            "OCR_NUMERIC_REVIEW_REQUIRED",
        )
    if document.kind == "image" and not numeric_fields_checked:
        raise invalid(
            "请对照原图核对所有编号、数量、金额、日期及漏读内容；有误或不确定时先完整转录。",
            "OCR_NUMERIC_REVIEW_REQUIRED",
        )


def _invalidate(data: dict, event: dict) -> dict:
    updated = deepcopy(data)
    updated["history"] += [
        {**event, "at": service._now().isoformat(), "previous_proposal": updated.get("proposal")}
    ]
    updated.update(proposal=None, check=None, human=False, nonce=None, usage=None)
    return updated


def _root(database_url: str, configured: Path | None) -> Path:
    if configured is not None:
        root = configured
    elif database_url.startswith("sqlite:///") and not database_url.endswith(":memory:"):
        root = Path(database_url.removeprefix("sqlite:///")).absolute().parent / "review-originals"
    else:
        raise invalid(
            "文件清单需本地持久数据库或显式原件目录，请先配置存储", "SOURCE_STORAGE_UNAVAILABLE"
        )
    prepare_source_storage(root)
    return root


def _store(root: Path, data: bytes) -> dict:
    name = "RVO-" + uuid4().hex
    with new_original(root, name) as (path, stream):
        stream.write(data)
    return {
        "storage_id": name,
        "path": str(path),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def upload(
    sessions: sessionmaker[Session],
    review_id: str,
    *,
    revision: int,
    key: str,
    filename: str,
    data: bytes,
    database_url: str,
    storage_dir: Path | None,
    encoding: str = "utf-8-sig",
    rotation: int = 0,
    ocr_data_dir: Path | None = None,
) -> tuple[str, bool]:
    if (
        not 1 <= len(key) <= 100
        or not filename
        or len(filename) > 256
        or any(ord(char) < 32 for char in filename)
    ):
        raise invalid("请使用有效文件名和上传标识")
    fingerprint = hashlib.sha256(
        json.dumps([filename, encoding, rotation], ensure_ascii=False).encode() + b"\0" + data
    ).hexdigest()
    with sessions() as session, begin_immediate(session):
        row = service._row(session, review_id)
        receipt = row.data.get("upload_receipts", {}).get(key)
        if receipt:
            if receipt["fingerprint"] != fingerprint:
                raise invalid("上传标识已用于另一文件，请使用新标识", "IDEMPOTENCY_CONFLICT")
            original(_asset(row.data, receipt["asset_id"]))
            return receipt["asset_id"], True
        service._version(row, revision)
        if row.status == "CANCELLED":
            raise invalid("任务已取消，请新建处理任务", "REVIEW_STATE")
        existing = next(
            (a for a in row.data.get("assets", []) if a["fingerprint"] == fingerprint), None
        )
        if existing:
            original(existing)
            updated = deepcopy(row.data)
            updated.setdefault("upload_receipts", {})[key] = {
                "fingerprint": fingerprint,
                "asset_id": existing["id"],
            }
            row.data = updated  # Receipt only; confirmation and model input are unchanged.
            return existing["id"], True
        if len(row.data.get("assets", [])) >= MAX_ASSETS:
            raise invalid("每任务最多6份原件，更多资料请分任务处理", "DOCUMENT_SIZE_LIMIT")
    document, preview = read_document(
        filename, data, encoding=encoding, rotation=rotation, ocr_data_dir=ocr_data_dir
    )
    root = _root(database_url, storage_dir)
    stored = _store(root, data)
    preview_meta = _store(root, preview) if preview is not None else {}
    asset_id = "RVS-" + uuid4().hex
    stored_document = document.model_dump(mode="json")
    numeric = {key: stored_document.pop(key) for key in NUMERIC_FIELDS}
    asset = {
        "id": asset_id,
        "filename": filename,
        "fingerprint": fingerprint,
        **stored,
        "role": "unknown",
        "confirmed": False,
        "records": [],
        "mapping": None,
        # Keep optional reader metadata outside the strict Document schema so
        # the immediately previous version can still open new records.
        "document": stored_document,
        "reader_version": READER_VERSION,
        "encoding": encoding,
        "rotation": rotation,
        "note": "",
        **{"preview_" + k: v for k, v in preview_meta.items()},
    }
    if document.kind == "image":
        asset["ocr_numeric"] = numeric
    with sessions() as session, begin_immediate(session):
        row = service._row(session, review_id)
        service._version(row, revision)
        if row.status == "CANCELLED" or len(row.data.get("assets", [])) >= MAX_ASSETS:
            raise invalid("任务已变化，请刷新重试；此次原件未被采用", "REVIEW_STATE")
        updated = _invalidate(
            row.data,
            {
                "kind": "source_uploaded",
                "source_id": asset_id,
                "filename": filename,
                "sha256": stored["sha256"],
            },
        )
        updated.setdefault("assets", []).append(asset)
        updated.setdefault("upload_receipts", {})[key] = {
            "fingerprint": fingerprint,
            "asset_id": asset_id,
        }
        service._save(row, updated, "DRAFT")
    # Uncertain commits may leave unreferenced private files. Do not delete them
    # automatically; the explicit dry-run retention tool handles these later.
    return asset_id, False


def confirm(
    sessions: sessionmaker[Session], review_id: str, asset_id: str, body: AssetConfirmation
) -> None:
    with sessions() as session:
        current = service._row(session, review_id).data
        asset = _asset(current, asset_id)
        if body.role != "excluded":
            original(asset)
        document = effective_document(asset)
        if body.role not in ROLES or body.role == "unknown":
            raise invalid("请明确选择原件角色，不会按数字猜用途", "SOURCE_ROLE_REQUIRED")
        if not body.fields_checked or not body.context_complete:
            raise invalid("关键字段或资料完整性尚未确认", "SOURCE_CONFIRMATION_REQUIRED")
        if body.role == "excluded" and not body.note.strip():
            raise invalid("明确排除须填写原因并保留未覆盖范围", "SOURCE_CONFIRMATION_REQUIRED")
        if (
            body.role == "instruction"
            and not body.note.strip()
            and any(
                other["id"] != asset_id
                and other["filename"] == asset["filename"]
                and other["sha256"] != asset["sha256"]
                and other.get("confirmed")
                and other["role"] == "instruction"
                for other in current.get("assets", [])
            )
        ):
            raise invalid("同名客户文件内容不同，请说明两者关系或明确排除旧件", "SOURCE_CONFLICT")
        if not document.confirmable and body.role != "excluded":
            raise invalid(
                "原件有未读内容，请补充可核对转录或改传图片", "SOURCE_CONFIRMATION_REQUIRED"
            )
        if body.role != "excluded":
            _require_reviewed_ocr(
                document,
                cjk_low_confidence_checked=body.cjk_low_confidence_checked,
                numeric_fields_checked=body.numeric_fields_checked,
            )
        if document.kind == "image" and any((s.confidence or 0) < 80 for s in document.spans):
            if not body.note.strip():
                raise invalid(
                    "低OCR分数行须核对并记录处理；有误请先纠正", "SOURCE_CONFIRMATION_REQUIRED"
                )
        records = []
        if body.role in {"orders", "inventory", "fulfillment", "documents"}:
            if body.mapping is None:
                raise invalid("请确认表格布局与列映射；图/PDF可先转录为表格", "MAPPING_REQUIRED")
            records = mapped_records(document, body.mapping, asset_id)
        elif document.kind == "xlsx" and body.role == "instruction":
            if body.mapping is None or body.mapping.table_id not in {
                t.table_id for t in document.tables
            }:
                raise invalid("工作簿须明确选择采用的工作表", "MAPPING_REQUIRED")
        cjk_checked = (
            body.role != "excluded"
            and document.kind == "image"
            and document.ocr_cjk_confidence == "review"
            and body.cjk_low_confidence_checked
        )
        numeric_checked = (
            body.role != "excluded" and document.kind == "image" and body.numeric_fields_checked
        )
    with sessions() as session, begin_immediate(session):
        row = service._row(session, review_id)
        service._version(row, body.revision)
        if row.status == "CANCELLED":
            raise invalid("任务已取消", "REVIEW_STATE")
        updated = _invalidate(
            row.data,
            {
                "kind": "source_confirmed",
                "source_id": asset_id,
                "role": body.role,
                "note": body.note,
                "cjk_low_confidence_checked": cjk_checked,
                "numeric_fields_checked": numeric_checked,
            },
        )
        changed = _asset(updated, asset_id)
        old_role = changed["role"]
        changed.update(
            role=body.role,
            confirmed=True,
            records=records,
            note=body.note,
            cjk_low_confidence_checked=cjk_checked,
            numeric_fields_checked=numeric_checked,
            mapping=body.mapping.model_dump(mode="json") if body.mapping else None,
        )
        if {body.role, old_role} & {"orders", "inventory", "fulfillment", "documents"}:
            updated["baseline_dirty"] = True
        service._save(row, updated, "DRAFT")


def correct(
    sessions: sessionmaker[Session], review_id: str, asset_id: str, body: AssetCorrection
) -> None:
    with sessions() as session, begin_immediate(session):
        row = service._row(session, review_id)
        service._version(row, body.revision)
        if row.status == "CANCELLED":
            raise invalid("任务已取消", "REVIEW_STATE")
        updated = _invalidate(
            row.data,
            {
                "kind": "source_transcribed",
                "source_id": asset_id,
                "reason": body.reason,
                "previous_text": _asset(row.data, asset_id).get("corrected_text"),
            },
        )
        asset = _asset(updated, asset_id)
        original(asset)
        text_document(body.text.encode(), markdown=True)
        asset.update(corrected_text=body.text, correction_reason=body.reason, confirmed=False)
        if asset["role"] in {"orders", "inventory", "fulfillment", "documents"}:
            updated["baseline_dirty"] = True
        service._save(row, updated, "DRAFT")


def apply_baseline(
    sessions: sessionmaker[Session], review_id: str, body: BaselineConfirmation
) -> None:
    with sessions() as session:
        row = service._row(session, review_id)
        for asset in row.data.get("assets", []):
            if asset.get("confirmed") and asset["role"] not in {"instruction", "excluded"}:
                original(asset)
        snapshot, provenance = build_snapshot(
            row.data.get("assets", []), captured_at=body.captured_at, synthetic=body.synthetic
        )
    with sessions() as session, begin_immediate(session):
        row = service._row(session, review_id)
        service._version(row, body.revision)
        if row.status == "CANCELLED":
            raise invalid("任务已取消", "REVIEW_STATE")
        updated = _invalidate(
            row.data,
            {"kind": "baseline_imported", "previous_snapshot": row.data["input"].get("snapshot")},
        )
        updated["input"]["snapshot"] = snapshot.model_dump(mode="json")
        updated["input"]["selected_order_id"] = None
        updated.update(snapshot_origins=provenance, baseline_dirty=False)
        service._save(row, updated, "DRAFT")


def select_order(sessions: sessionmaker[Session], review_id: str, body: OrderSelection) -> None:
    with sessions() as session, begin_immediate(session):
        row = service._row(session, review_id)
        service._version(row, body.revision)
        snapshot = ready_snapshot(row.data, require_selection=False)
        if body.order_id not in {order.order_id for order in snapshot.orders}:
            raise invalid("所选订单不在基线中，不会自动改绑", "ORDER_SELECTION_REQUIRED")
        if row.status == "CANCELLED":
            raise invalid("任务已取消", "REVIEW_STATE")
        updated = _invalidate(row.data, {"kind": "order_selected", "order_id": body.order_id})
        updated["input"]["selected_order_id"] = body.order_id
        service._save(row, updated, "DRAFT")


def model_lines(data: dict, *, verify: bool = True) -> list[dict]:
    if data.get("baseline_dirty"):
        raise invalid("原件/映射已变化，请重新应用基线并选单", "BASELINE_REQUIRED")
    lines = [
        {"text": text, "source_id": "INPUT", "locator": f"客户文字第{i}行", "human": False}
        for i, text in enumerate(data["input"].get("instruction", "").splitlines(), 1)
        if text.strip()
    ]
    for asset in data.get("assets", []):
        if not asset.get("confirmed") or asset["role"] == "unknown":
            raise invalid(
                "新原件尚未确认角色和字段，请确认或明确排除", "SOURCE_CONFIRMATION_REQUIRED"
            )
        if asset["role"] == "excluded":
            continue
        if verify:
            original(asset)
        if asset["role"] != "instruction":
            continue
        doc = effective_document(asset)
        _require_reviewed_ocr(
            doc,
            cjk_low_confidence_checked=bool(asset.get("cjk_low_confidence_checked")),
            numeric_fields_checked=bool(asset.get("numeric_fields_checked")),
        )
        spans: list[dict[str, Any]]
        if doc.kind == "xlsx":
            table_id = (asset.get("mapping") or {}).get("table_id")
            table = next(table for table in doc.tables if table.table_id == table_id)
            spans = [
                {"text": "\t".join(row), "locator": " / ".join(table.locators[i])}
                for i, row in enumerate(table.rows)
                if any(row)
            ]
        else:
            spans = [span.model_dump() for span in doc.spans]
        for span in spans:
            line = {
                "text": span["text"],
                "source_id": asset["id"],
                "locator": span["locator"],
                "human": bool(asset.get("corrected_text")),
                "source_note": asset["note"],
            }
            # These numbers describe the reader's extraction order, not visual
            # adjacency, paragraph boundaries, or permission to join quotations.
            if doc.kind == "pdf":
                match = re.fullmatch(r"PDF第([1-9][0-9]*)页，提取行([1-9][0-9]*)", span["locator"])
                if (
                    match
                    and type(span.get("page")) is int
                    and type(doc.pages) is int
                    and span["page"] == int(match[1])
                    and span["page"] <= doc.pages
                ):
                    line.update(source_page=int(match[1]), source_extracted_line=int(match[2]))
            lines.append(line)
    if not lines:
        raise invalid("请提供客户要求文字或确认客户要求原件", "INPUT_EMPTY")
    return [{"line": i, **line} for i, line in enumerate(lines, 1)]


def ready_snapshot(data: dict, *, require_selection: bool = True) -> Snapshot:
    try:
        snapshot = Snapshot.model_validate(data["input"].get("snapshot"))
    except ValidationError:
        raise invalid("请先导入有效订单、库存与履约基线", "BASELINE_REQUIRED") from None
    if require_selection and data["input"].get("selected_order_id") not in {
        order.order_id for order in snapshot.orders
    }:
        raise invalid("请从候选订单中明确选择，不会自动选单", "ORDER_SELECTION_REQUIRED")
    return snapshot


def public_data(raw: dict) -> dict:
    data = deepcopy(raw)
    for asset in data.get("assets", []):
        document = asset.get("document")
        numeric = asset.get("ocr_numeric")
        if isinstance(document, dict):
            if isinstance(numeric, dict):
                for key in NUMERIC_FIELDS:
                    if key in numeric:
                        document.setdefault(key, numeric[key])
            document.setdefault("ocr_numeric_check", None)
            document.setdefault("ocr_numeric_low_count", 0)
            document.setdefault("ocr_numeric_low_regions", [])
        for field in ("path", "preview_path", "storage_id", "preview_storage_id"):
            asset.pop(field, None)
    return data


def restore(
    sessions: sessionmaker[Session],
    review_id: str,
    asset_id: str,
    *,
    revision: int,
    data: bytes,
    database_url: str,
    storage_dir: Path | None,
) -> None:
    with sessions() as session:
        asset = _asset(service._row(session, review_id).data, asset_id)
        if len(data) != asset["size"] or hashlib.sha256(data).hexdigest() != asset["sha256"]:
            raise invalid("恢复文件与保留哈希不一致；新资料须另行上传", "SOURCE_CONFLICT")
    stored = _store(_root(database_url, storage_dir), data)
    with sessions() as session, begin_immediate(session):
        row = service._row(session, review_id)
        service._version(row, revision)
        updated = deepcopy(row.data)
        _asset(updated, asset_id).update(stored)
        updated["history"] += [
            {"kind": "original_restored", "source_id": asset_id, "sha256": stored["sha256"]}
        ]
        service._save(row, updated, row.status)


def create_draft(sessions: sessionmaker[Session], request_key: str, instruction: str = "") -> str:
    if not 1 <= len(request_key) <= 100 or len(instruction) > 15000:
        raise invalid("请求标识或文字长度无效")
    raw = {
        "request_key": request_key,
        "selected_order_id": None,
        "instruction": instruction,
        "snapshot": None,
    }
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
    with sessions() as session, begin_immediate(session):
        existing = session.scalar(
            select(ReviewWorkspace).where(ReviewWorkspace.request_key == request_key)
        )
        if existing:
            if existing.input_digest != digest:
                raise invalid("同一请求标识已有不同资料", "IDEMPOTENCY_CONFLICT")
            return existing.id
        review_id = "RV-" + uuid4().hex
        now = service._now()
        session.add(
            ReviewWorkspace(
                id=review_id,
                request_key=request_key,
                input_digest=digest,
                revision=1,
                status="DRAFT",
                created_at=now,
                updated_at=now,
                data={
                    "input": raw,
                    "history": [],
                    "attempts": [],
                    "proposal": None,
                    "check": None,
                    "human": False,
                    "assets": [],
                },
            )
        )
        return review_id
