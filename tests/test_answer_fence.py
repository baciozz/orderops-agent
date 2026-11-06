"""Answer fences are atomic, optional, and subordinate to delivery replay."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from app.db.models import Approval, Event, InboxDelivery, InputPayload, Plan, SalesOrder, Task
from app.db.session import begin_immediate
from app.errors import AppError
from app.orchestration.fixtures import TEXT_ALL_NAVY, TEXT_ATTACH_SHEET, TEXT_ONLY_UNCUT
from app.orchestration.inbox import accept_input, latest_input
from app.orchestration.state import transition
from app.orchestration.worker import Limits, run_once
from schemas.api import AnswerSubmitRequest, InputSubmitRequest
from sqlalchemy import func, select
from tests.test_demo_ui import web  # noqa: F401
from tests.test_stage2 import durable, planned  # noqa: F401


def snapshot(factory):
    with factory() as session:
        return {
            model.__name__: [
                tuple(getattr(row, column.name) for column in model.__table__.columns)
                for row in session.scalars(
                    select(model).order_by(model.id if hasattr(model, "id") else model.event_id)
                )
            ]
            for model in (Task, SalesOrder, Plan, Approval, Event, InputPayload, InboxDelivery)
        }


def body(key, revision=None):
    result = {"thread_id": "answers", "client_event_key": key, "text": "clarification"}
    if revision is not None:
        result["expected_intent_revision"] = revision
    return result


def test_stale_answer_does_not_invalidate_approval_or_append(durable):  # noqa: F811
    factory = durable[0]
    outcome, _ = planned(durable)
    before = snapshot(factory)
    with pytest.raises(AppError) as exc:
        accept_input(factory, task_id=outcome.task_id, **body("stale", outcome.intent_revision - 1))
    assert exc.value.code == "STALE_INTENT"
    assert snapshot(factory) == before
    accept_input(factory, task_id=outcome.task_id, **body("fresh", outcome.intent_revision))
    with factory() as session:
        assert session.get(Task, outcome.task_id).intent_revision == outcome.intent_revision + 1
        assert session.get(Plan, outcome.plan_id).status == "INVALIDATED"
        assert session.scalar(select(Approval)).status == "REVOKED"


def test_replay_after_revision_and_terminal_status_changes_is_read_only(durable):  # noqa: F811
    factory = durable[0]
    outcome, _ = planned(durable)
    request = body("lost-ack", outcome.intent_revision)
    first = accept_input(factory, task_id=outcome.task_id, **request)
    with factory() as session, begin_immediate(session):
        task = session.get(Task, outcome.task_id)
        transition(task, "CANCELLED")
    before = snapshot(factory)
    replay = accept_input(factory, task_id=outcome.task_id, **request)
    assert replay == (first[0], first[1], "CANCELLED")
    assert snapshot(factory) == before


def test_two_concurrent_answers_have_one_winner(durable):  # noqa: F811
    factory = durable[0]
    outcome, _ = planned(durable)
    barrier = Barrier(2)

    def answer(key):
        barrier.wait(timeout=5)
        try:
            return accept_input(
                factory, task_id=outcome.task_id, **body(key, outcome.intent_revision)
            )
        except AppError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(answer, ("race-a", "race-b")))
    assert sum(isinstance(result, tuple) for result in results) == 1
    assert results.count("STALE_INTENT") == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(InputPayload)) == 2
        assert session.scalar(select(func.count()).select_from(InboxDelivery)) == 2


def test_http_get_to_post_race_and_legacy_clients(web):  # noqa: F811
    client, factory, _ = web
    response = client.post("/inputs", json={**body("initial"), "selected_order_id": "SO-1001"})
    task_id = response.json()["task_id"]
    with factory() as session, begin_immediate(session):
        session.get(Task, task_id).status = "NEEDS_CLARIFICATION"
    revision = client.get(f"/tasks/{task_id}").json()["intent_revision"]
    first = client.post(f"/tasks/{task_id}/answers", json=body("winner", revision))
    assert first.status_code == 202
    with factory() as session, begin_immediate(session):
        session.get(Task, task_id).status = "NEEDS_CLARIFICATION"
    before = snapshot(factory)
    stale = client.post(f"/tasks/{task_id}/answers", json=body("late", revision))
    assert stale.status_code == 409
    assert "STALE_INTENT" in stale.text
    assert snapshot(factory) == before
    retry = client.post(f"/tasks/{task_id}/answers", json=body("winner", revision))
    assert retry.status_code == 202
    assert retry.json()["event_id"] == first.json()["event_id"]
    assert snapshot(factory) == before
    assert client.post(f"/tasks/{task_id}/answers", json=body("old-client")).status_code == 202


@pytest.mark.parametrize("revision", [-1, True, "1", 1.2])
def test_answer_fence_rejects_invalid_revisions(web, revision):  # noqa: F811
    client, _, _ = web
    assert client.post("/tasks/missing/answers", json=body("invalid", revision)).status_code == 400


def test_frozen_input_contract_is_unchanged():
    assert "expected_intent_revision" not in InputSubmitRequest.model_fields
    assert AnswerSubmitRequest(**body("legacy")).expected_intent_revision is None
    assert AnswerSubmitRequest(**body("zero", 0)).expected_intent_revision == 0


@pytest.mark.parametrize("bound", [False, True])
def test_worker_consumes_successive_fenced_answers(durable, bound):  # noqa: F811
    factory, clock, _, _ = durable
    text = TEXT_ALL_NAVY if bound else TEXT_ATTACH_SHEET
    _, task_id, _ = accept_input(factory, **{**body("initial"), "text": text}, clock=clock)
    assert run_once(factory, clock=clock).status == "NEEDS_CLARIFICATION"
    for index in range(2):
        with factory() as session:
            task = session.get(Task, task_id)
            revision = task.intent_revision
            input_id = latest_input(session, task).event_id
            assert bool(task.order_id) == bound
        request = {
            **body(f"cycle-{index}", revision),
            "text": text,
            "expected_input_event_id": input_id,
        }
        receipt = accept_input(factory, task_id=task_id, clock=clock, **request)
        with factory() as session:
            assert session.get(Task, task_id).intent_revision == revision + 1
        assert run_once(factory, clock=clock).status == "NEEDS_CLARIFICATION"
        with factory() as session:
            assert session.get(Task, task_id).intent_revision == revision + 1
        before = snapshot(factory)
        assert accept_input(factory, task_id=task_id, **request)[:2] == receipt[:2]
        with pytest.raises(AppError) as exc:
            accept_input(factory, task_id=task_id, **{**request, "client_event_key": "late"})
        assert exc.value.code == "STALE_INTENT"
        assert snapshot(factory) == before
    if bound:
        accept_input(
            factory,
            task_id=task_id,
            clock=clock,
            **{**body("resolved", revision + 1), "text": TEXT_ONLY_UNCUT},
        )
        assert (
            run_once(factory, clock=clock, limits=Limits(attempts=4)).status == "AWAITING_APPROVAL"
        )


def test_input_fence_blocks_unbound_cycles_and_binding_collision(durable):  # noqa: F811
    factory, clock, _, _ = durable
    event_id, task_id, _ = accept_input(
        factory, **{**body("initial"), "text": TEXT_ATTACH_SHEET}, clock=clock
    )
    assert run_once(factory, clock=clock).status == "NEEDS_CLARIFICATION"
    old_request = {**body("stale-tab", 1), "expected_input_event_id": event_id}
    accepted = accept_input(
        factory,
        task_id=task_id,
        clock=clock,
        **{
            **body("unbound-cycle", 1),
            "text": TEXT_ATTACH_SHEET,
            "expected_input_event_id": event_id,
        },
    )
    assert run_once(factory, clock=clock).status == "NEEDS_CLARIFICATION"
    before = snapshot(factory)
    with pytest.raises(AppError) as exc:
        accept_input(factory, task_id=task_id, **old_request)
    assert exc.value.code == "STALE_INTENT"
    assert snapshot(factory) == before
    # Start the target at the unbound revision; acquisition must advance it.
    with factory() as session, begin_immediate(session):
        session.get(SalesOrder, "SO-1001").intent_revision = 2
    accept_input(
        factory,
        task_id=task_id,
        clock=clock,
        **{**body("bind", 2), "text": TEXT_ONLY_UNCUT, "expected_input_event_id": accepted[0]},
    )
    assert run_once(factory, clock=clock).status == "AWAITING_APPROVAL"
    with factory() as session:
        assert session.get(Task, task_id).intent_revision == 3
    before = snapshot(factory)
    # Even a matching revision cannot rescue an obsolete input-event fence.
    old_request["expected_intent_revision"] = 3
    with pytest.raises(AppError) as exc:
        accept_input(factory, task_id=task_id, **old_request)
    assert exc.value.code == "STALE_INTENT"
    assert snapshot(factory) == before


def test_html_fence_uses_latest_input_not_plan_event(web):  # noqa: F811
    client, factory, _ = web
    response = client.post("/inputs", json={**body("initial"), "text": TEXT_ALL_NAVY})
    task_id, input_id = response.json()["task_id"], response.json()["event_id"]
    assert run_once(factory).status == "NEEDS_CLARIFICATION"
    with factory() as session:
        assert session.get(Task, task_id).last_event_id != input_id
    page = client.get(f"/ui/tasks/{task_id}")
    assert f'data-input-event-id="{input_id}"' in page.text
    revision = client.get(f"/tasks/{task_id}").json()["intent_revision"]
    answer = client.post(
        f"/tasks/{task_id}/answers",
        json={**body("answer", revision), "expected_input_event_id": input_id},
    )
    assert answer.status_code == 202
    before = snapshot(factory)
    replay = client.post(
        f"/tasks/{task_id}/answers",
        json={**body("answer", revision), "expected_input_event_id": input_id},
    )
    assert replay.status_code == 202
    assert replay.json()["event_id"] == answer.json()["event_id"]
    assert snapshot(factory) == before


@pytest.mark.parametrize("same_key", [False, True])
def test_unbound_concurrent_input_fences_and_duplicate_replays(durable, same_key):  # noqa: F811
    factory, clock, _, _ = durable
    input_id, task_id, _ = accept_input(
        factory, **{**body("initial"), "text": TEXT_ATTACH_SHEET}, clock=clock
    )
    assert run_once(factory, clock=clock).status == "NEEDS_CLARIFICATION"
    barrier = Barrier(2)

    def answer(index):
        barrier.wait(timeout=5)
        try:
            return accept_input(
                factory,
                task_id=task_id,
                **{
                    **body("same" if same_key else f"answer-{index}", 1),
                    "expected_input_event_id": input_id,
                },
            )
        except AppError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(answer, (1, 2)))
    if same_key:
        assert results[0] == results[1]
    else:
        assert results.count("STALE_INTENT") == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(InputPayload)) == 2
        assert session.scalar(select(func.count()).select_from(InboxDelivery)) == 2


def test_html_projection_race_keeps_old_input_fence(web, monkeypatch):  # noqa: F811
    from app.api import tasks

    client, factory, _ = web
    initial = client.post("/inputs", json={**body("initial"), "text": TEXT_ATTACH_SHEET}).json()
    task_id, input_id = initial["task_id"], initial["event_id"]
    assert run_once(factory).status == "NEEDS_CLARIFICATION"
    original_view = tasks._view

    def racing_view(task_id, request):
        old_view = original_view(task_id, request)
        accept_input(factory, task_id=task_id, **body("concurrent"))
        with factory() as session, begin_immediate(session):
            session.get(Task, task_id).status = "NEEDS_CLARIFICATION"
        return old_view

    monkeypatch.setattr(tasks, "_view", racing_view)
    page = client.get(f"/ui/tasks/{task_id}")
    assert f'data-input-event-id="{input_id}"' in page.text
    before = snapshot(factory)
    response = client.post(
        f"/tasks/{task_id}/answers",
        json={**body("old-page", 0), "expected_input_event_id": input_id},
    )
    assert response.status_code == 409
    assert snapshot(factory) == before


@pytest.mark.parametrize("empty_file", [False, True])
def test_missing_or_empty_database_keeps_task_404_and_stub(tmp_path, empty_file):
    from app.config import Settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    path = tmp_path / "uninitialized.db"
    if empty_file:
        path.touch()
    app = create_app(Settings(_env_file=None, database_url=f"sqlite:///{path}"))
    with TestClient(base_url="http://127.0.0.1", app=app) as client:
        assert client.get("/ui/tasks/unknown").status_code == 404
        assert client.get("/ui/tasks/T-STUB").status_code == 200
    if not empty_file:
        assert not path.exists()


def test_legacy_no_input_task_hides_unsafe_ui_but_keeps_api_compatibility(web):  # noqa: F811
    client, factory, _ = web
    with factory() as session, begin_immediate(session):
        session.add(
            Task(id="legacy", chain_id="legacy", status="NEEDS_CLARIFICATION", intent_revision=0)
        )
    page = client.get("/ui/tasks/legacy")
    assert page.status_code == 200
    assert 'id="clarification-answer"' not in page.text
    assert 'id="source-upload"' not in page.text
    assert "该历史任务无可核对的输入记录，请从新任务开始" in page.text
    before = snapshot(factory)
    assert (
        client.post(
            "/tasks/legacy/answers", json={**body("fenced"), "expected_input_event_id": "wrong"}
        ).status_code
        == 409
    )
    assert snapshot(factory) == before
    assert client.post("/tasks/legacy/answers", json=body("legacy-answer")).status_code == 202
