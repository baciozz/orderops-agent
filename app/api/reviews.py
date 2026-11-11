"""Separate, loopback-only checklist UI/API; no route commits business records."""

import json
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.errors import AppError
from app.review import service
from app.review.contracts import (
    Correction,
    CreateReview,
    ExtractRequest,
    Proposal,
    RevisionRequest,
    UpdateEvidence,
)
from app.review.sample import SAMPLE_INSTRUCTION, SAMPLE_SNAPSHOT
from app.services.export import export_change_list
from app.web import templates

router = APIRouter()
STATUS_LABELS = {
    "DRAFT": "资料已保存，尚未提取",
    "RUNNING": "正在调用模型；未执行订单",
    "NEEDS_REVIEW": "请核对提案和约束",
    "CONFIRMED": "已确认处理清单；未执行订单",
    "ERROR": "提取失败；资料已保留",
    "CANCELLED": "已取消；未执行订单",
}


@router.post("/reviews")
def create_review(body: CreateReview, request: Request):
    review_id = service.create(request.app.state.session_factory, body)
    return service.view(request.app.state.session_factory, review_id)


@router.get("/reviews/{review_id}")
def review_view(review_id: str, request: Request):
    return service.view(request.app.state.session_factory, review_id)


@router.post("/reviews/{review_id}/extract")
def extract_review(review_id: str, body: ExtractRequest, request: Request):
    service.extract(request.app.state.session_factory, request.app.state.settings, review_id, body)
    return service.view(request.app.state.session_factory, review_id)


@router.post("/reviews/{review_id}/correct")
def correct_review(review_id: str, body: Correction, request: Request):
    service.correct(request.app.state.session_factory, review_id, body)
    return service.view(request.app.state.session_factory, review_id)


@router.post("/reviews/{review_id}/evidence")
def update_review(review_id: str, body: UpdateEvidence, request: Request):
    service.update_evidence(request.app.state.session_factory, review_id, body)
    return service.view(request.app.state.session_factory, review_id)


@router.post("/reviews/{review_id}/confirm")
def confirm_review(review_id: str, body: RevisionRequest, request: Request):
    service.confirm(request.app.state.session_factory, review_id, body.revision)
    return service.view(request.app.state.session_factory, review_id)


@router.post("/reviews/{review_id}/cancel")
def cancel_review(review_id: str, body: RevisionRequest, request: Request):
    service.cancel(request.app.state.session_factory, review_id, body.revision)
    return service.view(request.app.state.session_factory, review_id)


@router.post("/reviews/{review_id}/recover")
def recover_review(review_id: str, body: RevisionRequest, request: Request):
    service.recover(request.app.state.session_factory, review_id, body.revision)
    return service.view(request.app.state.session_factory, review_id)


@router.get("/reviews/{review_id}/export")
def download_review(review_id: str, request: Request, fmt: str = "csv"):
    if fmt not in {"csv", "xlsx"}:
        raise AppError("仅支持 CSV 或 XLSX", code="FORMAT_UNSUPPORTED")
    item = service.export_view(request.app.state.session_factory, review_id)
    data = item["data"]
    if item["status"] != "CONFIRMED":
        raise AppError(
            "请先核对并确认处理清单，未确认资料不能导出为处理结果",
            code="REVIEW_NOT_CONFIRMED",
            http_status=409,
        )
    rows = []
    source_map = data["check"].get("source_map", {})
    if data["human"]:
        references = "人工提案，非模型引用；参考资料范围：" + "; ".join(
            dict.fromkeys(
                value["source_id"] + " " + value["locator"] for value in source_map.values()
            )
        )
    else:
        references_list = []
        for change in data["proposal"]["changes"]:
            for ref in change["citations"]:
                source = source_map.get(str(ref["line"]), {})
                references_list.append(
                    f"{source.get('source_id', 'INPUT')}:{ref['line']} "
                    f"{ref['quote']} ({source.get('locator', '原文')})"
                )
        references = "; ".join(references_list)
    for row in data["check"]["rows"] or [
        {"sku_id": "", "old_qty": None, "new_qty": None, "delta": None}
    ]:
        rows.append(
            {
                "review_id": review_id,
                "review_revision": item["revision"],
                "order_id": data["input"]["selected_order_id"],
                **row,
                "state": "已确认处理清单；未执行订单",
                "instruction_source": references,
                "scope": data["check"]["scope"],
                "todo": "；".join(data["check"]["downstream"] + data["check"].get("warnings", []))
                or "无变更；请保留核对记录",
            }
        )
    return Response(
        export_change_list(rows, fmt=fmt),
        media_type=(
            "text/csv; charset=utf-8"
            if fmt == "csv"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"content-disposition": f'attachment; filename="checklist.{fmt}"'},
    )


def _new_page(request: Request, values: dict, error: AppError | None = None):
    return templates.TemplateResponse(
        request,
        "review_new.html",
        {"values": values, "error": error},
        status_code=error.http_status if error else 200,
    )


def _page(
    request: Request,
    review_id: str,
    *,
    error: AppError | None = None,
    submitted: dict | None = None,
):
    item = service.view(request.app.state.session_factory, review_id)
    settings = request.app.state.settings
    uploaded = item["data"].get("assets", [])
    baseline_times = {
        record["values"].get("captured_at")
        for asset in uploaded
        if asset.get("confirmed")
        for record in asset.get("records", [])
        if record["values"].get("captured_at")
    }
    manual_rows = (item["data"].get("proposal") or {}).get("changes") or [
        dict(sku_id="", mode="set_total", quantity="", to_sku="")
    ]
    if submitted and "changes_json" in submitted:
        try:
            parsed = json.loads(submitted["changes_json"])
            if isinstance(parsed, list) and len(parsed) <= 100:
                manual_rows = parsed
        except ValueError:
            pass
    selected = item["data"]["input"].get("selected_order_id")
    snapshot = item["data"]["input"].get("snapshot") or {}
    current_skus = {
        line["sku_id"]
        for order in snapshot.get("orders", [])
        if order["order_id"] == selected
        for line in order["lines"]
    }
    return templates.TemplateResponse(
        request,
        "review_detail.html",
        {
            "item": item,
            "data": item["data"],
            "manual_rows": manual_rows,
            "manual_skus": [
                sku for sku in snapshot.get("catalog", []) if sku["sku_id"] in current_skus
            ],
            "error": error,
            "submitted": submitted or {},
            "label": "历史确认需重新核对"
            if item["recheck_required"]
            else STATUS_LABELS[item["status"]],
            "call_key": uuid4().hex,
            "upload_key": uuid4().hex,
            "baseline_time": next(iter(baseline_times)) if len(baseline_times) == 1 else "",
            "provider": settings.llm_provider or "未配置",
            "model": settings.llm_model or "未配置",
            "cap": str(settings.effective_live_budget_usd),
            "snapshot_json": json.dumps(
                item["data"]["input"]["snapshot"], ensure_ascii=False, indent=2
            ),
            "history_json": json.dumps(item["data"]["history"], ensure_ascii=False, indent=2),
        },
        status_code=error.http_status if error else 200,
    )


async def _form(request: Request) -> dict[str, str]:
    if (
        request.headers.get("content-type", "").split(";", 1)[0]
        != "application/x-www-form-urlencoded"
    ):
        raise AppError("请使用页面表单", code="VALIDATION_ERROR")
    try:
        parsed = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    except UnicodeError:
        raise AppError("表单须为 UTF-8", code="ENCODING_INVALID") from None
    return {key: values[0] for key, values in parsed.items()}


@router.get("/ui/reviews", response_class=HTMLResponse)
def review_list(request: Request):
    return templates.TemplateResponse(
        request,
        "review_list.html",
        {"items": service.recent(request.app.state.session_factory), "labels": STATUS_LABELS},
    )


@router.get("/ui/reviews/new", response_class=HTMLResponse)
def new_page(request: Request):
    return _new_page(
        request,
        {
            "request_key": uuid4().hex,
            "selected_order_id": "SYN-2401",
            "instruction": SAMPLE_INSTRUCTION,
            "snapshot": json.dumps(SAMPLE_SNAPSHOT, ensure_ascii=False, indent=2),
        },
    )


@router.post("/ui/reviews/new", response_class=HTMLResponse)
async def submit_new(request: Request):
    values = await _form(request)
    try:
        body = CreateReview.model_validate({**values, "snapshot": json.loads(values["snapshot"])})
        review_id = await run_in_threadpool(service.create, request.app.state.session_factory, body)
    except (ValidationError, ValueError, KeyError):
        return _new_page(
            request,
            values,
            AppError(
                "请检查快照 JSON、重复行、数量及所选订单；原输入已保留", code="REVIEW_INPUT_INVALID"
            ),
        )
    except AppError as exc:
        return _new_page(request, values, exc)
    return RedirectResponse(f"/ui/reviews/{review_id}", status_code=303)


@router.get("/ui/reviews/{review_id}", response_class=HTMLResponse)
def detail_page(review_id: str, request: Request):
    return _page(request, review_id)


def _human_proposal(values: dict, item: dict) -> Proposal:
    raw = item["data"]["input"]
    if not raw.get("snapshot") or not raw.get("selected_order_id"):
        raise AppError("请先导入业务基线并明确选择订单", code="BASELINE_REQUIRED")
    order = next(o for o in raw["snapshot"]["orders"] if o["order_id"] == raw["selected_order_id"])
    # Keep the original single-action form compatible; the browser editor sends
    # the complete explicit action list, never a patch that silently drops rows.
    rows = json.loads(values["changes_json"]) if "changes_json" in values else [values]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise AppError("请保留1–100项明确动作", code="REVIEW_INPUT_INVALID")
    changes = []
    for row in rows:
        if not isinstance(row, dict):
            raise AppError("动作格式无效，原输入已保留", code="REVIEW_INPUT_INVALID")
        sku = row["sku_id"]
        old = next((line for line in order["lines"] if line["sku_id"] == sku), None)
        if old is None:
            raise AppError("请选择订单中实际存在的 SKU", code="REVIEW_INPUT_INVALID")
        mode = row["mode"]
        value = row.get("quantity")
        if mode == "cancel_remaining":
            qty = None
        elif type(value) is int:
            qty = value
        elif (
            isinstance(value, str) and value.lstrip("+-").isascii() and value.lstrip("+-").isdigit()
        ):
            qty = int(value)
        else:
            raise AppError("数量须为明确整数，不会四舍五入或截断", code="REVIEW_INPUT_INVALID")
        after = (
            qty
            if mode == "set_total"
            else old["ordered_qty"] + qty
            if mode == "increment" and qty is not None
            else old["ordered_qty"] - qty
            if mode == "replace_remaining" and qty is not None
            else max(old.get("shipped_qty") or 0, old.get("cut_qty") or 0)
        )
        changes.append(
            {
                "mode": mode,
                "sku_id": sku,
                "to_sku": row.get("to_sku") or None,
                "quantity": qty,
                "unit": "件",
                "before_qty": old["ordered_qty"],
                "after_qty": after,
                "citations": [],
            }
        )
    return Proposal.model_validate(
        {
            "order_id": raw["selected_order_id"],
            "disposition": "propose",
            "summary": "人工输入/纠正，未经真实模型验证",
            "questions": [],
            "changes": changes,
        }
    )


def _human_no_change(values: dict, item: dict) -> tuple[Proposal, str]:
    raw = item["data"]["input"]
    selected = raw.get("selected_order_id")
    snapshot = raw.get("snapshot") or {}
    if not selected or selected not in {o["order_id"] for o in snapshot.get("orders", [])}:
        raise AppError("请先导入业务基线并明确选择订单", code="BASELINE_REQUIRED")
    reason = values.get("no_change_reason", "").strip()
    if values.get("acknowledge_no_change") != "yes" or not reason:
        raise AppError("请明确勾选无需变更并填写核对理由", code="REVIEW_INPUT_INVALID")
    return Proposal.model_validate(
        {
            "order_id": selected,
            "disposition": "no_change",
            "summary": "人工核对后提出无需订单变更；仍需确认处理清单。",
            "questions": [],
            "changes": [],
        }
    ), reason


@router.post("/ui/reviews/{review_id}/{action}", response_class=HTMLResponse)
async def ui_action(review_id: str, action: str, request: Request):
    values = await _form(request)
    sessions = request.app.state.session_factory
    try:
        revision = int(values["revision"])
        if action == "extract":
            body = ExtractRequest.model_validate(
                {
                    "revision": revision,
                    "request_key": values["request_key"],
                    "acknowledge_transmission": values.get("acknowledge_transmission") == "yes",
                }
            )
            await run_in_threadpool(
                service.extract, sessions, request.app.state.settings, review_id, body
            )
        elif action == "correct":
            item = await run_in_threadpool(service.view, sessions, review_id)
            proposal = _human_proposal(values, item)
            await run_in_threadpool(
                service.correct,
                sessions,
                review_id,
                Correction(revision=revision, proposal=proposal, reason=values["reason"]),
            )
        elif action == "no-change":
            item = await run_in_threadpool(service.view, sessions, review_id)
            proposal, reason = _human_no_change(values, item)
            await run_in_threadpool(
                service.correct,
                sessions,
                review_id,
                Correction(revision=revision, proposal=proposal, reason=reason),
            )
        elif action == "evidence":
            evidence = UpdateEvidence.model_validate(
                {
                    "revision": revision,
                    "instruction": values["instruction"],
                    "snapshot": json.loads(values["snapshot"]),
                }
            )
            await run_in_threadpool(service.update_evidence, sessions, review_id, evidence)
        elif action in {"confirm", "cancel", "recover"}:
            await run_in_threadpool(getattr(service, action), sessions, review_id, revision)
        else:
            raise AppError("未知操作", code="NOT_FOUND", http_status=404)
    except (ValidationError, ValueError, KeyError):
        return _page(
            request,
            review_id,
            error=AppError(
                "请检查数量、所选动作和确认项；输入已保留。快照须为有效 JSON。",
                code="REVIEW_INPUT_INVALID",
            ),
            submitted=values,
        )
    except AppError as exc:
        return _page(request, review_id, error=exc, submitted=values)
    return RedirectResponse(f"/ui/reviews/{review_id}", status_code=303)
