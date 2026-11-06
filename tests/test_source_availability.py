"""Missing or damaged originals never masquerade as successful evidence replay."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from app.db.models import InputSource
from app.errors import AppError
from app.orchestration.worker import run_once
from app.services.kernel import approve_plan, commit_plan
from app.services.task_view import build_task_view
from tests import test_bounded_sheet_durable as support
from tests.test_bounded_sheet_durable import INITIAL, book, respond, start, upload
from tests.test_durable_new_order import business, snapshot

sheet_env = support.sheet_env


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_original_loss_blocks_replay_and_approval_until_trusted_restore(
    sheet_env, session_factory, damage
):
    request, settings = sheet_env
    task_id = start(request, session_factory, settings).task_id
    content = book([["华东服饰", "A", "白", "M", 30]])
    source = upload(request, task_id, content)
    respond(request, task_id, INITIAL, [source.source_id])
    outcome = run_once(session_factory, settings=settings)

    def view():
        with session_factory() as session:
            return build_task_view(
                session, task_id, request_id="test", correlation_id="test", now=datetime.now(UTC)
            )

    assert view().approval.approvable
    with session_factory() as session:
        path = Path(session.get(InputSource, source.source_id).storage_ref)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"damaged")
    with pytest.raises(AppError) as error:
        upload(request, task_id, content)
    assert error.value.code == "SOURCE_UNAVAILABLE"
    assert not view().approval.approvable
    args = dict(
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
    )
    with pytest.raises(AppError):
        approve_plan(session_factory, **args)
    # Explicit trusted restoration, not an automatic rewrite by replay.
    path.write_bytes(content)
    assert upload(request, task_id, content).source_id == source.source_id
    assert view().approval.approvable
    assert approve_plan(session_factory, **args).approval_id


def test_loss_after_approval_blocks_concurrent_replay_and_commit(
    sheet_env, session_factory, engine
):
    request, settings = sheet_env
    task_id = start(request, session_factory, settings).task_id
    content = book([["华东服饰", "A", "白", "M", 30]])
    source = upload(request, task_id, content)
    respond(request, task_id, INITIAL, [source.source_id])
    plan = run_once(session_factory, settings=settings)
    approval = approve_plan(
        session_factory,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
    )
    with session_factory() as session:
        Path(session.get(InputSource, source.source_id).storage_ref).unlink()
    before = business(snapshot(engine))
    barrier = Barrier(2)

    def attempt(action):
        barrier.wait(5)
        try:
            if action == "replay":
                upload(request, task_id, content)
            else:
                commit_plan(
                    session_factory,
                    plan_id=plan.plan_id,
                    plan_hash=plan.plan_hash,
                    approval_id=approval.approval_id,
                    idempotency_key="lost-source",
                )
        except AppError as error:
            return error.code
        pytest.fail("Missing source must be rejected")

    with ThreadPoolExecutor(max_workers=2) as pool:
        errors = list(pool.map(attempt, ["replay", "commit"]))
    assert errors == ["SOURCE_UNAVAILABLE", "SHEET_CONTEXT_INVALID"]
    assert business(snapshot(engine)) == before
