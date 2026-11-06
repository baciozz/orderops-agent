"""An idempotency key cannot silently acknowledge a different answer."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from app.db.models import InputPayload, Task
from app.db.session import begin_immediate
from app.errors import AppError
from app.orchestration.inbox import accept_input
from sqlalchemy import func, select
from tests import test_stage2 as support
from tests.test_answer_fence import snapshot

durable = support.durable


def answerable(factory, key):
    _, task_id, _ = accept_input(factory, thread_id="shared", client_event_key=key, text="initial")
    with factory() as session, begin_immediate(session):
        session.get(Task, task_id).status = "NEEDS_CLARIFICATION"
    return task_id


@pytest.mark.parametrize("legacy", [False, True])
def test_answer_target_collision_is_read_only_conflict(durable, legacy):
    factory = durable[0]
    a, b = answerable(factory, "a"), answerable(factory, "b")
    kwargs = dict(thread_id="shared", client_event_key="answer", text="one", task_id=a)
    first = accept_input(factory, **kwargs)
    if legacy:
        with factory() as session, begin_immediate(session):
            row = session.get(InputPayload, first[0])
            row.body = {k: v for k, v in row.body.items() if k != "request_hash_v1"}
    before = snapshot(factory)
    with pytest.raises(AppError) as error:
        accept_input(factory, **{**kwargs, "task_id": b})
    assert error.value.code == "INPUT_REQUEST_CONFLICT"
    assert snapshot(factory) == before
    assert accept_input(factory, **kwargs) == first
    assert snapshot(factory) == before


@pytest.mark.parametrize(
    "changed", [{"text": "different"}, {"source_ids": ["source"]}, {"selected_order_id": "SO-1001"}]
)
def test_submit_collision_compares_content_and_scope(durable, changed):
    factory = durable[0]
    args = dict(thread_id="shared", client_event_key="key", text="initial")
    accept_input(factory, **args)
    before = snapshot(factory)
    with pytest.raises(AppError) as error:
        accept_input(factory, **{**args, **changed})
    assert error.value.code == "INPUT_REQUEST_CONFLICT"
    assert snapshot(factory) == before


def test_concurrent_different_answers_have_one_receipt(durable):
    factory = durable[0]
    a, b = answerable(factory, "a"), answerable(factory, "b")
    barrier = Barrier(2)

    def answer(task_id):
        barrier.wait(5)
        try:
            return accept_input(
                factory, thread_id="shared", client_event_key="answer", text="same", task_id=task_id
            )
        except AppError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(answer, [a, b]))
    assert results.count("INPUT_REQUEST_CONFLICT") == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(InputPayload)) == 3
        assert sorted(session.get(Task, task_id).status for task_id in [a, b]) == [
            "NEEDS_CLARIFICATION",
            "QUEUED",
        ]


def test_submit_key_cannot_acknowledge_answer(durable):
    factory = durable[0]
    task_id = answerable(factory, "initial")
    before = snapshot(factory)
    with pytest.raises(AppError) as error:
        accept_input(
            factory, thread_id="shared", client_event_key="initial", text="initial", task_id=task_id
        )
    assert error.value.code == "INPUT_REQUEST_CONFLICT"
    assert snapshot(factory) == before
