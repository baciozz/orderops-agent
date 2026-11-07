"""Current linked production revisions are required; history is not a substitute."""

import pytest
from app.config import Settings
from app.db.models import DocRevision, ProductionOrder
from app.db.session import begin_immediate, make_engine, make_session_factory
from app.services import kernel as service
from app.services import verification
from sqlalchemy import select
from tests import test_verification_mto_missing_lines as mto_support
from tests import test_verification_result_fencing as fencing
from tests.verification_scale import NOW

kernel = mto_support.kernel
mto = mto_support.mto
approved_commit = mto_support.approved_commit


def remove_current_snapshot(factory, doc_id="BM-0"):
    with factory() as session, begin_immediate(session):
        mo = session.get(ProductionOrder, doc_id)
        snapshot = session.scalar(
            select(DocRevision).where(
                DocRevision.doc_type == "production_order",
                DocRevision.doc_id == doc_id,
                DocRevision.rev_no == mo.doc_revision,
            )
        )
        saved = {
            column.name: getattr(snapshot, column.name) for column in DocRevision.__table__.columns
        }
        session.delete(snapshot)
        return saved


def cancel(factory):
    plan = mto_support.plan_cancel(factory)
    assert plan.status == "VALIDATED"
    approval = service.approve_plan(
        factory,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
        now=NOW,
    )
    return service.commit_plan(
        factory,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        approval_id=approval.approval_id,
        idempotency_key="snapshot-cancel",
        now=NOW,
        acknowledge=True,
    )


@pytest.mark.parametrize("state", ["NOT_STARTED", "PARTIALLY_CUT", "CUT", "CANCELLED"])
def test_missing_current_snapshot_is_detected_including_valid_cancellation(mto, state):
    if state == "CANCELLED":
        cancel(mto)
    else:
        mto_support.execution(mto, {"NOT_STARTED": 0, "PARTIALLY_CUT": 1, "CUT": 2}[state], 0)
    assert service.inspect_downstream(mto) == []
    saved = remove_current_snapshot(mto)
    before = mto_support.facts(mto)
    report = service.inspect_downstream(mto)
    assert len(report) == 1
    assert report[0] == {
        "doc_id": "BM-0",
        "problem": f"BM-0 当前单据修订 {saved['rev_no']} 缺失",
        "repair": {
            "action": "manual_review",
            "doc_type": "production_order",
            "doc_id": "BM-0",
            "doc_revision": saved["rev_no"],
            "order_id": "BENCH-0",
        },
    }
    assert mto_support.facts(mto) == before


def test_seed_mo_current_snapshot_loss_is_not_a_pass(kernel):
    saved = remove_current_snapshot(kernel, "MO-2002")
    assert saved["rev_no"] == 1
    with kernel() as session:
        assert service.consistency_problems(session) == []
        assert service.downstream_problems(session) == ["MO-2002 当前单据修订 1 缺失"]


@pytest.mark.parametrize("cancelled", [False, True])
def test_missing_old_snapshot_does_not_invalidate_current_document(mto, cancelled):
    if cancelled:
        cancel(mto)
    with mto() as session, begin_immediate(session):
        session.delete(session.get(DocRevision, "BD-BM-0-1"))
    assert service.inspect_downstream(mto) == []


@pytest.mark.parametrize("substitute", ["old_revision", "wrong_doc_type", "wrong_doc_id"])
def test_only_exact_current_production_snapshot_can_satisfy_check(mto, substitute):
    saved = remove_current_snapshot(mto)
    with mto() as session, begin_immediate(session):
        if substitute == "old_revision":
            session.get(DocRevision, "BD-BM-0-2").snapshot_json = saved["snapshot_json"]
        else:
            replacement = {**saved, "id": "misleading-snapshot"}
            replacement["doc_type" if substitute == "wrong_doc_type" else "doc_id"] = (
                "purchase_order" if substitute == "wrong_doc_type" else "BM-OTHER"
            )
            session.add(DocRevision(**replacement))
    assert [item["problem"] for item in service.inspect_downstream(mto)] == [
        "BM-0 当前单据修订 3 缺失"
    ]


@pytest.mark.parametrize("cancelled", [False, True])
def test_snapshot_loss_survives_retry_restart_and_manual_check_until_restored(
    mto, approved_commit, database_url, cancelled
):
    if cancelled:
        cancel(mto)
    operation_id = fencing.pending(mto, approved_commit[1])
    saved = remove_current_snapshot(mto)
    before = mto_support.facts(mto)
    service._post_verify(mto, operation_id)
    assert fencing.operation(mto, operation_id)["committed_verification_pending"]
    config = Settings(_env_file=None, verification_max_retries=1)
    assert verification.retry_pending(mto, operation_id, now=NOW, settings=config) == 1
    state = fencing.operation(mto, operation_id)
    assert state["verification_manual_required"] and state["committed_verification_pending"]
    assert mto_support.facts(mto) == before
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    try:
        assert not verification.verify_manually(factory, operation_id, now=NOW, settings=config)
        assert mto_support.facts(factory) == before
        # Restore the exact captured synthetic record, never fabricate it from current MO fields.
        with factory() as session, begin_immediate(session):
            session.add(DocRevision(**saved))
        repaired = mto_support.facts(factory)
        assert verification.verify_manually(factory, operation_id, now=NOW, settings=config)
        assert mto_support.facts(factory) == repaired
        done = fencing.operation(factory, operation_id)
        assert (
            not done["committed_verification_pending"] and not done["verification_manual_required"]
        )
        assert done["verification_retry_attempts"] == 1
        assert done["result_json"] == state["result_json"]
    finally:
        engine.dispose()
