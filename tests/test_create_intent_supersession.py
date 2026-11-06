"""Task-scoped CREATE input ownership, including legacy and serialized races.

Strict controlled LIVE outputs test infrastructure, not model interpretation accuracy.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from app.db.models import Approval, Event, Plan, Task
from app.db.session import begin_immediate
from app.errors import AppError
from app.orchestration.inbox import accept_input
from app.orchestration.recovery import ResponseLost, durable_commit
from app.orchestration.watchdog import scan
from app.services.kernel import approve_plan, bound_task_id, commit_plan, plan_new_order
from sqlalchemy import select
from tests.test_late_bound_intent import business, run, sandbox, snapshot  # noqa: F401


def output(qty=19, **updates):
    return dict(
        order_candidates=[],
        customer_candidate="华东服饰",
        line_changes=[],
        new_lines=[dict(sku_id="A-WHITE-M", qty=30), dict(sku_id="B-BLUE-L", qty=qty)],
        missing_fields=[],
        conflicts=[],
        questions=[],
        event_type="NEW_ORDER",
        order_id=None,
        clarification_choice=None,
        **updates,
    )


def receipt(factory, key="first", task_id=None, **kwargs):
    return accept_input(
        factory,
        thread_id="create",
        client_event_key=key,
        task_id=task_id,
        text="新建白M30蓝L19" if task_id is None else "蓝 L 改成 20",
        **kwargs,
    )


def approve(factory, plan):
    return approve_plan(
        factory,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
    )


def commit(factory, plan, approval, key="commit", **kwargs):
    return commit_plan(
        factory,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        approval_id=approval.approval_id,
        idempotency_key=key,
        **kwargs,
    )


def initial(environment, approved=True):
    engine, factory, settings = environment
    r = receipt(factory)
    p = run(factory, settings, output())
    assert p.status == "AWAITING_APPROVAL" and p.intent_revision == 1
    a = approve(factory, p) if approved else None
    return r, p, a


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("replacement", ["queued", "valid", "clarify", "invalid", "shortage"])
def test_old_candidates_never_authorize_after_receipt(sandbox, approved, replacement):  # noqa: F811
    engine, factory, settings = sandbox
    r, old, approval = initial(sandbox, approved)
    before = snapshot(engine)
    r2 = receipt(factory, "second", old.task_id)
    with factory() as s:
        task = s.get(Task, old.task_id)
        assert task.intent_revision == 2 and task.chain_id == old.chain_id
        assert s.get(Plan, old.plan_id).status == "INVALIDATED"
        if approval:
            assert s.get(Approval, approval.approval_id).status == "REVOKED"
    if replacement != "queued":
        candidate = output(20)
        if replacement == "clarify":
            candidate["questions"] = ["请核对数量"]
        if replacement == "invalid":
            candidate["new_lines"] = "invalid schema"
        if replacement == "shortage":
            candidate = output(25)
            with pytest.raises(AppError, match="整单拒绝"):
                run(factory, settings, candidate)
        else:
            new = run(factory, settings, candidate)
            assert new.intent_revision == 2
    with pytest.raises(AppError):
        approve(factory, old)
    if approval:
        with pytest.raises(AppError):
            commit(factory, old, approval)
    after = snapshot(engine)
    # Shortage deliberately retains its nonexecuting proposal as failure evidence.
    before_business, after_business = business(before), business(after)
    if replacement == "shortage":
        before_business.pop("purchase_proposal")
        assert after_business.pop("purchase_proposal")
    assert after_business == before_business
    assert [a for a in after["approval"] if a["plan_id"] != old.plan_id] == [
        a for a in before["approval"] if a["plan_id"] != old.plan_id
    ]
    assert receipt(factory, "second", old.task_id)[:2] == r2[:2]
    assert receipt(factory)[:2] == r[:2]
    assert snapshot(engine) == after


def test_current_plan_requires_separate_approval_and_commits_latest_only(sandbox):  # noqa: F811
    engine, factory, settings = sandbox
    _, old, approval = initial(sandbox)
    receipt(factory, "second", old.task_id)
    new = run(factory, settings, output(20))
    assert new.chain_id == old.chain_id and new.intent_revision == old.intent_revision + 1
    before = snapshot(engine)
    with pytest.raises(AppError):
        commit(factory, new, approval)
    assert snapshot(engine) == before
    fresh = approve(factory, new)
    assert approve(factory, new) == fresh
    result = commit(factory, new, fresh)
    after = snapshot(engine)
    assert sorted(
        (r["sku_id"], r["qty"]) for r in after["reservation"] if r["order_id"] == result.order_id
    ) == [("A-WHITE-M", 30), ("B-BLUE-L", 20)]
    assert len(after["operation"]) == 1
    assert commit(factory, new, fresh).replayed
    assert snapshot(engine) == after
    with pytest.raises(AppError):
        receipt(factory, "third", old.task_id)
    assert snapshot(engine) == after


def test_multiple_identical_answers_advance_once_and_same_event_plan_replays(sandbox):  # noqa: F811
    engine, factory, settings = sandbox
    _, old, _ = initial(sandbox)
    for i in (2, 3):
        received = receipt(factory, f"answer-{i}", old.task_id)
        new = run(factory, settings, output(19))
        assert new.intent_revision == i and new.chain_id == old.chain_id
        before = snapshot(engine)
        assert receipt(factory, f"answer-{i}", old.task_id)[:2] == received[:2]
        replay = plan_new_order(factory, lines=[], client_event_key=received[0])
        assert replay.plan_id == new.plan_id
        assert snapshot(engine) == before
    with factory() as s:
        assert s.get(Plan, old.plan_id).status == "INVALIDATED"


def test_receipt_rollback_and_stale_expected_fences_leave_old_approval_intact(sandbox):  # noqa: F811
    engine, factory, _ = sandbox
    original, old, approval = initial(sandbox)
    before = snapshot(engine)

    def crash(_):
        raise RuntimeError("crash before receipt commit")

    with pytest.raises(RuntimeError):
        receipt(factory, "second", old.task_id, fault=crash)
    assert snapshot(engine) == before
    for fence in ({"expected_intent_revision": 0}, {"expected_input_event_id": "stale"}):
        with pytest.raises(AppError) as exc:
            receipt(factory, "second", old.task_id, **fence)
        assert exc.value.code == "STALE_INTENT"
        assert snapshot(engine) == before
    receipt(
        factory,
        "second",
        old.task_id,
        expected_intent_revision=1,
        expected_input_event_id=original[0],
    )
    after = snapshot(engine)
    assert receipt(factory, "second", old.task_id, expected_intent_revision=1)[0]
    assert snapshot(engine) == after


@pytest.mark.parametrize("operation", ["approve", "commit", "answer"])
def test_serialized_receipt_races_cannot_resurrect_plan(sandbox, operation):  # noqa: F811
    engine, factory, settings = sandbox
    _, old, approval = initial(sandbox, operation != "approve")
    barrier = Barrier(2)

    def race(fn):
        barrier.wait(timeout=5)
        try:
            return fn()
        except AppError as exc:
            return exc.code

    other = {
        "approve": lambda: approve(factory, old),
        "commit": lambda: commit(factory, old, approval),
        "answer": lambda: receipt(factory, "other", old.task_id),
    }[operation]
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(race, lambda: receipt(factory, "second", old.task_id))
        b = pool.submit(race, other)
        received, competing = a.result(timeout=15), b.result(timeout=15)
    state = snapshot(engine)
    if operation == "commit" and not isinstance(competing, str):
        # Commit won the write lock before receipt. The answer must be rejected.
        assert received == "ILLEGAL_TRANSITION" and len(state["operation"]) == 1
    else:
        assert not state["operation"]
        assert next(p for p in state["plan"] if p["id"] == old.plan_id)["status"] == "INVALIDATED"
        assert all(
            a["status"] != "ACTIVE" for a in state["approval"] if a["plan_id"] == old.plan_id
        )
        new = run(factory, settings, output(20))
        assert new.intent_revision == 2
        with pytest.raises(AppError):
            approve(factory, old)
        if approval:
            with pytest.raises(AppError):
                commit(factory, old, approval)


@pytest.mark.parametrize("approved", [True, False])
def test_legacy_equal_revision_old_plan_fails_provenance_gate(sandbox, approved):  # noqa: F811
    engine, factory, settings = sandbox
    _, old, a = initial(sandbox, approved)
    receipt(factory, "second", old.task_id)
    run(factory, settings, output(20))
    # Recreate pre-fix durable rows: two input events, unchanged epoch and old live plan.
    with factory() as s, begin_immediate(s):
        s.get(Task, old.task_id).intent_revision = 1
        s.get(Plan, old.plan_id).status = "APPROVED" if approved else "VALIDATED"
        if a:
            s.get(Approval, a.approval_id).status = "ACTIVE"
    before = business(snapshot(engine))
    with pytest.raises(AppError) as exc:
        approve(factory, old)
    assert exc.value.code == "STALE_INTENT"
    if a:
        with pytest.raises(AppError) as exc:
            commit(factory, old, a)
        assert exc.value.code == "STALE_INTENT"
    assert business(snapshot(engine)) == before
    # A new receipt advances beyond every persisted plan epoch, never resets to one.
    receipt(factory, "third", old.task_id)
    new = run(factory, settings, output(20))
    assert new.intent_revision == 3


@pytest.mark.parametrize("damage", ["missing_origin", "wrong_chain", "wrong_origin_intent"])
def test_legacy_unproven_current_plan_fails_closed(sandbox, damage):  # noqa: F811
    engine, factory, _ = sandbox
    _, plan, approval = initial(sandbox)
    with factory() as s, begin_immediate(s):
        event = s.scalar(select(Event).where(Event.payload_ref == plan.plan_id))
        if damage == "missing_origin":
            event.payload_ref = None
        elif damage == "wrong_chain":
            event.chain_id = "unproven"
        else:
            event.intent_revision += 1
    before = business(snapshot(engine))
    with pytest.raises(AppError):
        approve(factory, plan)
    with pytest.raises(AppError):
        commit(factory, plan, approval)
    assert business(snapshot(engine)) == before


def test_legacy_current_plan_with_intact_provenance_remains_usable(sandbox):  # noqa: F811
    _, factory, _ = sandbox
    _, plan, approval = initial(sandbox)
    # Legacy latest plans with verifiable event/receipt identity need no migration.
    assert approve(factory, plan) == approval
    assert commit(factory, plan, approval).revision == 1


def test_old_worker_fenced_and_crash_retry_preserves_epoch(sandbox):  # noqa: F811
    engine, factory, settings = sandbox
    original, old, _ = initial(sandbox)
    latest = receipt(factory, "second", old.task_id)
    token = bound_task_id.set(old.task_id)
    try:
        with pytest.raises(AppError) as exc:
            plan_new_order(
                factory,
                lines=output()["new_lines"],
                customer_name="华东服饰",
                client_event_key="unrecognized-old-input",
            )
        assert exc.value.code == "STALE_INTENT"
    finally:
        bound_task_id.reset(token)
    now = datetime.now(UTC)

    def crash(ctx):
        if ctx.spec.agent_id == "inventory-check":
            raise RuntimeError("crash after parse")

    with pytest.raises(RuntimeError):
        run(factory, settings, output(20), hook=crash, clock=lambda: now)
    with factory() as s:
        assert s.get(Task, old.task_id).intent_revision == 2
    later = now + timedelta(seconds=61)
    scan(factory, clock=lambda: later, settings=settings)
    new = run(factory, settings, output(20), clock=lambda: later)
    assert new.intent_revision == 2 and new.chain_id == old.chain_id
    assert receipt(factory, "second", old.task_id)[:2] == latest[:2]
    before = snapshot(engine)
    # Original event replay is historical only, never reactivates or republishes its plan.
    replay = plan_new_order(factory, lines=[], client_event_key=original[0])
    assert replay.plan_id == old.plan_id
    assert snapshot(engine) == before
    with pytest.raises(AppError):
        approve(factory, replay)


def test_committed_ack_loss_replay_precedes_current_state_gate(sandbox):  # noqa: F811
    engine, factory, _ = sandbox
    _, plan, approval = initial(sandbox)
    args = dict(
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        approval_id=approval.approval_id,
        idempotency_key="lost-ack",
    )

    def lose(_):
        raise ResponseLost()

    with pytest.raises(ResponseLost):
        durable_commit(factory, **args, fault=lose)
    before = business(snapshot(engine))
    assert durable_commit(factory, **args).replayed
    assert business(snapshot(engine)) == before
    after = snapshot(engine)
    assert durable_commit(factory, **args).replayed
    assert snapshot(engine) == after


@pytest.mark.parametrize("status", ["CANCELLED", "SUPERSEDED", "FAILED"])
def test_terminal_task_cannot_approve_commit_or_accept_new_input(sandbox, status):  # noqa: F811
    engine, factory, _ = sandbox
    original, plan, approval = initial(sandbox)
    with factory() as s, begin_immediate(s):
        s.get(Task, plan.task_id).status = status
    before = snapshot(engine)
    with pytest.raises(AppError):
        approve(factory, plan)
    with pytest.raises(AppError):
        receipt(factory, "second", plan.task_id)
    assert receipt(factory)[:2] == original[:2]
    assert snapshot(engine) == before
    with pytest.raises(AppError):
        commit(factory, plan, approval)
    assert business(snapshot(engine)) == business(before)


def test_cross_task_receipt_pointer_is_not_current_create_provenance(sandbox):  # noqa: F811
    engine, factory, _ = sandbox
    _, plan, approval = initial(sandbox)
    foreign = accept_input(factory, thread_id="foreign", client_event_key="foreign", text="other")
    with factory() as s, begin_immediate(s):
        s.get(Task, plan.task_id).last_event_id = foreign[0]
        event = s.scalar(select(Event).where(Event.payload_ref == plan.plan_id))
        event.source_event_key = foreign[0]
    before = business(snapshot(engine))
    with pytest.raises(AppError):
        approve(factory, plan)
    with pytest.raises(AppError):
        commit(factory, plan, approval)
    assert business(snapshot(engine)) == before
    with factory() as session, begin_immediate(session):
        event = session.scalar(select(Event).where(Event.payload_ref == plan.plan_id))
        event.source_event_key = "damaged-origin"
    token = bound_task_id.set(plan.task_id)
    try:
        with pytest.raises(AppError):
            plan_new_order(factory, lines=[], client_event_key=foreign[0])
    finally:
        bound_task_id.reset(token)


def test_new_candidate_to_existing_change_preserves_order_chronology(sandbox):  # noqa: F811
    from tests.test_late_bound_intent import payload

    engine, factory, settings = sandbox
    _, old, approval = initial(sandbox)
    receipt(factory, "change", old.task_id)
    new = run(factory, settings, payload())
    assert new.status == "AWAITING_APPROVAL" and new.order_id == "SO-1001"
    assert new.intent_revision == 3
    with pytest.raises(AppError):
        approve(factory, old)
    with pytest.raises(AppError):
        commit(factory, old, approval)
    fresh = approve(factory, new)
    assert commit(factory, new, fresh).order_id == "SO-1001"
    assert len(snapshot(engine)["sales_order"]) == 2


@pytest.mark.parametrize("partial", [True, False])
def test_inferred_order_task_can_still_accept_explicit_new_order(sandbox, partial):  # noqa: F811
    from tests.test_late_bound_intent import payload

    engine, factory, settings = sandbox
    receipt(factory)
    inferred = payload()
    prior = run(factory, settings, inferred)
    if partial:
        # Persisted partial inferred binding is a supported pre-existing state.
        with factory() as session, begin_immediate(session):
            session.get(Task, prior.task_id).status = "NEEDS_CLARIFICATION"
    with factory() as session:
        assert session.get(Task, prior.task_id).status == (
            "NEEDS_CLARIFICATION" if partial else "AWAITING_APPROVAL"
        )
    with factory() as session:
        assert session.get(Task, prior.task_id).order_id == "SO-1001"
    latest = receipt(factory, "new-order-answer", prior.task_id)
    with factory() as session:
        epoch = session.get(Task, prior.task_id).intent_revision
        assert session.get(Event, latest[0]).order_id == "SO-1001"
    new = run(factory, settings, output(20))
    assert new.status == "AWAITING_APPROVAL" and new.order_id is None
    assert new.intent_revision == epoch
    assert new.chain_id != prior.chain_id
    assert new.chain_id != "CH-SO-1001"
    before = snapshot(engine)
    approval = approve(factory, new)
    result = commit(factory, new, approval)
    after = snapshot(engine)
    assert len(after["sales_order"]) == len(before["sales_order"]) + 1
    assert result.order_id not in {"SO-1001", "SO-1002"}
    assert [r for r in after["sales_order"] if r["id"] in {"SO-1001", "SO-1002"}] == before[
        "sales_order"
    ]


def test_expired_worker_cannot_publish_after_recovery_and_fresh_answer(sandbox):  # noqa: F811
    from app.orchestration.state import guard
    from app.orchestration.worker import claim

    engine, factory, settings = sandbox
    first = receipt(factory)
    now = datetime.now(UTC)
    task_id, fence = claim(factory, "stale-worker", clock=lambda: now)
    later = now + timedelta(seconds=61)
    scan(factory, clock=lambda: later, settings=settings)
    recovered = run(factory, settings, output(), clock=lambda: later)
    receipt(factory, "second", task_id)
    before = snapshot(engine)
    task_token = bound_task_id.set(task_id)
    lease_token = guard.set((task_id, fence, lambda: now, 60))
    try:
        with pytest.raises(AppError) as exc:
            plan_new_order(
                factory,
                lines=output()["new_lines"],
                customer_name="华东服饰",
                client_event_key=first[0],
            )
        assert exc.value.code == "STALE_ATTEMPT"
    finally:
        guard.reset(lease_token)
        bound_task_id.reset(task_token)
    assert snapshot(engine) == before
    new = run(factory, settings, output(20), clock=lambda: later)
    assert new.intent_revision == 2 and new.chain_id == recovered.chain_id


def test_distinct_new_tasks_in_same_thread_have_distinct_owned_chains(sandbox):  # noqa: F811
    _, factory, settings = sandbox
    _, first, _ = initial(sandbox)
    receipt(factory, "other-new")
    second = run(factory, settings, output(20))
    assert first.task_id != second.task_id
    assert first.chain_id != second.chain_id
    assert first.chain_id != "create" and second.chain_id != "create"


def test_return_from_existing_order_scope_starts_fresh_create_chain(sandbox):  # noqa: F811
    from tests.test_late_bound_intent import payload

    _, factory, settings = sandbox
    _, first, _ = initial(sandbox)
    receipt(factory, "change", first.task_id)
    changed = run(factory, settings, payload())
    assert changed.chain_id == "CH-SO-1001"
    receipt(factory, "new-again", first.task_id)
    final = run(factory, settings, output(20))
    assert final.chain_id not in {first.chain_id, changed.chain_id, "create"}
    with pytest.raises(AppError):
        approve(factory, first)
    assert final.status == "AWAITING_APPROVAL"
    assert commit(factory, final, approve(factory, final)).order_id != "SO-1001"


@pytest.mark.parametrize("ambiguous", [False, True])
def test_legacy_multiple_chains_preserve_proven_current_or_fail_closed(sandbox, ambiguous):  # noqa: F811
    engine, factory, settings = sandbox
    _, old, _ = initial(sandbox)
    receipt(factory, "second", old.task_id)
    current = run(factory, settings, output(20))
    with factory() as session, begin_immediate(session):
        event = session.scalar(select(Event).where(Event.payload_ref == old.plan_id))
        event.chain_id = "CH-legacy-old"
        if ambiguous:
            session.get(Task, old.task_id).chain_id = "unknown-chain"
    receipt(factory, "third", old.task_id)
    before = business(snapshot(engine))
    if ambiguous:
        with pytest.raises(AppError) as exc:
            run(factory, settings, output(20))
        assert exc.value.code == "STALE_INTENT"
    else:
        newest = run(factory, settings, output(20))
        assert newest.chain_id == current.chain_id and newest.intent_revision == 3
    assert business(snapshot(engine)) == before


def test_legacy_prior_create_without_proven_origin_cannot_replan(sandbox):  # noqa: F811
    engine, factory, settings = sandbox
    _, old, _ = initial(sandbox)
    receipt(factory, "second", old.task_id)
    with factory() as session, begin_immediate(session):
        event = session.scalar(select(Event).where(Event.payload_ref == old.plan_id))
        event.payload_ref = None
    before = business(snapshot(engine))
    with pytest.raises(AppError) as exc:
        run(factory, settings, output(20))
    assert exc.value.code == "STALE_INTENT"
    assert business(snapshot(engine)) == before
