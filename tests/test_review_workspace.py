"""Offline synthetic review-only journeys, guarded model transport, races and recovery.

MockTransport is deliberately not evidence of real-model understanding.
"""

import csv
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from io import BytesIO, StringIO
from threading import Event

import httpx
import openpyxl
import pytest
from alembic import command
from app.adapters.budget import BudgetLedger
from app.adapters.live_model import LiveLLMClient
from app.adapters.providers.schema import ensure_supported_schema
from app.config import Settings
from app.db.base import Base
from app.db.models import ModelUsage, ReviewWorkspace
from app.db.schema import _migration_config, init_db
from app.errors import AppError
from app.main import create_app
from app.review import service
from app.review.contracts import (
    CreateReview,
    ExtractRequest,
    Proposal,
    Snapshot,
    UpdateEvidence,
    model_schema,
)
from app.review.rules import check_proposal
from app.review.sample import SAMPLE_INSTRUCTION, SAMPLE_SNAPSHOT
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, select


def input_body(**overrides):
    return {
        "request_key": "synthetic-one",
        "selected_order_id": "SYN-2401",
        "instruction": SAMPLE_INSTRUCTION,
        "snapshot": deepcopy(SAMPLE_SNAPSHOT),
        **overrides,
    }


def proposal(**overrides):
    return {
        "order_id": "SYN-2401",
        "disposition": "propose",
        "summary": "合成测试提案",
        "questions": [],
        "changes": [
            {
                "mode": "set_total",
                "sku_id": "00123-藏蓝-M",
                "to_sku": None,
                "quantity": 36,
                "unit": "件",
                "before_qty": 50,
                "after_qty": 36,
                "citations": [{"line": 1, "quote": "总数改为 36 件"}],
            }
        ],
        **overrides,
    }


def configured(database_url, **overrides):
    return Settings(
        _env_file=None,
        database_url=database_url,
        live_enabled=True,
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:9999/v1",
        llm_model="offline-mock-NOT-a-real-model",
        price_in_per_mtok="0.10",
        price_out_per_mtok="0.20",
        **overrides,
    )


def model(sessions, settings, handler):
    return LiveLLMClient(
        settings,
        BudgetLedger(
            sessions,
            cap_usd=settings.live_budget_usd,
            price_in_per_mtok=settings.price_in_per_mtok,
            price_out_per_mtok=settings.price_out_per_mtok,
        ),
        transport=httpx.MockTransport(handler),
    )


def response(payload=None):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            proposal() if payload is None else payload, ensure_ascii=False
                        )
                    },
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 120},
        },
    )


def extract(sessions, settings, review_id, client, *, key="call-one"):
    service.extract(
        sessions,
        settings,
        review_id,
        ExtractRequest(
            revision=service.view(sessions, review_id)["revision"],
            request_key=key,
            acknowledge_transmission=True,
        ),
        client=client,
    )


def all_business_rows(sessions):
    with sessions() as session:
        return {
            name: [tuple(row) for row in session.execute(table.select()).all()]
            for name, table in Base.metadata.tables.items()
            if name not in {"review_workspace", "model_usage"}
        }


def test_end_to_end_model_transport_correction_confirmation_and_real_export(
    session_factory, database_url, monkeypatch
):
    settings = configured(database_url)
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return response()

    client_model = model(session_factory, settings, handle)
    monkeypatch.setattr(service, "live_client", lambda *args: client_model)
    before = all_business_rows(session_factory)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        created = client.post("/reviews", json=input_body()).json()
        review_id = created["id"]
        assert client.get(f"/reviews/{review_id}/export").status_code == 409
        call = {"revision": 1, "request_key": "safe-key", "acknowledge_transmission": True}
        parsed = client.post(f"/reviews/{review_id}/extract", json=call).json()
        assert parsed["data"]["check"]["rows"][0]["delta"] == -14
        assert parsed["business_execution"] == "NOT_EXECUTED"
        assert client.post(f"/reviews/{review_id}/extract", json=call).json() == parsed
        assert len(seen) == 1
        decoded = json.loads(seen[0]["messages"][1]["content"])
        assert decoded["snapshot"]["catalog"][0]["style"] == "00123"
        assert "expected" not in decoded and "case_id" not in decoded
        confirm = {"revision": parsed["revision"]}
        assert client.post(f"/reviews/{review_id}/confirm", json=confirm).status_code == 200
        assert client.post(f"/reviews/{review_id}/confirm", json=confirm).status_code == 200
        csv_data = client.get(f"/reviews/{review_id}/export").content.decode("utf-8-sig")
        rows = list(csv.DictReader(StringIO(csv_data)))
        assert [(r["old_qty"], r["new_qty"], r["delta"]) for r in rows] == [("50", "36", "-14")]
        assert "未执行订单" in rows[0]["state"] and "INPUT:1 总数改为 36 件" in csv_data
        assert rows[0]["baseline_source"] == "snapshot:/orders/0/lines/0/ordered_qty"
        book = openpyxl.load_workbook(
            BytesIO(client.get(f"/reviews/{review_id}/export?fmt=xlsx").content), data_only=False
        )
        assert book.active.max_row == 2
        excel_rows = list(book.active.values)
        excel_row = dict(zip(excel_rows[0], excel_rows[1], strict=True))
        assert (excel_row["new_qty"], excel_row["delta"]) == (36, -14)
        assert excel_row["baseline_source"] == rows[0]["baseline_source"]
        book.close()
        assert client.get(f"/reviews/{review_id}/export?fmt=html").status_code == 400
        latest = client.get(f"/reviews/{review_id}").json()
        fixed = proposal()
        fixed["changes"][0].update(quantity=38, after_qty=38)
        corrected = client.post(
            f"/reviews/{review_id}/correct",
            json={"revision": latest["revision"], "proposal": fixed, "reason": "人工确认改为38"},
        ).json()
        assert corrected["status"] == "NEEDS_REVIEW" and corrected["data"]["human"]
        assert corrected["data"]["history"][0]["result"]["proposal"] == proposal()
        assert client.get(f"/reviews/{review_id}/export").status_code == 409
        assert client.post(f"/reviews/{review_id}/confirm", json=confirm).status_code == 409
    assert all_business_rows(session_factory) == before
    with session_factory() as session:
        usage = session.scalars(select(ModelUsage)).all()
        assert len(usage) == 1 and usage[0].agent_id == review_id and usage[0].status == "SETTLED"


@pytest.mark.parametrize(
    "mode,qty,after,delta",
    [
        ("set_total", 30, 30, -20),
        ("increment", 30, 80, 30),
        ("increment", -5, 45, -5),
        ("cancel_remaining", None, 10, -40),
    ],
)
def test_quantity_semantics(mode, qty, after, delta):
    snap = deepcopy(SAMPLE_SNAPSHOT)
    snap["inventory"][0]["available_qty"] = 35
    payload = proposal()
    payload["changes"][0].update(mode=mode, quantity=qty, after_qty=after)
    result = check_proposal(
        Snapshot.model_validate(snap),
        "SYN-2401",
        SAMPLE_INSTRUCTION,
        Proposal.model_validate(payload),
    )
    assert result["status"] == "ready" and result["rows"][0]["delta"] == delta


@pytest.mark.parametrize(
    "defect",
    [
        "shipped-unknown",
        "inventory-unknown",
        "inventory-missing",
        "too-low",
        "shortage",
        "wrong-order",
        "wrong-old",
        "wrong-new",
        "unknown-sku",
        "bad-unit",
        "missing-source",
        "fabricated-source",
        "out-of-bounds-source",
        "overlap",
        "unresolved-question",
        "cancel-quantity",
        "target-on-set",
        "shipped-over-ordered",
    ],
)
def test_unsafe_or_unproven_proposals_never_become_confirmable(defect):
    snap = deepcopy(SAMPLE_SNAPSHOT)
    payload = proposal()
    change = payload["changes"][0]
    if defect == "shipped-unknown":
        snap["orders"][0]["lines"][0].pop("shipped_qty")
    elif defect == "inventory-unknown":
        snap["inventory"][0]["available_qty"] = None
    elif defect == "inventory-missing":
        snap["inventory"] = []
    elif defect == "too-low":
        change.update(quantity=5, after_qty=5)
    elif defect == "shortage":
        change.update(quantity=99, after_qty=99)
    elif defect == "wrong-order":
        payload["order_id"] = "OTHER"
    elif defect == "wrong-old":
        change["before_qty"] = 51
    elif defect == "wrong-new":
        change["after_qty"] = 37
    elif defect == "unknown-sku":
        change["sku_id"] = "not-a-sku"
    elif defect == "bad-unit":
        change["unit"] = "包"
    elif defect == "missing-source":
        change["citations"] = []
    elif defect == "fabricated-source":
        change["citations"][0]["quote"] = "原文里不存在"
    elif defect == "out-of-bounds-source":
        change["citations"][0]["line"] = 2
    elif defect == "overlap":
        payload["changes"].append(deepcopy(change))
    elif defect == "unresolved-question":
        payload["questions"] = ["哪张订单？"]
    elif defect == "cancel-quantity":
        change.update(mode="cancel_remaining", after_qty=10)
    elif defect == "target-on-set":
        change["to_sku"] = "00123-白-M"
    elif defect == "shipped-over-ordered":
        snap["orders"][0]["lines"][0]["shipped_qty"] = 60
    result = check_proposal(
        Snapshot.model_validate(snap),
        "SYN-2401",
        SAMPLE_INSTRUCTION,
        Proposal.model_validate(payload),
    )
    assert result["status"] == "blocked" and result["issues"] and not result["rows"]


def test_mto_protects_cut_and_requires_current_documents():
    snap = deepcopy(SAMPLE_SNAPSHOT)
    snap["catalog"][0]["fulfillment_mode"] = "MTO"
    snap["orders"][0]["lines"][0]["cut_qty"] = 20
    payload = proposal()
    parsed = Proposal.model_validate(payload)
    assert (
        check_proposal(Snapshot.model_validate(snap), "SYN-2401", SAMPLE_INSTRUCTION, parsed)[
            "status"
        ]
        == "blocked"
    )
    snap["documents"] = [
        {
            "doc_id": kind,
            "kind": kind,
            "order_id": "SYN-2401",
            "order_revision": 1,
            "sku_id": "00123-藏蓝-M",
            "quantity": 50,
            "status": "ACTIVE",
        }
        for kind in ("MO", "PO")
    ]
    result = check_proposal(Snapshot.model_validate(snap), "SYN-2401", SAMPLE_INSTRUCTION, parsed)
    assert result["status"] == "ready" and "供应商未确认" in result["downstream"][-1]
    payload["changes"][0].update(quantity=15, after_qty=15)
    assert (
        check_proposal(
            Snapshot.model_validate(snap),
            "SYN-2401",
            SAMPLE_INSTRUCTION,
            Proposal.model_validate(payload),
        )["status"]
        == "blocked"
    )
    snap["documents"][0]["order_revision"] = 2
    assert (
        check_proposal(Snapshot.model_validate(snap), "SYN-2401", SAMPLE_INSTRUCTION, parsed)[
            "status"
        ]
        == "blocked"
    )


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate-sku",
        "duplicate-line",
        "unknown-sku",
        "negative",
        "float",
        "bool-quantity",
        "nul",
        "extra-secret-field",
    ],
)
def test_invalid_import_rejected(defect):
    raw = input_body()
    if defect == "duplicate-sku":
        raw["snapshot"]["catalog"] *= 2
    elif defect == "duplicate-line":
        raw["snapshot"]["orders"][0]["lines"] *= 2
    elif defect == "unknown-sku":
        raw["snapshot"]["orders"][0]["lines"][0]["sku_id"] = "ghost"
    elif defect in {"negative", "float", "bool-quantity"}:
        raw["snapshot"]["orders"][0]["lines"][0]["ordered_qty"] = {
            "negative": -1,
            "float": 1.5,
            "bool-quantity": True,
        }[defect]
    elif defect == "nul":
        raw["instruction"] = "secret\x00"
    elif defect == "extra-secret-field":
        raw["api_key"] = "must-not-be-accepted"
    with pytest.raises(ValidationError):
        CreateReview.model_validate(raw)


def test_missing_config_keeps_input_attempts_and_business_untouched(session_factory, database_url):
    review_id = service.create(session_factory, CreateReview.model_validate(input_body()))
    before = service.view(session_factory, review_id)
    for _ in range(4):
        with pytest.raises(AppError, match="不会转为演示答案"):
            service.extract(
                session_factory,
                Settings(_env_file=None),
                review_id,
                ExtractRequest(revision=1, request_key="no-config", acknowledge_transmission=True),
            )
    assert service.view(session_factory, review_id) == before
    assert service.create(session_factory, CreateReview.model_validate(input_body())) == review_id
    with pytest.raises(AppError) as conflict:
        service.create(
            session_factory, CreateReview.model_validate(input_body(instruction="不同内容"))
        )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(AppError):
        service.create(
            session_factory,
            CreateReview.model_validate(
                input_body(request_key="other", selected_order_id="不存在")
            ),
        )
    with pytest.raises(AppError):
        service.view(session_factory, "not-found")


@pytest.mark.parametrize("failure", ["timeout", "transport", "http429", "invalid-json", "schema"])
def test_model_failure_retains_input_blocks_export_and_recovers_without_duplicate_call(
    session_factory, database_url, failure
):
    settings = configured(database_url)
    review_id = service.create(session_factory, CreateReview.model_validate(input_body()))
    seen = []

    def fail(request):
        seen.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("never-reflect-upstream-canary")
        if failure == "transport":
            raise httpx.ConnectError("never-reflect-upstream-canary")
        if failure == "http429":
            return httpx.Response(429, json={"secret": "canary"})
        if failure == "invalid-json":
            return httpx.Response(200, text="not JSON canary")
        return response({"order_id": "SYN-2401", "extra": "not allowed"})

    client_model = model(session_factory, settings, fail)
    extract(session_factory, settings, review_id, client_model)
    failed = service.view(session_factory, review_id)
    assert failed["status"] == "ERROR" and failed["data"]["proposal"] is None
    assert failed["data"]["input"]["instruction"] == SAMPLE_INSTRUCTION
    assert "canary" not in json.dumps(failed)
    extract(session_factory, settings, review_id, client_model)
    assert len(seen) == 1
    with pytest.raises(AppError):
        service.confirm(session_factory, review_id, failed["revision"])
    extract(
        session_factory,
        settings,
        review_id,
        model(session_factory, settings, lambda _: response()),
        key="explicit-new-call",
    )
    assert service.view(session_factory, review_id)["data"]["check"]["status"] == "ready"


@pytest.mark.parametrize("winner", ["cancel", "update", "recovery"])
def test_late_model_cannot_overwrite_cancel_new_evidence_or_recovery(
    session_factory, database_url, monkeypatch, winner
):
    settings = configured(database_url)
    review_id = service.create(session_factory, CreateReview.model_validate(input_body()))
    entered, release = Event(), Event()

    def wait(request):
        entered.set()
        assert release.wait(10)
        return response()

    client_model = model(session_factory, settings, wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(extract, session_factory, settings, review_id, client_model)
        assert entered.wait(10)
        running = service.view(session_factory, review_id)
        with pytest.raises(AppError):
            service.recover(session_factory, review_id, running["revision"])
        if winner == "cancel":
            service.cancel(session_factory, review_id, running["revision"])
        elif winner == "update":
            service.update_evidence(
                session_factory,
                review_id,
                UpdateEvidence(
                    revision=running["revision"],
                    instruction="这份仅询价，不改单",
                    snapshot=SAMPLE_SNAPSHOT,
                ),
            )
        else:
            later = service._now() + timedelta(hours=1)
            monkeypatch.setattr(service, "_now", lambda: later)
            service.recover(session_factory, review_id, running["revision"])
        winner_state = service.view(session_factory, review_id)
        release.set()
        future.result(timeout=10)
    assert service.view(session_factory, review_id) == winner_state
    assert winner_state["data"]["proposal"] is None


def test_same_key_concurrent_extract_sends_once_and_other_key_refused(
    session_factory, database_url
):
    settings = configured(database_url)
    review_id = service.create(session_factory, CreateReview.model_validate(input_body()))
    entered, release = Event(), Event()
    sent = []

    def wait(request):
        sent.append(request)
        entered.set()
        assert release.wait(10)
        return response()

    client_model = model(session_factory, settings, wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(extract, session_factory, settings, review_id, client_model)
        assert entered.wait(10)
        extract(session_factory, settings, review_id, client_model)
        with pytest.raises(AppError):
            extract(session_factory, settings, review_id, client_model, key="other-call")
        release.set()
        future.result(timeout=10)
    assert len(sent) == 1


def test_database_failure_rolls_back_creation_and_original_key_recovers(session_factory, engine):
    def fail(*args):
        if args[2].lstrip().startswith("INSERT INTO review_workspace"):
            raise RuntimeError("synthetic-write-fault")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            service.create(session_factory, CreateReview.model_validate(input_body()))
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with session_factory() as session:
        assert session.scalars(select(ReviewWorkspace)).all() == []
    assert service.create(session_factory, CreateReview.model_validate(input_body())).startswith(
        "RV-"
    )


def test_explicit_clarification_no_change_and_forged_executable_clarification():
    snap = Snapshot.model_validate(SAMPLE_SNAPSHOT)
    for disposition in ("clarify", "no_change"):
        payload = proposal(
            disposition=disposition,
            changes=[],
            questions=["请确认数量"] if disposition == "clarify" else [],
        )
        assert (
            check_proposal(snap, "SYN-2401", SAMPLE_INSTRUCTION, Proposal.model_validate(payload))[
                "status"
            ]
            == disposition
        )
    assert (
        check_proposal(
            snap,
            "SYN-2401",
            SAMPLE_INSTRUCTION,
            Proposal.model_validate(proposal(disposition="clarify")),
        )["status"]
        == "blocked"
    )


def test_schema_transport_subset_is_supported():
    ensure_supported_schema(model_schema())


def test_workspace_migration_empty_downgrade_and_nonempty_refusal_are_lossless(
    session_factory, engine
):
    with engine.connect() as connection:
        cfg = _migration_config()
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, "execution_verification_guard")
    init_db(engine)
    review_id = service.create(session_factory, CreateReview.model_validate(input_body()))
    before = service.view(session_factory, review_id)
    with engine.connect() as connection:
        cfg = _migration_config()
        cfg.attributes["connection"] = connection
        with pytest.raises(RuntimeError, match="清单工作区非空"):
            command.downgrade(cfg, "execution_verification_guard")
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == (
            "local_review_workspace"
        )
    assert service.view(session_factory, review_id) == before


def test_call_cap_and_fixture_injection_are_rejected_before_transport(
    session_factory, database_url
):
    settings = configured(database_url, model_max_calls_per_task=1)
    review_id = service.create(session_factory, CreateReview.model_validate(input_body()))
    sent = []

    def fail(request):
        sent.append(request)
        return response({"invalid": "schema"})

    client = model(session_factory, settings, fail)
    extract(session_factory, settings, review_id, client)
    with pytest.raises(AppError) as cap:
        extract(session_factory, settings, review_id, client, key="new-after-failure")
    assert cap.value.code == "CALL_LIMIT" and len(sent) == 1

    class ForbiddenFixture:
        mode = "FIXTURE"

    with pytest.raises(AppError, match="不能使用 FIXTURE"):
        extract(session_factory, settings, review_id, ForbiddenFixture(), key="fixture")


def test_round_call_cap_is_atomic_across_two_ledgers(session_factory):
    def reserve_once():
        ledger = BudgetLedger(
            session_factory, cap_usd=1, price_in_per_mtok=1, price_out_per_mtok=1, max_total_calls=1
        )
        try:
            return ledger.reserve(
                provider="offline", model="offline-mock", input_tokens=10, max_output_tokens=10
            )
        except AppError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: reserve_once(), range(2)))
    assert sum(outcome.startswith("MU-") for outcome in outcomes) == 1
    assert outcomes.count("CALL_LIMIT") == 1
    with pytest.raises(ValueError):
        BudgetLedger(
            session_factory, cap_usd=1, price_in_per_mtok=1, price_out_per_mtok=1, max_total_calls=0
        )


def test_oversized_prompt_or_output_limit_never_spends_attempt_or_transmits(
    session_factory, database_url
):
    review_id = service.create(
        session_factory, CreateReview.model_validate(input_body(instruction="合成超长资料" * 2400))
    )
    before = service.view(session_factory, review_id)
    sent = []
    settings = configured(database_url)
    client = model(session_factory, settings, lambda req: sent.append(req) or response())
    with pytest.raises(AppError) as oversized:
        extract(session_factory, settings, review_id, client)
    assert oversized.value.code == "REVIEW_MODEL_LIMIT" and not sent
    assert service.view(session_factory, review_id) == before
    other = service.create(
        session_factory, CreateReview.model_validate(input_body(request_key="two"))
    )
    settings = configured(database_url, model_max_tokens=3001)
    with pytest.raises(AppError) as bad_output:
        extract(session_factory, settings, other, client)
    assert bad_output.value.code == "REVIEW_MODEL_LIMIT" and not sent


def test_result_write_failure_keeps_running_until_explicit_recovery_and_new_call(
    session_factory, database_url, engine, monkeypatch
):
    settings = configured(database_url)
    review_id = service.create(session_factory, CreateReview.model_validate(input_body()))
    writes = 0

    def fail_result(connection, cursor, statement, parameters, context, executemany):
        nonlocal writes
        if statement.startswith("UPDATE review_workspace"):
            writes += 1
            if writes == 2:
                raise RuntimeError("synthetic-result-write-fault")

    client = model(session_factory, settings, lambda _: response())
    event.listen(engine, "before_cursor_execute", fail_result)
    try:
        with pytest.raises(RuntimeError):
            extract(session_factory, settings, review_id, client)
    finally:
        event.remove(engine, "before_cursor_execute", fail_result)
    running = service.view(session_factory, review_id)
    assert running["status"] == "RUNNING" and running["data"]["proposal"] is None
    later = service._now() + timedelta(minutes=5)
    monkeypatch.setattr(service, "_now", lambda: later)
    service.recover(session_factory, review_id, running["revision"])
    extract(session_factory, settings, review_id, client, key="explicit-after-recovery")
    assert service.view(session_factory, review_id)["data"]["check"]["status"] == "ready"


def test_html_form_preserves_bad_input_and_manual_flow_survives_app_restart(
    session_factory, database_url
):
    settings = Settings(_env_file=None, database_url=database_url)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        page = client.get("/ui/reviews/new")
        assert page.status_code == 200 and "尚不可用" in page.text
        invalid = client.post(
            "/ui/reviews/new",
            data={
                "request_key": "html-one",
                "selected_order_id": "SYN-2401",
                "instruction": "保留合成输入",
                "snapshot": "invalid-json",
            },
        )
        assert invalid.status_code == 400 and "保留合成输入" in invalid.text
        created = client.post(
            "/ui/reviews/new",
            data={
                "request_key": "html-one",
                "selected_order_id": "SYN-2401",
                "instruction": SAMPLE_INSTRUCTION,
                "snapshot": json.dumps(SAMPLE_SNAPSHOT),
            },
        )
        review_id = str(created.url).split("/")[-1]
        assert "资料已保存" in created.text
        bad = client.post(
            f"/ui/reviews/{review_id}/correct",
            data={
                "revision": 1,
                "sku_id": "00123-藏蓝-M",
                "mode": "set_total",
                "quantity": "x",
                "reason": "保留原因",
            },
        )
        assert bad.status_code == 400 and "保留原因" in bad.text
        saved = client.post(
            f"/ui/reviews/{review_id}/correct",
            data={
                "revision": 1,
                "sku_id": "00123-藏蓝-M",
                "mode": "set_total",
                "quantity": "36",
                "reason": "人工核对",
            },
        )
        assert "可确认处理清单" in saved.text
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        item = client.get(f"/reviews/{review_id}").json()
        confirmed = client.post(
            f"/ui/reviews/{review_id}/confirm", data={"revision": item["revision"]}
        )
        assert "下载 CSV 清单" in confirmed.text and "已确认处理清单；未执行订单" in confirmed.text
        latest = client.get(f"/reviews/{review_id}").json()
        changed = client.post(
            f"/ui/reviews/{review_id}/evidence",
            data={
                "revision": latest["revision"],
                "instruction": "现在再加2件，需要重新核对",
                "snapshot": json.dumps(SAMPLE_SNAPSHOT),
            },
        )
        assert "资料已保存" in changed.text and "下载 CSV 清单" not in changed.text
        assert "SYN-2401" in client.get("/ui/reviews").text
        new = client.get(f"/reviews/{review_id}").json()
        cancelled = client.post(
            f"/ui/reviews/{review_id}/cancel", data={"revision": new["revision"]}
        )
        assert (
            "已取消" in cancelled.text
            and "调用真实模型提取（不执行订单）</button>" not in cancelled.text
        )
