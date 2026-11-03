"""Separate interpreters race on file SQLite; no shared Python locks or engines.

Run the same module as a child with this worktree's venv interpreter. Files form
bounded start/takeover barriers, so the tests also work without socket IPC.
"""

import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.adapters.budget import BudgetLedger
from app.config import Settings
from app.db.models import (
    Approval,
    ConsumerReceipt,
    DispatchStep,
    Event,
    InboxDelivery,
    JobAttempt,
    ModelUsage,
    Operation,
    OutboxDelivery,
    Plan,
    Reservation,
    Sku,
    StockBalance,
    StockMovement,
    Task,
)
from app.db.session import begin_immediate, make_engine, make_session_factory
from app.errors import AppError, BudgetExceeded
from app.orchestration.fixtures import TEXT_NEW_ORDER, golden_fixtures
from app.orchestration.inbox import accept_input
from app.orchestration.outbox import publish_pending
from app.orchestration.state import guard
from app.orchestration.watchdog import scan
from app.orchestration.worker import claim, heartbeat, run_once
from app.services.kernel import approve_plan, commit_plan, consistency_problems, plan_new_order
from app.services.seed import load_seed
from sqlalchemy import func, select

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]
PROCESSES = 4
TASKS = 6


def wait_for(path, *, deadline):
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {path.name}")
        time.sleep(0.005)


def start_children(tmp_path, url, mode, *, count=PROCESSES):
    env = {key: value for key, value in os.environ.items() if not key.startswith("OOS_")}
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "tests.test_multiprocess", mode, url, str(tmp_path), str(index)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(count)
    ]
    return processes


def collect_children(processes, *, deadline):
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=max(0.1, deadline - time.monotonic()))
            assert process.returncode == 0, f"child failed:\n{stderr}\n{stdout}"
            assert "database is locked" not in stderr.lower()
            results.append(json.loads(stdout))
        return results
    finally:
        stop_children(processes)


def stop_children(processes):
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)


def release_children(tmp_path, processes, *, deadline):
    try:
        for index, process in enumerate(processes):
            wait_for(tmp_path / f"ready-{index}", deadline=deadline)
            assert process.poll() is None, "child exited before start barrier"
        (tmp_path / "start").touch()
    except BaseException:
        stop_children(processes)
        raise


def synthetic_fixtures():
    fixtures = {}
    for index in range(TASKS):
        payload = golden_fixtures()[TEXT_NEW_ORDER]
        payload["business_event"]["new_lines"][0]["sku_id"] = f"MP-{index}"
        payload["business_event"]["new_lines"][0]["qty"] = 1
        payload["new_lines"][0]["sku_id"] = f"MP-{index}"
        payload["new_lines"][0]["qty"] = 1
        fixtures[f"new order {index}"] = payload
    return fixtures


def test_process_workers_commit_each_task_once(session_factory, database_url, tmp_path):
    load_seed(session_factory, database_url=database_url)
    with session_factory() as session, begin_immediate(session):
        for index in range(TASKS):
            session.add(
                Sku(
                    id=f"MP-{index}",
                    style=f"MP-{index}",
                    color="white",
                    size="M",
                    default_fulfillment_mode="STOCK",
                )
            )
        session.flush()
        for index in range(TASKS):
            session.add(
                StockBalance(
                    warehouse_id="W1", sku_id=f"MP-{index}", on_hand=10, reserved=0, revision=1
                )
            )
            session.add(
                StockMovement(
                    id=f"MP-receipt-{index}",
                    operation_id=f"MP-receipt-{index}",
                    seq=1,
                    movement_type="RECEIPT",
                    warehouse_id="W1",
                    sku_id=f"MP-{index}",
                    delta_on_hand=10,
                    delta_reserved=0,
                    before_revision=0,
                    after_revision=1,
                    at=NOW,
                )
            )
    task_ids = {
        accept_input(
            session_factory,
            thread_id=f"mp-{index}",
            client_event_key=f"mp-{index}",
            text=f"new order {index}",
            clock=lambda: NOW,
        )[1]
        for index in range(TASKS)
    }
    deadline = time.monotonic() + 15
    processes = start_children(tmp_path, database_url, "worker")
    release_children(tmp_path, processes, deadline=deadline)
    results = collect_children(processes, deadline=deadline)
    handled = [task_id for result in results for task_id in result["handled"]]
    assert Counter(handled) == Counter(dict.fromkeys(task_ids, 1))
    with session_factory() as session:
        tasks = list(session.scalars(select(Task).where(Task.id.in_(task_ids))))
        assert {task.status for task in tasks} == {"SUCCEEDED"}
        assert len({task.result_ref for task in tasks}) == TASKS
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == TASKS
        assert session.scalar(select(func.count()).select_from(Plan)) == TASKS
        assert session.scalar(select(func.count()).select_from(Operation)) == TASKS
        assert session.scalar(select(func.count()).select_from(Approval)) == TASKS
        assert set(session.scalars(select(Approval.status))) == {"CONSUMED"}
        assert session.scalar(select(func.count()).select_from(OutboxDelivery)) == TASKS
        assert set(session.scalars(select(OutboxDelivery.delivery_state))) == {"DELIVERED"}
        assert set(session.scalars(select(InboxDelivery.delivery_state))) == {"DELIVERED"}
        assert session.scalar(select(func.count()).select_from(ConsumerReceipt)) == TASKS
        assert session.scalar(select(func.count()).select_from(Reservation)) == TASKS + 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(StockMovement)
                .where(StockMovement.sku_id.like("MP-%"))
            )
            == 2 * TASKS
        )
        assert consistency_problems(session) == []
        for task in tasks:
            plan = session.scalars(select(Plan).where(Plan.task_id == task.id)).one()
            operation = session.scalars(select(Operation).where(Operation.plan_id == plan.id)).one()
            assert task.result_ref == operation.id
            assert (
                session.scalar(
                    select(func.count()).select_from(Event).where(Event.payload_ref == operation.id)
                )
                == 1
            )


def test_process_budget_reservations_respect_exact_cap(session_factory, database_url, tmp_path):
    # Each of four processes tries three calls; only three one-dollar slots fit.
    deadline = time.monotonic() + 10
    processes = start_children(tmp_path, database_url, "budget")
    release_children(tmp_path, processes, deadline=deadline)
    results = collect_children(processes, deadline=deadline)
    assert sum(result["allowed"] for result in results) == 3
    assert sum(result["denied"] for result in results) == 9
    with session_factory() as session:
        rows = list(session.scalars(select(ModelUsage)))
        assert len(rows) == 3
        assert {row.status for row in rows} == {"RESERVED"}
        assert sum((row.cost_usd for row in rows), Decimal(0)) == Decimal("3.000000")


def test_process_stale_worker_cannot_write_after_takeover(session_factory, database_url, tmp_path):
    load_seed(session_factory, database_url=database_url)
    task_id = accept_input(
        session_factory,
        thread_id="stale",
        client_event_key="stale",
        text=TEXT_NEW_ORDER,
        clock=lambda: NOW,
    )[1]
    deadline = time.monotonic() + 10
    processes = start_children(tmp_path, database_url, "stale", count=1)
    release_children(tmp_path, processes, deadline=deadline)
    try:
        wait_for(tmp_path / "old-worker-paused", deadline=deadline)
        future = NOW + timedelta(seconds=61)
        assert scan(session_factory, clock=lambda: future) == [task_id]
        assert claim(session_factory, "replacement", clock=lambda: future) == (task_id, 2)
        assert heartbeat(session_factory, task_id, 2, clock=lambda: future)
        (tmp_path / "replacement-claimed").touch()
        results = collect_children(processes, deadline=deadline)
    finally:
        stop_children(processes)
    assert results == [{"error": "STALE_ATTEMPT"}]
    with session_factory() as session:
        assert session.get(Task, task_id).status == "RUNNING"
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 2
        for model in (DispatchStep, Plan, Approval, Operation, OutboxDelivery, ConsumerReceipt):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert consistency_problems(session) == []


def child_work(factory):
    handled = []
    settings = Settings(_env_file=None)
    deadline = time.monotonic() + 10
    while True:
        assert time.monotonic() < deadline, "worker did not drain queue"
        outcome = run_once(
            factory,
            owner=f"process-{os.getpid()}",
            clock=lambda: NOW,
            fixtures=synthetic_fixtures(),
            settings=settings,
        )
        if outcome is None:
            with factory() as session:
                remaining = session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.status.in_(["QUEUED", "RUNNING"]))
                )
            if not remaining:
                break
            time.sleep(0.005)
            continue
        assert outcome.status == "AWAITING_APPROVAL", outcome
        # The worker stops for human approval. This test driver is the demo human;
        # it must use all normal approval/hash/intent/inventory/version checks.
        approval = approve_plan(
            factory,
            plan_id=outcome.plan_id,
            plan_hash=outcome.plan_hash,
            expected_intent_revision=outcome.intent_revision,
            now=NOW,
        )
        request = dict(
            plan_id=outcome.plan_id,
            approval_id=approval.approval_id,
            plan_hash=outcome.plan_hash,
            idempotency_key=f"commit-{outcome.task_id}",
            now=NOW,
        )
        committed = commit_plan(factory, **request)
        replay = commit_plan(factory, **request)
        assert replay.replayed and replay.operation_id == committed.operation_id
        handled.append(outcome.task_id)
    publish_pending(factory, clock=lambda: NOW)
    return {"handled": handled}


def child_budget(factory):
    book = BudgetLedger(
        factory,
        cap_usd=Decimal("3"),
        price_in_per_mtok=Decimal("1000000"),
        price_out_per_mtok=Decimal("0"),
    )
    allowed = denied = 0
    for _ in range(3):
        try:
            book.reserve(provider="local", model="fixture", input_tokens=1, max_output_tokens=0)
        except BudgetExceeded:
            denied += 1
        else:
            allowed += 1
    return {"allowed": allowed, "denied": denied}


def child_stale(factory, root):
    def takeover(_ctx):
        (root / "old-worker-paused").touch()
        wait_for(root / "replacement-claimed", deadline=time.monotonic() + 8)

    try:
        run_once(
            factory, clock=lambda: NOW, before_step=takeover, settings=Settings(_env_file=None)
        )
    except AppError as exc:
        assert guard.get() is None
        assert exc.code == "STALE_ATTEMPT"
        # Try a business write with the stale guard as well: the kernel must
        # reject it before a plan, event, approval, operation or outbox is added.
        with factory() as session:
            task_id = session.scalar(select(Task.id))
        token = guard.set((task_id, 1, lambda: NOW, 60))
        try:
            try:
                plan_new_order(
                    factory,
                    lines=[{"sku_id": "A-WHITE-M", "qty": 1}],
                    customer_name="华东服饰",
                    client_event_key="stale-business-write",
                    now=NOW,
                )
            except AppError as stale:
                assert stale.code == "STALE_ATTEMPT"
            else:
                raise AssertionError("stale business write was accepted")
        finally:
            guard.reset(token)
        return {"error": exc.code}
    raise AssertionError("old worker was not fenced")


def child_main():
    mode, url, root, index = sys.argv[1:]
    root = Path(root)
    # Children do not load dotenv or inherit pytest's settings monkeypatches.
    Settings.model_config["env_file"] = None
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        # Establish each independent SQLite connection before the start barrier.
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
        (root / f"ready-{index}").touch()
        wait_for(root / "start", deadline=time.monotonic() + 8)
        if mode == "worker":
            result = child_work(factory)
        elif mode == "budget":
            result = child_budget(factory)
        else:
            assert mode == "stale"
            result = child_stale(factory, root)
        print(json.dumps(result))
    finally:
        engine.dispose()


if __name__ == "__main__":
    child_main()
