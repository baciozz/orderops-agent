"""Direct route retry identity, without HTTP servers or network access."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from types import SimpleNamespace

import pytest
from app.api.orders import create_plan
from app.db.models import Base, Event, Plan
from app.db.schema import init_db
from app.db.session import make_engine, make_session_factory
from app.errors import AppError
from app.services.kernel import approve_plan, commit_plan, plan_change, plan_new_order
from app.services.seed import load_seed
from schemas.api import PlanChangeRequest
from sqlalchemy import select


@pytest.fixture()
def seeded(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    return session_factory


@pytest.fixture()
def concurrent_seeded(tmp_path):
    # Race tests exercise production lock waiting, not the shared 200 ms denial fixture.
    url = f"sqlite:///{tmp_path / 'direct-plan-race.db'}"
    engine = make_engine(url)
    try:
        init_db(engine)
        factory = make_session_factory(engine)
        load_seed(factory, database_url=url)
        yield factory
    finally:
        engine.dispose()


def body(**updates):
    return {
        "order_id": "SO-1001",
        "base_revision": 1,
        "intent_revision": 1,
        "changes": [
            {"mode": "replace_remaining", "from_sku": "A-BLACK-M", "to_sku": "A-WHITE-M", "qty": 40}
        ],
        "client_event_key": "direct-test",
        **updates,
    }


def route(factory, data):
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_factory=factory)),
        state=SimpleNamespace(request_id="test", correlation_id="test"),
    )
    return create_plan(PlanChangeRequest.model_validate(data), request)


def snapshot(factory):
    with factory() as session:
        return {
            table.name: sorted(repr(tuple(row)) for row in session.execute(select(table)))
            for table in Base.metadata.sorted_tables
        }


def conflict(factory, data):
    before = snapshot(factory)
    with pytest.raises(AppError) as caught:
        route(factory, data)
    assert (caught.value.code, caught.value.http_status) == ("PLAN_REQUEST_CONFLICT", 409)
    assert snapshot(factory) == before


@pytest.mark.parametrize(
    "update",
    [
        {"order_id": "SO-NOT-FOUND"},
        {"base_revision": 2},
        {"intent_revision": 2},
        {
            "changes": [
                {
                    "mode": "replace_remaining",
                    "from_sku": "A-BLACK-M",
                    "to_sku": "A-WHITE-M",
                    "qty": 10,
                }
            ]
        },
        {"source_refs": [{"source_id": "new-evidence"}]},
    ],
)
def test_changed_request_is_read_only_conflict(seeded, update):
    plan = route(seeded, body())
    approve_plan(
        seeded,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
    )
    conflict(seeded, body(**update))


def test_exact_replay_before_and_after_commit(seeded):
    original = route(seeded, body())
    before = snapshot(seeded)
    numeric = body(base_revision="1", intent_revision="1")
    numeric["changes"][0]["qty"] = "40"
    assert route(seeded, numeric) == original
    assert snapshot(seeded) == before
    approval = approve_plan(
        seeded,
        plan_id=original.plan_id,
        plan_hash=original.plan_hash,
        expected_intent_revision=original.intent_revision,
    )
    assert route(seeded, body()).plan_id == original.plan_id
    commit_plan(
        seeded,
        plan_id=original.plan_id,
        plan_hash=original.plan_hash,
        approval_id=approval.approval_id,
        idempotency_key="commit-direct",
    )
    before = snapshot(seeded)
    replay = route(seeded, body())
    assert (replay.plan_id, replay.plan_hash, replay.intent_revision) == (
        original.plan_id,
        original.plan_hash,
        original.intent_revision,
    )
    assert replay.status == "COMMITTED"
    assert not replay.requires_human_approval
    assert snapshot(seeded) == before


def test_default_key_and_new_key_invalidation(seeded):
    data = body(client_event_key=None)
    original = route(seeded, data)
    assert route(seeded, body(client_event_key="plan-SO-1001-1")) == original
    changed = deepcopy(data)
    changed["changes"][0]["qty"] = 10
    conflict(seeded, changed)
    route(seeded, body(client_event_key="next", intent_revision=original.intent_revision))
    replay = route(seeded, data)
    assert replay.plan_id == original.plan_id
    assert replay.status == "INVALIDATED"


@pytest.mark.parametrize("legacy", ["change", "new", "event"])
def test_legacy_unbound_and_non_plan_records_fail_closed(seeded, legacy):
    if legacy == "change":
        plan_change(seeded, **body())
    elif legacy == "new":
        plan_new_order(
            seeded,
            lines=[{"sku_id": "A-WHITE-M", "qty": 10}],
            customer_code="HD",
            client_event_key="direct-test",
        )
    else:
        plan = route(seeded, body())
        with seeded.begin() as session:
            event = session.scalar(select(Event).where(Event.payload_ref == plan.plan_id))
            event.payload_ref = "diagnostic-not-a-plan"
    conflict(seeded, body())


def test_kernel_cannot_reuse_direct_bound_key(seeded):
    route(seeded, body())
    before = snapshot(seeded)
    for call in [
        lambda: plan_change(seeded, **body()),
        lambda: plan_new_order(seeded, lines=[], client_event_key="direct-test"),
    ]:
        with pytest.raises(AppError, match="事件键"):
            call()
        assert snapshot(seeded) == before


def test_competing_request_has_one_winner(concurrent_seeded):
    seeded = concurrent_seeded
    ready = Barrier(2)

    def run(qty):
        data = body()
        data["changes"][0]["qty"] = qty
        # Rendezvous before route() creates a session or takes a database lock.
        ready.wait(timeout=10)
        try:
            return route(seeded, data)
        except AppError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, [10, 40]))
    assert sum(isinstance(result, AppError) for result in results) == 1
    loser = next(result for result in results if isinstance(result, AppError))
    assert (loser.code, loser.http_status) == ("PLAN_REQUEST_CONFLICT", 409)
    winner = next(result for result in results if not isinstance(result, AppError))
    with seeded() as session:
        assert [plan.id for plan in session.scalars(select(Plan))] == [winner.plan_id]
        events = session.scalars(select(Event)).all()
        assert len(events) == 1
        assert events[0].source_event_key == "direct-test"
        assert events[0].payload_ref == winner.plan_id
    before = snapshot(seeded)
    for qty, result in zip([10, 40], results, strict=True):
        data = body()
        data["changes"][0]["qty"] = qty
        if isinstance(result, AppError):
            conflict(seeded, data)
        else:
            assert route(seeded, data) == winner
        assert snapshot(seeded) == before


@pytest.mark.parametrize("kind", ["shortage", "clarify"])
def test_persisted_business_results_are_bound(seeded, kind):
    data = body()
    data["changes"] = {
        "shortage": [{"mode": "increment", "sku_id": "A-WHITE-M", "qty": 1000}],
        "clarify": [
            {"mode": "replace_remaining", "from_sku": "A-BLACK-M", "to_sku": "A-GOLD-M", "qty": 10}
        ],
        "reject": [{"mode": "increment", "sku_id": "A-WHITE-M", "qty": -1}],
    }[kind]

    def attempt():
        try:
            result = route(seeded, data)
            return result.plan_id, result.status
        except AppError as error:
            return error.code, error.http_status

    first = attempt()
    before = snapshot(seeded)
    assert attempt() == first
    assert snapshot(seeded) == before
    with seeded() as session:
        assert len(session.scalars(select(Event)).all()) == 1
    conflict(seeded, body())


def test_failure_without_event_does_not_reserve_key(seeded):
    before = snapshot(seeded)
    with pytest.raises(AppError):
        route(seeded, body(base_revision=999))
    assert snapshot(seeded) == before
    assert route(seeded, body()).plan_id


def test_simultaneous_exact_retry_returns_same_plan(concurrent_seeded):
    seeded = concurrent_seeded
    ready = Barrier(2)

    def run(_):
        # No session or write lock exists when either worker reaches this barrier.
        ready.wait(timeout=10)
        return route(seeded, body())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, range(2)))
    assert results[0] == results[1]
    with seeded() as session:
        assert [plan.id for plan in session.scalars(select(Plan))] == [results[0].plan_id]
        events = session.scalars(select(Event)).all()
        assert len(events) == 1
        assert events[0].source_event_key == "direct-test"
        assert events[0].payload_ref == results[0].plan_id
    before = snapshot(seeded)
    assert route(seeded, body()) == results[0]
    assert snapshot(seeded) == before


def test_diagnostic_only_failure_fails_closed_on_retry(seeded):
    data = body(changes=[{"mode": "increment", "sku_id": "A-WHITE-M", "qty": -1}])
    with pytest.raises(AppError) as caught:
        route(seeded, data)
    assert caught.value.http_status == 422
    with seeded() as session:
        assert len(session.scalars(select(Event)).all()) == 1
        assert not session.scalars(select(Plan)).all()
    conflict(seeded, data)
    conflict(seeded, body())
    # A deliberate new event uses the current intent, rather than silently reusing the old one.
    from app.db.models import SalesOrder

    with seeded() as session:
        intent = session.get(SalesOrder, "SO-1001").intent_revision
    assert route(seeded, body(client_event_key="after-diagnostic", intent_revision=intent)).plan_id


def test_source_defaults_normalize_but_evidence_order_is_identity(seeded):
    refs = [{"source_id": "one"}, {"source_id": "two"}]
    original = route(seeded, body(source_refs=refs))
    explicit = PlanChangeRequest.model_validate(body(source_refs=refs)).model_dump(mode="json")
    assert route(seeded, explicit) == original
    conflict(seeded, body(source_refs=list(reversed(refs))))
    conflict(seeded, body(source_refs=[{"source_id": "one", "text_excerpt": "changed"}, refs[1]]))


def test_missing_binding_version_is_not_trusted(seeded):
    original = route(seeded, body())
    with seeded.begin() as session:
        plan = session.get(Plan, original.plan_id)
        metadata = deepcopy(plan.normalized_changes)
        metadata["direct_request_binding"].pop("version")
        plan.normalized_changes = metadata
    conflict(seeded, body())
