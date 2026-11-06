"""Bounded lexical, workbook-integrity and receipt/provenance rejection tests."""

from copy import deepcopy
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from app.db.models import InputPayload, Task
from app.errors import AppError
from app.orchestration.worker import run_once
from app.services.sheet_order import accepted_provenance, quantity
from tests.test_bounded_sheet_durable import (
    INITIAL,
    book,
    respond,
    sheet_env,
    shortage,
    start,
    upload,
)

# Imports are pytest fixture registration, not unused helpers.
__all__ = ["sheet_env"]


@pytest.fixture(scope="session", autouse=True)
def _sandbox_asyncio_polling():
    yield


@pytest.mark.parametrize(
    "text",
    [
        "0",
        "-1",
        "+20",
        "20.0",
        "2e1",
        "２０",
        "20件",
        "01",
        "True",
        "9223372036854775808",
        "9" * 500,
        True,
        20,
        None,
    ],
)
def test_quantity_rejects_nonlexical_or_overflow(text):
    with pytest.raises(AppError):
        quantity(text)


@pytest.mark.parametrize("text", ["1", "20", "9223372036854775807"])
def test_quantity_boundary(text):
    assert quantity(text) == int(text)


def rewrite_xml(content, operation):
    output = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for member in source.infolist():
            data = source.read(member.filename)
            if member.filename == "xl/worksheets/sheet1.xml":
                data = operation(data)
            target.writestr(member, data)
    return output.getvalue()


@pytest.mark.parametrize("value", ["+20", "20.0", "2e1", "２０", True, "9223372036854775808"])
def test_persisted_quantity_text_rejected(sheet_env, session_factory, value):
    request, settings = sheet_env
    origin = start(request, session_factory, settings)
    source = upload(request, origin.task_id, book([["华东服饰", "B", "蓝", "L", value]]))
    respond(request, origin.task_id, INITIAL, [source.source_id])
    outcome = run_once(session_factory, settings=settings)
    assert outcome.status == "NEEDS_CLARIFICATION" and outcome.plan_id is None


def test_numeric_xml_plus_normalizes_to_persisted_20(sheet_env, session_factory):
    request, settings = sheet_env
    origin = start(request, session_factory, settings)
    content = rewrite_xml(
        book([["华东服饰", "B", "蓝", "L", 20]]),
        lambda xml: xml.replace(b"<v>20</v>", b"<v>+20</v>"),
    )
    source = upload(request, origin.task_id, content)
    respond(request, origin.task_id, INITIAL, [source.source_id])
    outcome = run_once(session_factory, settings=settings)
    assert outcome.status == "AWAITING_APPROVAL"
    with session_factory() as session:
        record = accepted_provenance(session, session.get(Task, origin.task_id))
        assert record["rows"][0]["base_qty"] == 20
        assert record["rows"][0]["refs"][4]["text_excerpt"] == "20"


def test_merge_lost_by_spans_still_rejected(sheet_env, session_factory):
    request, settings = sheet_env
    origin = start(request, session_factory, settings)
    content = rewrite_xml(
        book([["华东服饰", "B", "蓝", "L", 20]]),
        lambda xml: xml.replace(
            b"</sheetData>",
            b'</sheetData><mergeCells count="1"><mergeCell ref="A2:A3"/></mergeCells>',
        ),
    )
    source = upload(request, origin.task_id, content)
    respond(request, origin.task_id, INITIAL, [source.source_id])
    outcome = run_once(session_factory, settings=settings)
    assert outcome.status == "NEEDS_CLARIFICATION" and outcome.plan_id is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("activation_sequence", True),
        ("current_intent", 999),
        ("sources", []),
        ("rows", []),
        ("extra", 1),
    ],
)
def test_accepted_context_reconstructs_instead_of_trusting_json(
    sheet_env, session_factory, field, value
):
    request, settings = sheet_env
    original, _, initial, _ = shortage(request, session_factory, settings)
    with session_factory() as session:
        receipt = session.get(InputPayload, initial.event_id)
        record = deepcopy(receipt.body["sheet_order_v1"])
        record[field] = value
        receipt.body = {**receipt.body, "sheet_order_v1": record}
        session.commit()
        with pytest.raises(AppError):
            accepted_provenance(session, session.get(Task, original.task_id))


def test_previous_shortage_hash_rejects_consistent_json_tamper(sheet_env, session_factory):
    from app.db.models import Plan

    request, settings = sheet_env
    origin, _, initial, rejected = shortage(request, session_factory, settings)
    with session_factory() as session:
        receipt = session.get(InputPayload, initial.event_id)
        plan = session.get(Plan, rejected.plan_id)
        record = deepcopy(receipt.body["sheet_order_v1"])
        record["shortage"]["facts"][0]["stock_revision"] += 1
        receipt.body = {**receipt.body, "sheet_order_v1": record}
        plan.normalized_changes = {**plan.normalized_changes, "sheet_order_v1": record}
        session.commit()
    respond(request, origin.task_id, "蓝 L 改成 20", key="tampered-answer")
    outcome = run_once(session_factory, settings=settings)
    assert outcome.status == "NEEDS_CLARIFICATION" and outcome.plan_id is None


@pytest.mark.parametrize(
    "value",
    [
        {"x": list(range(30001))},
        {"x": 1.0},
        {"x": [[[[[[[[[[[[[]]]]]]]]]]]]]},
        {str(n): n for n in range(33)},
    ],
)
def test_private_record_bounds(value):
    from app.services.sheet_order import _bounded

    with pytest.raises(AppError):
        _bounded(value)
