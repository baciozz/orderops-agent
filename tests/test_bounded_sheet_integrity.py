# ruff: noqa: F811
"""Independent authorization checks over the real bounded durable plan pipeline."""

from copy import deepcopy

import pytest
from app.api.orders import commit
from app.db.models import InputPayload, InputSource, Plan, SourceSpan
from app.db.session import begin_immediate
from app.domain.changes import ChangeEval, SnapLine
from app.domain.hashing import canonical_hash
from app.domain.plan_snapshot import build_plan_payload
from app.errors import AppError
from app.orchestration.worker import run_once
from schemas.api import CommitRequest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from tests.test_bounded_sheet_durable import (  # noqa: F401
    _sandbox_asyncio_polling,
    approve_current,
    business,
    respond,
    sheet_env,
    shortage,
    snapshot,
)


def candidate(environment, factory):
    request, settings = environment
    original, sources, initial, rejected = shortage(request, factory, settings)
    answer = respond(request, original.task_id, "蓝 L 改成 20")
    out = run_once(factory, settings=settings)
    assert out.status == "AWAITING_APPROVAL"
    return request, answer, out, sources


TAMPERS = [
    "remove",
    "version",
    "task",
    "activation",
    "initial",
    "current",
    "sequence",
    "intent",
    "answer_event",
    "answer_text",
    "answer_old",
    "answer_new",
    "white",
    "source_hash",
    "source_ref",
    "plan_refs",
    "plan_qty",
    "raw_answer",
    "raw_sequence",
    "file_hash",
    "span",
    "prior_proposal",
    "prior_plan",
]


def tamper(session, out, answer, sources, name):
    plan = session.get(Plan, out.plan_id)
    normalized = deepcopy(plan.normalized_changes)
    record = normalized["sheet_order_v1"]
    if name == "remove":
        normalized.pop("sheet_order_v1")
    elif name == "version":
        record["version"] = True
    elif name in {"task", "activation", "initial", "current"}:
        key = {
            "task": "task_id",
            "activation": "activation_event_id",
            "initial": "initial_event_id",
            "current": "current_event_id",
        }[name]
        record[key] = "foreign"
    elif name == "sequence":
        record["current_sequence"] = True
    elif name == "intent":
        record["current_intent"] += 1
    elif name.startswith("answer_"):
        key = {
            "answer_event": "event_id",
            "answer_text": "text",
            "answer_old": "old_qty",
            "answer_new": "new_qty",
        }[name]
        record["correction"][key] = "foreign" if key in {"event_id", "text"} else 17
    elif name == "white":
        row = next(row for row in record["rows"] if row["sku_id"] == "A-WHITE-M")
        row["effective_qty"] = 29
    elif name == "source_hash":
        record["sources"][0]["sha256"] = "0" * 64
    elif name == "source_ref":
        record["rows"][0]["refs"][0]["cell_range"] = "A99"
    elif name == "plan_refs":
        plan.source_refs = []
    elif name == "plan_qty":
        normalized["changes"][0]["qty"] = 29
    elif name in {"raw_answer", "raw_sequence"}:
        receipt = session.get(InputPayload, answer.event_id)
        receipt.body = {
            **receipt.body,
            ("text" if name == "raw_answer" else "input_sequence"): (
                "蓝 L 改成 19" if name == "raw_answer" else True
            ),
        }
    elif name == "file_hash":
        session.get(InputSource, sources[0]).sha256 = "0" * 64
    elif name == "span":
        span = session.scalar(select(SourceSpan).where(SourceSpan.source_id == sources[0]))
        span.text_excerpt = "tampered"
    elif name == "prior_proposal":
        record["previous_shortage"]["facts"][0]["proposal_id"] = "foreign"
    elif name == "prior_plan":
        record["previous_shortage"]["plan_id"] = "foreign"
    plan.normalized_changes = normalized
    flag_modified(plan, "normalized_changes")


@pytest.mark.parametrize("phase", ["approve", "commit"])
@pytest.mark.parametrize("change", TAMPERS)
def test_provenance_tamper_never_authorizes_business_write(
    sheet_env, session_factory, engine, phase, change
):
    request, answer, out, sources = candidate(sheet_env, session_factory)
    approved = approve_current(request, out) if phase == "commit" else None
    with session_factory() as session, begin_immediate(session):
        tamper(session, out, answer, sources, change)
    before = business(snapshot(engine))
    with pytest.raises(AppError):
        if phase == "approve":
            approve_current(request, out)
        else:
            commit(
                out.plan_id,
                CommitRequest(
                    approval_id=approved.approval_id,
                    plan_hash=out.plan_hash,
                    idempotency_key="must-not-commit",
                ),
                request,
            )
    assert business(snapshot(engine)) == before
    assert snapshot(engine)["operation"] == []


def test_optional_hash_contract_keeps_legacy_and_binds_equal_quantity_answer_identity():
    arguments = dict(
        plan_kind="CREATE",
        order_id=None,
        customer_id="customer",
        base_revision=0,
        intent_revision=3,
        evaluation=ChangeEval(
            kind="validated",
            lines={"sku": SnapLine("sku", 20, "STOCK")},
            reservation_delta={"sku": 20},
        ),
        stock_revisions={"sku": 1},
    )
    legacy = build_plan_payload(**arguments)
    assert "sheet_order_v1" not in legacy
    assert legacy == build_plan_payload(**arguments, sheet_order_v1=None)
    first = build_plan_payload(**arguments, sheet_order_v1={"current_event_id": "answer-one"})
    second = build_plan_payload(**arguments, sheet_order_v1={"current_event_id": "answer-two"})
    assert len({canonical_hash(legacy), canonical_hash(first), canonical_hash(second)}) == 3


def test_kernel_shortage_replay_preserves_exact_proposal_links(sheet_env, session_factory, engine):
    """Supplement the durable golden path with retry of its already stored kernel request."""
    from app.services.kernel import plan_new_order

    request, settings = sheet_env
    original, sources, initial, rejected = shortage(request, session_factory, settings)
    before = snapshot(engine)
    replay = plan_new_order(session_factory, lines=[], client_event_key=initial.event_id)
    assert replay.status == "NEEDS_CLARIFICATION"
    assert replay.plan_id == rejected.plan_id
    assert replay.proposal_ids == [row["id"] for row in before["purchase_proposal"]]
    assert snapshot(engine) == before
