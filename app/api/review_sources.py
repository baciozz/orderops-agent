"""File-based checklist UI/API. Roles and mappings always require confirmation."""

from email import policy
from email.parser import BytesParser
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app.api.reviews import _form, _page
from app.errors import AppError
from app.review import assets, service
from app.review.assets import (
    AssetConfirmation,
    AssetCorrection,
    BaselineConfirmation,
    OrderSelection,
)
from app.review.contracts import ReviewModel, RevisionRequest
from app.review.documents import invalid
from app.review.mapping import ALIASES, FIELDS, ROLES, MappingSpec, suggested_columns
from app.web import templates

router = APIRouter()
ROLE_FIELDS = {
    "orders": [
        "order_id",
        "revision",
        "sku_id",
        "style",
        "color",
        "size",
        "fulfillment_mode",
        "ordered_qty",
        "shipped_qty",
        "cut_qty",
        "captured_at",
    ],
    "inventory": ["sku_id", "style", "color", "size", "available_qty"],
    "fulfillment": [
        "order_id",
        "revision",
        "sku_id",
        "style",
        "color",
        "size",
        "shipped_qty",
        "cut_qty",
    ],
    "documents": [
        "doc_id",
        "kind",
        "order_id",
        "order_revision",
        "sku_id",
        "style",
        "color",
        "size",
        "quantity",
        "status",
    ],
}


class DraftRequest(ReviewModel):
    request_key: str = Field(min_length=1, max_length=100)
    instruction: str = Field(default="", max_length=15000)


class TextRequest(RevisionRequest):
    instruction: str = Field(max_length=15000)


async def upload_form(request: Request) -> tuple[dict[str, str], str, bytes]:
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data;") or any(
        c in content_type for c in "\r\n"
    ):
        raise invalid("请通过文件选择框上传一个文件")
    message = BytesParser(policy=policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + await request.body()
    )
    if not message.is_multipart() or message.defects:
        raise invalid("上传格式无效，请重新选择文件")
    values, files = {}, []
    for part in message.iter_parts():
        if part.defects or part.get_content_disposition() != "form-data":
            raise invalid("上传字段格式无效")
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise invalid("上传字段必须是字节内容")
        if part.get_filename() is not None:
            if name != "file":
                raise invalid("请使用file字段上传一个文件")
            files.append((part.get_filename(), payload))
        else:
            if name not in {"revision", "key", "encoding", "rotation"} or name in values:
                raise invalid("上传字段重复或未知")
            try:
                values[name] = payload.decode("utf-8")
            except UnicodeError:
                raise invalid("上传字段须为UTF-8") from None
    if len(files) != 1 or not files[0][0]:
        raise invalid("每次请选择一个非空文件名的文件")
    return values, files[0][0], files[0][1]


@router.post("/reviews/drafts")
def new_draft(body: DraftRequest, request: Request):
    review_id = assets.create_draft(
        request.app.state.session_factory, body.request_key, body.instruction
    )
    return service.view(request.app.state.session_factory, review_id)


@router.post("/ui/reviews/draft")
async def new_ui_draft(request: Request):
    values = await _form(request)
    review_id = await run_in_threadpool(
        assets.create_draft,
        request.app.state.session_factory,
        values.get("request_key") or uuid4().hex,
        values.get("instruction", ""),
    )
    return RedirectResponse(f"/ui/reviews/{review_id}", 303)


@router.post("/reviews/{review_id}/sources")
async def upload_api(review_id: str, request: Request):
    values, filename, content = await upload_form(request)
    try:
        asset_id, duplicate = await run_in_threadpool(
            assets.upload,
            request.app.state.session_factory,
            review_id,
            revision=int(values["revision"]),
            key=values["key"],
            filename=filename,
            data=content,
            database_url=request.app.state.settings.database_url,
            storage_dir=request.app.state.source_storage_dir,
            encoding=values.get("encoding", "utf-8-sig"),
            rotation=int(values.get("rotation", 0)),
            ocr_data_dir=request.app.state.settings.ocr_data_dir,
        )
    except (ValueError, KeyError):
        raise invalid("上传版本或选项无效，请刷新重试") from None
    return {
        "source_id": asset_id,
        "duplicate": duplicate,
        "review": service.view(request.app.state.session_factory, review_id),
    }


@router.post("/ui/reviews/{review_id}/sources/upload")
async def upload_ui(review_id: str, request: Request):
    try:
        result = await upload_api(review_id, request)
    except AppError as exc:
        return _page(request, review_id, error=exc)
    return RedirectResponse(
        f"/ui/reviews/{review_id}/sources/{result['source_id']}"
        + ("?duplicate=1" if result["duplicate"] else ""),
        303,
    )


@router.post("/reviews/{review_id}/sources/{asset_id}/confirm")
def confirm_api(review_id: str, asset_id: str, body: AssetConfirmation, request: Request):
    assets.confirm(request.app.state.session_factory, review_id, asset_id, body)
    return service.view(request.app.state.session_factory, review_id)


@router.post("/reviews/{review_id}/sources/{asset_id}/correct")
def correct_api(review_id: str, asset_id: str, body: AssetCorrection, request: Request):
    assets.correct(request.app.state.session_factory, review_id, asset_id, body)
    return service.view(request.app.state.session_factory, review_id)


@router.post("/reviews/{review_id}/sources/apply-baseline")
def baseline_api(review_id: str, body: BaselineConfirmation, request: Request):
    assets.apply_baseline(request.app.state.session_factory, review_id, body)
    return service.view(request.app.state.session_factory, review_id)


@router.post("/reviews/{review_id}/select-order")
def select_api(review_id: str, body: OrderSelection, request: Request):
    assets.select_order(request.app.state.session_factory, review_id, body)
    return service.view(request.app.state.session_factory, review_id)


@router.post("/reviews/{review_id}/instruction")
def instruction_api(review_id: str, body: TextRequest, request: Request):
    with request.app.state.session_factory() as session:
        from app.db.session import begin_immediate

        with begin_immediate(session):
            row = service._row(session, review_id)
            service._version(row, body.revision)
            if row.status == "CANCELLED":
                raise invalid("任务已取消", "REVIEW_STATE")
            data = assets._invalidate(
                row.data,
                {"kind": "instruction_updated", "previous_text": row.data["input"]["instruction"]},
            )
            data["input"]["instruction"] = body.instruction
            service._save(row, data, "DRAFT")
    return service.view(request.app.state.session_factory, review_id)


@router.post("/reviews/{review_id}/sources/{asset_id}/restore")
async def restore_api(review_id: str, asset_id: str, request: Request):
    values, _, content = await upload_form(request)
    try:
        await run_in_threadpool(
            assets.restore,
            request.app.state.session_factory,
            review_id,
            asset_id,
            revision=int(values["revision"]),
            data=content,
            database_url=request.app.state.settings.database_url,
            storage_dir=request.app.state.source_storage_dir,
        )
    except (ValueError, KeyError):
        raise invalid("恢复版本无效，请刷新重试") from None
    return service.view(request.app.state.session_factory, review_id)


@router.get("/reviews/{review_id}/sources/{asset_id}/original")
def original_api(review_id: str, asset_id: str, request: Request, preview: bool = False):
    content, media, suffix = assets.content(
        request.app.state.session_factory, review_id, asset_id, preview=preview
    )
    disposition = "inline" if media.startswith("image/") else "attachment"
    return Response(
        content,
        media_type=media,
        headers={
            "content-disposition": f'{disposition}; filename="{asset_id}{suffix}"',
            "x-content-type-options": "nosniff",
            "cache-control": "no-store",
        },
    )


def source_page(
    request: Request,
    review_id: str,
    asset_id: str,
    *,
    error: AppError | None = None,
    submitted: dict | None = None,
):
    item = service.view(request.app.state.session_factory, review_id)
    asset = assets._asset(item["data"], asset_id)
    document = assets.effective_document(asset)
    values = submitted or dict(request.query_params)
    role = values.get("role", asset["role"])
    if role not in ROLES:
        role = "unknown"
    spec = MappingSpec.model_validate(asset.get("mapping") or {})
    table_id = values.get("table_id", spec.table_id)
    table = next((t for t in document.tables if t.table_id == table_id), None)
    if table is None and document.tables:
        table = document.tables[0]
        table_id = table.table_id
    try:
        header_row = max(
            1, min(int(values.get("header_row", spec.header_row)), len(table.rows) if table else 1)
        )
    except ValueError:
        header_row = 1
    headers = table.rows[header_row - 1] if table and table.rows else []
    same_selection = spec.table_id == table_id and spec.header_row == header_row
    columns = dict(spec.columns) if spec.columns and same_selection else suggested_columns(headers)
    for field in FIELDS:
        if submitted and "column_" + field in submitted:
            value = submitted["column_" + field]
            if value and value.isdigit() and 0 <= int(value) < 64:
                columns[field] = int(value)
            else:
                columns.pop(field, None)
    duplicate_names = [
        a
        for a in item["data"].get("assets", [])
        if a["id"] != asset_id
        and a["filename"] == asset["filename"]
        and a["sha256"] != asset["sha256"]
    ]
    return templates.TemplateResponse(
        request,
        "review_source_detail.html",
        {
            "item": item,
            "asset": asset,
            "document": document,
            "roles": ROLES,
            "role": role,
            "table": table,
            "table_id": table_id,
            "headers": headers,
            "header_row": header_row,
            "fields": ROLE_FIELDS.get(role, []),
            "field_labels": {f: ALIASES.get(f, [f])[0] for f in FIELDS},
            "columns": columns,
            "constants": spec.constants if same_selection else {},
            "layout": values.get("layout", spec.layout),
            "error": error,
            "submitted": submitted or {},
            "duplicate_names": duplicate_names,
            "upload_key": uuid4().hex,
        },
        status_code=error.http_status if error else 200,
    )


@router.get("/ui/reviews/{review_id}/sources/{asset_id}")
def inspect_source(review_id: str, asset_id: str, request: Request):
    return source_page(request, review_id, asset_id)


def _mapping(values: dict) -> MappingSpec | None:
    if not values.get("table_id"):
        return None
    columns = {
        field: int(values["column_" + field])
        for field in FIELDS
        if values.get("column_" + field) not in {None, "", "-1"}
    }
    constants = {
        field: values["constant_" + field] for field in FIELDS if values.get("constant_" + field)
    }
    sizes = [
        int(number.strip()) - 1
        for number in values.get("quantity_columns", "").split(",")
        if number.strip()
    ]
    return MappingSpec(
        table_id=values["table_id"],
        layout=values.get("layout", "rows"),
        header_row=int(values.get("header_row", 1)),
        columns=columns,
        constants=constants,
        quantity_columns=sizes,
        quantity_field=values.get("quantity_field", "ordered_qty"),
        expand_merged=values.get("expand_merged") == "yes",
        first_data_row=int(values["first_data_row"]) if values.get("first_data_row") else None,
        last_data_row=int(values["last_data_row"]) if values.get("last_data_row") else None,
    )


@router.post("/ui/reviews/{review_id}/sources/{asset_id}/{action}")
async def source_action(review_id: str, asset_id: str, action: str, request: Request):
    if action == "restore":
        try:
            await restore_api(review_id, asset_id, request)
        except AppError as exc:
            return source_page(request, review_id, asset_id, error=exc)
        return RedirectResponse(f"/ui/reviews/{review_id}/sources/{asset_id}", 303)
    values = await _form(request)
    try:
        if action == "confirm":
            body = AssetConfirmation(
                revision=int(values["revision"]),
                role=values["role"],
                mapping=_mapping(values),
                fields_checked=values.get("fields_checked") == "yes",
                context_complete=values.get("context_complete") == "yes",
                cjk_low_confidence_checked=values.get("cjk_low_confidence_checked") == "yes",
                numeric_fields_checked=values.get("numeric_fields_checked") == "yes",
                note=values.get("note", ""),
            )
            await run_in_threadpool(
                assets.confirm, request.app.state.session_factory, review_id, asset_id, body
            )
        elif action == "correct":
            correction = AssetCorrection(
                revision=int(values["revision"]), text=values["text"], reason=values["reason"]
            )
            await run_in_threadpool(
                assets.correct, request.app.state.session_factory, review_id, asset_id, correction
            )
        else:
            raise AppError("未知操作", code="NOT_FOUND", http_status=404)
    except (ValueError, KeyError, ValidationError):
        return source_page(
            request,
            review_id,
            asset_id,
            error=invalid("请检查映射、范围和确认项；输入已保留"),
            submitted=values,
        )
    except AppError as exc:
        return source_page(request, review_id, asset_id, error=exc, submitted=values)
    return RedirectResponse(f"/ui/reviews/{review_id}/sources/{asset_id}", 303)


@router.post("/ui/reviews/{review_id}/sources/apply-baseline")
async def baseline_ui(review_id: str, request: Request):
    values = await _form(request)
    try:
        body = BaselineConfirmation(
            revision=int(values["revision"]),
            captured_at=values["captured_at"],
            synthetic=values.get("synthetic") == "yes",
        )
        await run_in_threadpool(
            assets.apply_baseline, request.app.state.session_factory, review_id, body
        )
    except (ValueError, KeyError, ValidationError):
        return _page(
            request, review_id, error=invalid("请确认快照时间和版本，输入已保留"), submitted=values
        )
    except AppError as exc:
        return _page(request, review_id, error=exc, submitted=values)
    return RedirectResponse(f"/ui/reviews/{review_id}", 303)


@router.post("/ui/reviews/{review_id}/sources/select-order")
async def select_ui(review_id: str, request: Request):
    values = await _form(request)
    try:
        body = OrderSelection(revision=int(values["revision"]), order_id=values["order_id"])
        await run_in_threadpool(
            assets.select_order, request.app.state.session_factory, review_id, body
        )
    except (ValueError, KeyError, ValidationError):
        return _page(request, review_id, error=invalid("请明确选择订单"), submitted=values)
    except AppError as exc:
        return _page(request, review_id, error=exc, submitted=values)
    return RedirectResponse(f"/ui/reviews/{review_id}", 303)


@router.post("/ui/reviews/{review_id}/sources/instruction")
async def instruction_ui(review_id: str, request: Request):
    values = await _form(request)
    try:
        body = TextRequest(
            revision=int(values["revision"]), instruction=values.get("instruction", "")
        )
        await run_in_threadpool(instruction_api, review_id, body, request)
    except (ValueError, KeyError, ValidationError):
        return _page(
            request, review_id, error=invalid("文字或版本无效；请检查输入"), submitted=values
        )
    except AppError as exc:
        return _page(request, review_id, error=exc, submitted=values)
    return RedirectResponse(f"/ui/reviews/{review_id}", 303)
