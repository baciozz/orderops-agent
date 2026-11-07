"""Real receipt/upload/worker pipeline for the explicit no-model sheet command."""

from io import BytesIO
from types import SimpleNamespace

import openpyxl
import pytest
from app.api.inputs import answer, get_task, submit
from app.api.orders import approve, commit
from app.api.sources import upload_source
from app.config import Settings
from app.db.models import InputPayload
from app.errors import AppError
from app.orchestration.worker import run_once
from app.services.kernel import approve_plan, plan_change
from app.services.seed import load_seed
from schemas.api import AnswerSubmitRequest, ApproveRequest, CommitRequest, InputSubmitRequest
from starlette.requests import Request
from tests.test_durable_new_order import business, snapshot

ACTIVATE = "新建现货订单（表格）"
INITIAL = "按附件新建订单"


@pytest.fixture(scope="session", autouse=True)
def _sandbox_asyncio_polling():
    # These handlers do not suspend; no event loop, sockets or IPC fallback.
    yield


def immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError("Unexpected asynchronous suspension")


@pytest.fixture()
def sheet_env(engine, session_factory, database_url, tmp_path, monkeypatch):
    load_seed(session_factory, database_url=database_url)
    settings = Settings(_env_file=None, database_url=database_url, live_enabled=False)
    root = tmp_path / "uploads"
    root.mkdir(mode=0o700)
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=session_factory, settings=settings, source_storage_dir=root
        )
    )
    request = Request({"type": "http", "app": app})
    request.state.request_id = request.state.correlation_id = "bounded-test"

    def forbidden(*args, **kwargs):
        raise AssertionError("Bounded workflow must not construct model, ledger or fixtures")

    for name in ("LiveLLMClient", "FixtureLLMClient", "BudgetLedger", "golden_fixtures"):
        monkeypatch.setattr(f"app.orchestration.dispatch.{name}", forbidden)
    # Keep a distinct approved business task throughout all precommit boundaries.
    prior = plan_change(
        session_factory,
        order_id="SO-1001",
        base_revision=1,
        intent_revision=1,
        changes=[{"mode": "increment", "sku_id": "A-WHITE-M", "qty": 1}],
        source_refs=[],
        client_event_key="unrelated-approved",
    )
    approve_plan(
        session_factory,
        plan_id=prior.plan_id,
        plan_hash=prior.plan_hash,
        expected_intent_revision=prior.intent_revision,
    )
    return request, settings


def book(rows=None, mutate=lambda wb: None):
    wb = openpyxl.Workbook()
    wb.active.title = "订单"
    wb.active.append(["客户", "款", "颜色", "尺码", "数量"])
    for row in (
        rows
        if rows is not None
        else [["华东服饰", "A", "白", "M", 30], ["华东服饰", "B", "蓝", "L", 25]]
    ):
        wb.active.append(row)
    mutate(wb)
    stream = BytesIO()
    wb.save(stream)
    wb.close()
    return stream.getvalue()


def upload(request, task_id, content=None, key="original"):
    content = book() if content is None else content
    boundary = "bounded-upload"
    chunks = []
    for name, value in (("task_id", task_id), ("client_upload_key", key)):
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            ).encode()
        )
    chunks += [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="order.xlsx"\r\nContent-Type: application/vnd.openxmlformats-'
            "officedocument.spreadsheetml.sheet\r\n\r\n"
        ).encode(),
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ]

    async def receive():
        return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

    req = Request(
        {
            "type": "http",
            "app": request.app,
            "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
        },
        receive,
    )
    req.state.request_id = req.state.correlation_id = "bounded-upload"
    return immediate(upload_source(req))


def start(request, factory, settings):
    body = InputSubmitRequest(thread_id="sheet", client_event_key="activate", text=ACTIVATE)
    receipt = submit(body, request)
    outcome = run_once(factory, settings=settings)
    assert outcome.status == "NEEDS_CLARIFICATION" and outcome.plan_id is None
    return receipt


def respond(request, task_id, text, sources=None, key="answer"):
    return answer(
        task_id,
        AnswerSubmitRequest(
            thread_id="sheet", client_event_key=key, text=text, source_ids=sources or []
        ),
        request,
    )


def shortage(request, factory, settings, *, content=None, extra=None):
    origin = start(request, factory, settings)
    sources = [upload(request, origin.task_id, content).source_id]
    if extra is not None:
        sources.append(upload(request, origin.task_id, extra, "second").source_id)
    initial = respond(request, origin.task_id, INITIAL, sources, "initial")
    out = run_once(factory, settings=settings)
    assert out.status == "NEEDS_CLARIFICATION" and out.plan_id
    return origin, sources, initial, out


def approve_current(request, outcome):
    return approve(
        outcome.plan_id,
        ApproveRequest(
            plan_hash=outcome.plan_hash, expected_intent_revision=outcome.intent_revision
        ),
        request,
    )


def test_original_durable_shortage_correction_approval_commit_replay(
    sheet_env, engine, session_factory
):
    request, settings = sheet_env
    before = snapshot(engine)
    original_bytes = book()
    original, sources, initial, rejected = shortage(
        request, session_factory, settings, content=original_bytes
    )
    after_shortage = snapshot(engine)
    assert business(after_shortage) == business(before)
    assert after_shortage["approval"] == before["approval"]
    proposals = after_shortage["purchase_proposal"]
    assert len(proposals) == 1 and proposals[0]["shortage_qty"] == 5
    assert get_task(original.task_id, request)["questions"]
    answer_receipt = respond(request, original.task_id, "蓝 L 改成 20")
    assert get_task(original.task_id, request)["questions"] == []
    out = run_once(session_factory, settings=settings)
    assert out.status == "AWAITING_APPROVAL" and out.plan_id != rejected.plan_id
    planned = snapshot(engine)
    assert business(planned) == business(before)
    assert planned["purchase_proposal"] == proposals
    assert planned["source_span"] == after_shortage["source_span"]
    assert planned["model_usage"] == []
    assert sorted(
        r["attempt_no"] for r in planned["job_attempt"] if r["task_id"] == original.task_id
    ) == [1, 2, 3]
    assert all(r["delivery_state"] == "DELIVERED" for r in planned["inbox_delivery"])
    with session_factory() as session:
        raw = session.get(InputPayload, answer_receipt.event_id).body
        assert raw["source_ids"] == []
        assert raw["sheet_order_v1"]
        extracted = raw["extraction"]
        assert sorted((r["sku_id"], r["qty"]) for r in extracted["new_lines"]) == [
            ("A-WHITE-M", 30),
            ("B-BLUE-L", 20),
        ]
        refs = extracted["source_refs"]
        assert any(
            r["source_id"] == "SRC-INPUT"
            and r["message_index"] == 3
            and r["field"] == "quantity"
            and r["text_excerpt"] == "蓝 L 改成 20"
            for r in refs
        )
    assert upload(request, original.task_id, original_bytes).source_id == sources[0]
    assert (
        respond(request, original.task_id, INITIAL, sources, "initial").event_id == initial.event_id
    )
    assert respond(request, original.task_id, "蓝 L 改成 20").event_id == answer_receipt.event_id
    assert snapshot(engine) == planned
    with pytest.raises(AppError):
        commit(
            out.plan_id,
            CommitRequest(approval_id="missing", plan_hash=out.plan_hash, idempotency_key="commit"),
            request,
        )
    approval = approve_current(request, out)
    assert business(snapshot(engine)) == business(before)
    body = CommitRequest(
        approval_id=approval.approval_id, plan_hash=out.plan_hash, idempotency_key="commit"
    )
    result = commit(out.plan_id, body, request)
    committed = snapshot(engine)
    assert sorted(
        (r["sku_id"], r["qty"])
        for r in committed["reservation"]
        if r["order_id"] == result.order_id
    ) == [("A-WHITE-M", 30), ("B-BLUE-L", 20)]
    assert len(committed["operation"]) == 1
    assert committed["purchase_proposal"] == proposals
    assert committed["source_span"] == after_shortage["source_span"]
    assert [r for r in committed["approval"] if r["plan_id"] != out.plan_id] == before["approval"]
    assert commit(out.plan_id, body, request) == result
    assert run_once(session_factory, settings=settings) is None
    assert snapshot(engine) == committed


@pytest.mark.parametrize(
    "text",
    [
        "不要蓝 L 改成 20",
        "蓝 L 不改成 20",
        "蓝 L 改成 20 再加白 M 5",
        "蓝 L 改成20",
        "蓝 L 改成 20件",
        "蓝 L 改成 020",
        "蓝 l 改成 20",
        "蓝\tL 改成 20",
        "蓝 L 改成 20\n",
        "蓝 L 改成 20。",
        "蓝 L 改成 ２０",
        "蓝 L 改成 0",
        "蓝 L 改成 9223372036854775808",
        "白 XL 改成 2",
        "按附件新建订单",
    ],
)
def test_unsupported_third_answer_does_not_apply_partial_or_recover_budget(
    sheet_env, engine, session_factory, text
):
    request, settings = sheet_env
    original, _, _, _ = shortage(request, session_factory, settings)
    before = snapshot(engine)
    receipt = respond(request, original.task_id, text)
    outcome = run_once(session_factory, settings=settings)
    assert outcome.status == "NEEDS_CLARIFICATION" and outcome.plan_id is None
    state = snapshot(engine)
    assert business(state) == business(before)
    assert state["purchase_proposal"] == before["purchase_proposal"]
    assert [{k: v for k, v in r.items() if k != "status"} for r in state["plan"]] == [
        {k: v for k, v in r.items() if k != "status"} for r in before["plan"]
    ]
    assert respond(request, original.task_id, text).event_id == receipt.event_id
    assert run_once(session_factory, settings=settings) is None
    respond(request, original.task_id, "蓝 L 改成 20", key="fourth")
    assert run_once(session_factory, settings=settings) is None
    final = snapshot(engine)
    assert business(final) == business(before)
    assert final["model_usage"] == []
    assert final["plan"] == state["plan"]
    task = next(r for r in final["task"] if r["id"] == original.task_id)
    assert task["status"] == "FAILED" and task["error_code"] == "ATTEMPT_LIMIT"


@pytest.mark.parametrize("quantity", [1, 7, 19, 20])
@pytest.mark.parametrize("sources_mode", ["inherit", "explicit"])
def test_variable_quantities_multisource_preserve_full_rows(
    sheet_env, engine, session_factory, quantity, sources_mode
):
    request, settings = sheet_env
    original, sources, _, _ = shortage(
        request,
        session_factory,
        settings,
        content=book([["华东服饰", "A", "白", "M", 30]]),
        extra=book([["华东服饰", "B", "蓝", "L", 25]]),
    )
    before = snapshot(engine)
    selected = list(reversed(sources)) if sources_mode == "explicit" else []
    respond(request, original.task_id, f"  蓝  L 改成 {quantity}  ", selected)
    outcome = run_once(session_factory, settings=settings)
    assert outcome.status == "AWAITING_APPROVAL"
    extraction = get_task(original.task_id, request)["extraction"]
    assert sorted((r["sku_id"], r["qty"]) for r in extraction["new_lines"]) == [
        ("A-WHITE-M", 30),
        ("B-BLUE-L", quantity),
    ]
    assert business(snapshot(engine)) == business(before)
    assert snapshot(engine)["source_span"] == before["source_span"]


@pytest.mark.parametrize("change", ["add", "drop", "replace", "upload_only"])
def test_source_set_is_inherited_not_replaced(sheet_env, engine, session_factory, change):
    request, settings = sheet_env
    original, sources, _, _ = shortage(
        request,
        session_factory,
        settings,
        content=book([["华东服饰", "A", "白", "M", 30]]),
        extra=book([["华东服饰", "B", "蓝", "L", 25]]),
    )
    third = upload(request, original.task_id, book(), "third").source_id
    before = snapshot(engine)
    selection = {
        "add": sources + [third],
        "drop": sources[:1],
        "replace": [third],
        "upload_only": [],
    }[change]
    respond(request, original.task_id, "蓝 L 改成 20", selection)
    out = run_once(session_factory, settings=settings)
    assert out.status == ("AWAITING_APPROVAL" if change == "upload_only" else "NEEDS_CLARIFICATION")
    assert business(snapshot(engine)) == business(before)
    assert snapshot(engine)["purchase_proposal"] == before["purchase_proposal"]


@pytest.mark.parametrize(
    "quantity", ["+20", "20.0", "2e1", "２０", True, 0, -1, 1.5, "20件", "9223372036854775808"]
)
def test_invalid_persisted_quantity_never_builds_plan(sheet_env, engine, session_factory, quantity):
    request, settings = sheet_env
    origin = start(request, session_factory, settings)
    source = upload(
        request, origin.task_id, book([["华东服饰", "B", "蓝", "L", quantity]])
    ).source_id
    before = snapshot(engine)
    respond(request, origin.task_id, INITIAL, [source], "initial")
    out = run_once(session_factory, settings=settings)
    assert out.status == "NEEDS_CLARIFICATION" and out.plan_id is None
    assert business(snapshot(engine)) == business(before)


@pytest.mark.parametrize(
    "layout",
    [
        "wrong_sheet",
        "extra_sheet",
        "extra_column",
        "header",
        "partial",
        "duplicate",
        "merge",
        "empty",
        "mixed_customer",
        "unknown_sku",
    ],
)
def test_layout_rejected_without_guess_or_fill(sheet_env, engine, session_factory, layout):
    request, settings = sheet_env
    origin = start(request, session_factory, settings)

    def mutate(wb):
        ws = wb.active
        if layout == "wrong_sheet":
            ws.title = "Orders"
        elif layout == "extra_sheet":
            wb.create_sheet()["A1"] = "other business"
        elif layout == "extra_column":
            ws["F2"] = "extra"
        elif layout == "header":
            ws["A1"] = "客户名称"
        elif layout == "partial":
            ws["A3"] = None
        elif layout == "duplicate":
            ws.append(["华东服饰", "B", "蓝", "L", 25])
        elif layout == "merge":
            ws.merge_cells("A2:A3")
        elif layout == "empty":
            ws.delete_rows(2, 2)
        elif layout == "mixed_customer":
            ws["A3"] = "unknown"
        elif layout == "unknown_sku":
            ws["B3"] = "UNKNOWN"

    source = upload(request, origin.task_id, book(mutate=mutate)).source_id
    before = snapshot(engine)
    respond(request, origin.task_id, INITIAL, [source], "initial")
    out = run_once(session_factory, settings=settings)
    assert out.status == "NEEDS_CLARIFICATION" and out.plan_id is None
    assert business(snapshot(engine)) == business(before)


def test_fourth_receipt_invalidates_approved_third_plan_before_attempt_limit(
    sheet_env, engine, session_factory
):
    from app.db.models import Approval, Plan

    request, settings = sheet_env
    original, _, _, _ = shortage(request, session_factory, settings)
    respond(request, original.task_id, "蓝 L 改成 20")
    out = run_once(session_factory, settings=settings)
    approval = approve_current(request, out)
    before = snapshot(engine)
    respond(request, original.task_id, "蓝 L 改成 19", key="fourth")
    with session_factory() as s:
        assert s.get(Plan, out.plan_id).status == "INVALIDATED"
        assert s.get(Approval, approval.approval_id).status == "REVOKED"
    assert run_once(session_factory, settings=settings) is None
    with pytest.raises(AppError):
        approve_current(request, out)
    with pytest.raises(AppError):
        commit(
            out.plan_id,
            CommitRequest(
                approval_id=approval.approval_id, plan_hash=out.plan_hash, idempotency_key="stale"
            ),
            request,
        )
    assert business(snapshot(engine)) == business(before)


def test_no_model_mode_resolution_or_client_factory_even_when_live_enabled(
    sheet_env, session_factory, monkeypatch
):
    request, settings = sheet_env
    configured = settings.model_copy(update={"live_enabled": True})

    def forbidden(*args, **kwargs):
        raise AssertionError("No model configuration/provider may be inspected")

    monkeypatch.setattr("app.orchestration.dispatch.resolve_mode", forbidden)
    receipt = submit(
        InputSubmitRequest(thread_id="sheet", client_event_key="activate", text=ACTIVATE), request
    )
    out = run_once(session_factory, settings=configured, client_factory=forbidden)
    assert out.task_id == receipt.task_id and out.status == "NEEDS_CLARIFICATION"


def test_fresh_inventory_recheck_and_postapproval_revision_change(
    sheet_env, engine, session_factory
):
    from app.db.models import StockBalance
    from app.db.session import begin_immediate

    request, settings = sheet_env
    original, _, _, _ = shortage(request, session_factory, settings)
    with session_factory() as s, begin_immediate(s):
        stock = s.get(StockBalance, ("W1", "B-BLUE-L"))
        stock.on_hand -= 1
        stock.revision += 1
    respond(request, original.task_id, "蓝 L 改成 19")
    out = run_once(session_factory, settings=settings)
    assert out.status == "AWAITING_APPROVAL"
    approval = approve_current(request, out)
    with session_factory() as s, begin_immediate(s):
        stock = s.get(StockBalance, ("W1", "B-BLUE-L"))
        stock.on_hand -= 1
        stock.revision += 1
    before = snapshot(engine)
    with pytest.raises(AppError):
        commit(
            out.plan_id,
            CommitRequest(
                approval_id=approval.approval_id,
                plan_hash=out.plan_hash,
                idempotency_key="changed-stock",
            ),
            request,
        )
    assert business(snapshot(engine)) == business(before)


def test_worker_lost_lease_cannot_publish_correction_context(sheet_env, engine, session_factory):
    from app.db.models import InputPayload, JobAttempt
    from app.db.session import begin_immediate
    from sqlalchemy import select

    request, settings = sheet_env
    original, _, _, _ = shortage(request, session_factory, settings)
    received = respond(request, original.task_id, "蓝 L 改成 20")
    before = snapshot(engine)

    def lose_lease(ctx):
        if ctx.spec.agent_id == "parser":
            with session_factory() as s, begin_immediate(s):
                attempt = s.scalar(
                    select(JobAttempt)
                    .where(JobAttempt.task_id == original.task_id)
                    .order_by(JobAttempt.attempt_no.desc())
                )
                attempt.fencing_token += 1

    with pytest.raises(AppError):
        run_once(session_factory, settings=settings, before_step=lose_lease)
    with session_factory() as s:
        assert "sheet_order_v1" not in s.get(InputPayload, received.event_id).body
    assert business(snapshot(engine)) == business(before)
    assert snapshot(engine)["plan"] == before["plan"]


@pytest.mark.parametrize("scope", ["selected", "bound", "later_activation", "forged_marker"])
def test_unproven_activation_fails_closed_without_provider(
    sheet_env, engine, session_factory, scope
):
    from app.db.models import InputPayload, Task
    from app.db.session import begin_immediate

    request, settings = sheet_env
    body = InputSubmitRequest(
        thread_id="sheet",
        client_event_key="activate",
        text=ACTIVATE if scope in {"selected", "bound"} else "ordinary",
        selected_order_id="SO-1001" if scope == "selected" else None,
    )
    received = submit(body, request)
    with session_factory() as s, begin_immediate(s):
        task = s.get(Task, received.task_id)
        if scope == "bound":
            task.order_id = "SO-1001"
        elif scope == "forged_marker":
            raw = s.get(InputPayload, received.event_id)
            raw.body = {**raw.body, "sheet_order_v1": {"version": 1}}
        elif scope == "later_activation":
            task.status = "NEEDS_CLARIFICATION"
    if scope == "later_activation":
        respond(request, received.task_id, ACTIVATE)
    before = snapshot(engine)
    out = run_once(session_factory, settings=settings)
    assert out.status == "NEEDS_CLARIFICATION" and out.plan_id is None
    assert business(snapshot(engine)) == business(before)


def test_missing_explicit_initial_sources_and_foreign_source_fail_closed(
    sheet_env, engine, session_factory
):
    request, settings = sheet_env
    original = start(request, session_factory, settings)
    foreign = submit(
        InputSubmitRequest(thread_id="other", client_event_key="other", text=ACTIVATE), request
    )
    source = upload(request, foreign.task_id).source_id
    before = snapshot(engine)
    respond(request, original.task_id, INITIAL, [source], "initial")
    # Claim ordering is task ID, so drain both pending tasks.
    first = run_once(session_factory, settings=settings)
    second = run_once(session_factory, settings=settings)
    assert all(
        out.status == "NEEDS_CLARIFICATION" and out.plan_id is None for out in (first, second)
    )
    assert business(snapshot(engine)) == business(before)


def test_full_rowset_ambiguity_not_only_shortage_subset(sheet_env, engine, session_factory):
    from app.db.models import Sku, StockBalance
    from app.db.session import begin_immediate

    request, settings = sheet_env
    with session_factory() as s, begin_immediate(s):
        s.add(
            Sku(
                id="D-BLUE-L",
                style="D",
                color="BLUE",
                size="L",
                active=True,
                default_fulfillment_mode="STOCK",
            )
        )
        s.flush()
        s.add(
            StockBalance(warehouse_id="W1", sku_id="D-BLUE-L", on_hand=100, reserved=0, revision=1)
        )
    original, _, _, _ = shortage(
        request,
        session_factory,
        settings,
        content=book(
            [
                ["华东服饰", "A", "白", "M", 30],
                ["华东服饰", "B", "蓝", "L", 25],
                ["华东服饰", "D", "蓝", "L", 10],
            ]
        ),
    )
    before = snapshot(engine)
    respond(request, original.task_id, "蓝 L 改成 20")
    out = run_once(session_factory, settings=settings)
    assert out.status == "NEEDS_CLARIFICATION" and out.plan_id is None
    assert business(snapshot(engine)) == business(before)


def test_crash_after_accepted_extraction_replays_owned_context_with_remaining_budget(
    sheet_env, engine, session_factory
):
    from datetime import UTC, datetime, timedelta

    from app.db.models import InputPayload
    from app.orchestration.watchdog import scan

    request, settings = sheet_env
    original = start(request, session_factory, settings)
    source = upload(
        request,
        original.task_id,
        book([["华东服饰", "A", "白", "M", 30], ["华东服饰", "B", "蓝", "L", 20]]),
    ).source_id
    received = respond(request, original.task_id, INITIAL, [source], "initial")
    before = snapshot(engine)
    now = datetime.now(UTC)

    def crash(ctx):
        if ctx.spec.agent_id == "inventory-check":
            raise RuntimeError("crash after fenced context acceptance")

    with pytest.raises(RuntimeError):
        run_once(session_factory, settings=settings, clock=lambda: now, before_step=crash)
    with session_factory() as s:
        persisted = s.get(InputPayload, received.event_id).body["sheet_order_v1"]
    assert business(snapshot(engine)) == business(before)
    assert snapshot(engine)["plan"] == before["plan"]
    later = now + timedelta(seconds=61)
    scan(session_factory, clock=lambda: later, settings=settings)
    out = run_once(session_factory, settings=settings, clock=lambda: later)
    assert out.status == "AWAITING_APPROVAL" and out.intent_revision == 2
    with session_factory() as s:
        assert s.get(InputPayload, received.event_id).body["sheet_order_v1"] == persisted
    assert business(snapshot(engine)) == business(before)


def test_shortage_transaction_crash_has_no_half_plan_or_proposal(
    sheet_env, engine, session_factory
):
    from app.db.models import InputPayload, PurchaseProposal
    from sqlalchemy import event

    request, settings = sheet_env
    original = start(request, session_factory, settings)
    source = upload(request, original.task_id).source_id
    received = respond(request, original.task_id, INITIAL, [source], "initial")
    before = snapshot(engine)

    def crash(*args):
        raise RuntimeError("synthetic proposal write failure")

    event.listen(PurchaseProposal, "before_insert", crash)
    try:
        with pytest.raises(RuntimeError):
            run_once(session_factory, settings=settings)
    finally:
        event.remove(PurchaseProposal, "before_insert", crash)
    after = snapshot(engine)
    assert business(after) == business(before)
    assert after["plan"] == before["plan"]
    assert after["purchase_proposal"] == before["purchase_proposal"]
    with session_factory() as s:
        record = s.get(InputPayload, received.event_id).body["sheet_order_v1"]
        assert "shortage" not in record


def test_approval_answer_race_never_leaves_old_plan_executable(sheet_env, engine, session_factory):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.db.models import Approval, Plan
    from sqlalchemy import select

    request, settings = sheet_env
    original, _, _, _ = shortage(request, session_factory, settings)
    respond(request, original.task_id, "蓝 L 改成 20")
    out = run_once(session_factory, settings=settings)
    before = snapshot(engine)
    barrier = Barrier(2)

    def approve_racing():
        barrier.wait()
        try:
            return approve_current(request, out)
        except AppError:
            return None

    def answer_racing():
        barrier.wait()
        return respond(request, original.task_id, "蓝 L 改成 19", key="fourth")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(approve_racing), pool.submit(answer_racing)
        approval, receipt = a.result(), b.result()
    assert receipt.task_id == original.task_id
    with session_factory() as s:
        assert s.get(Plan, out.plan_id).status == "INVALIDATED"
        assert not list(
            s.scalars(
                select(Approval).where(Approval.plan_id == out.plan_id, Approval.status == "ACTIVE")
            )
        )
    with pytest.raises(AppError):
        approve_current(request, out)
    if approval is not None:
        with pytest.raises(AppError):
            commit(
                out.plan_id,
                CommitRequest(
                    approval_id=approval.approval_id,
                    plan_hash=out.plan_hash,
                    idempotency_key="raced",
                ),
                request,
            )
    assert business(snapshot(engine)) == business(before)


@pytest.mark.parametrize(
    "damage", ["inactive_sku", "mto", "inactive_customer", "duplicate_customer"]
)
def test_master_resolution_is_exact_active_and_stock_only(
    sheet_env, engine, session_factory, damage
):
    from app.db.models import Customer, Sku
    from app.db.session import begin_immediate

    request, settings = sheet_env
    original = start(request, session_factory, settings)
    source = upload(request, original.task_id).source_id
    with session_factory() as s, begin_immediate(s):
        if damage == "inactive_sku":
            s.get(Sku, "B-BLUE-L").active = False
        elif damage == "mto":
            s.get(Sku, "B-BLUE-L").default_fulfillment_mode = "MTO"
        elif damage == "inactive_customer":
            s.get(Customer, "CUST-HD").active = False
        else:
            s.add(Customer(id="CUST-DUP", code="DUP", display_name="华东服饰", active=True))
    before = snapshot(engine)
    respond(request, original.task_id, INITIAL, [source], "initial")
    out = run_once(session_factory, settings=settings)
    assert out.status == "NEEDS_CLARIFICATION" and out.plan_id is None
    assert business(snapshot(engine)) == business(before)


def test_nochange_answer_creates_new_historical_shortage_without_rewriting_original(
    sheet_env, engine, session_factory
):
    request, settings = sheet_env
    original, _, _, first = shortage(request, session_factory, settings)
    before = snapshot(engine)
    respond(request, original.task_id, "蓝 L 改成 25")
    out = run_once(session_factory, settings=settings)
    assert out.status == "NEEDS_CLARIFICATION" and out.plan_id != first.plan_id
    after = snapshot(engine)
    assert business(after) == business(before)
    assert len(after["purchase_proposal"]) == 2
    assert all(
        r["shortage_qty"] == 5 and r["status"] == "DRAFT" for r in after["purchase_proposal"]
    )
    assert all(r in after["purchase_proposal"] for r in before["purchase_proposal"])
