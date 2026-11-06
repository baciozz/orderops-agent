"""Real fixture business journeys, injected loss, recovery, and stale publication."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, get_ident

import pytest
from app.config import Settings
from app.db.models import DownstreamLink, ExecutionEvidence, Operation, OrderLine, OrderRevision
from app.db.session import begin_immediate, make_engine, make_session_factory
from app.main import create_app
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE, TEXT_NEW_ORDER
from app.services import execution_audit, kernel, verification
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from tests import test_commit_verification_pending as business
from tests import test_link_audit as support
from tests import test_link_manifest_design as design
from tests import test_verification_result_fencing as fencing

seeded = support.seeded
NOW = fencing.NOW


def observation(factory, operation_id):
    with factory() as session:
        op = session.get(Operation, operation_id)
        return op.committed_verification_pending, op.verification_observation


def save_and_erase(factory, revision):
    with factory() as session:
        revision_id = session.scalar(
            select(OrderRevision.id).where(
                OrderRevision.order_id == "SO-1001", OrderRevision.revision_no == revision
            )
        )
        lines = [
            dict(r)
            for r in session.execute(
                select(OrderLine.__table__).where(
                    OrderLine.revision_id == revision_id, OrderLine.sku_id == "C-BLACK-L"
                )
            ).mappings()
        ]
        links = [
            dict(r)
            for r in session.execute(
                select(DownstreamLink.__table__).where(DownstreamLink.order_id == "SO-1001")
            ).mappings()
        ]
    design.erase_line_and_origin(factory, revision)
    return lines, links


def restore(factory, saved):
    with factory() as session, begin_immediate(session):
        for model, rows in zip((OrderLine, DownstreamLink), saved, strict=True):
            session.execute(insert(model), rows)


def dispatch_commit(client, text):
    planned = client.post("/dispatch", json={"text": text, "client_event_key": "guard-input"})
    assert planned.status_code == 200
    plan = planned.json()
    approval = client.post(
        f"/plans/{plan['plan_id']}/approve",
        json={
            "plan_hash": plan["plan_hash"],
            "expected_intent_revision": plan["intent_revision"],
            "acknowledge_duplicate": True,
        },
    )
    assert approval.status_code == 200
    body = {
        "plan_hash": plan["plan_hash"],
        "approval_id": approval.json()["approval_id"],
        "idempotency_key": "guard-commit",
    }
    url = f"/plans/{plan['plan_id']}/commit"
    result = client.post(url, json=body)
    assert result.status_code == 200
    return result.json(), url, body


@pytest.mark.parametrize("text", [TEXT_BLACK_TO_WHITE, TEXT_NEW_ORDER])
def test_normal_http_business_journey_reports_partial_never_global_pass(seeded, database_url, text):
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        result, url, body = dispatch_commit(client, text)
        view = client.get(f"/tasks/{result['task_id']}/view").json()
        assert view["card"]["status"] == "SUCCEEDED"
        assert view["result"]["verification"] == "partial"
        detail = view["result"]["verification_details"]
        assert detail["order_graph"] == "inconclusive"
        assert detail["global_completeness"] == "not_assessed"
        assert detail["execution"]["dependency_closure"] == "not_established"
        assert detail["execution"]["uncovered"]
        assert detail["authorizes_business_action"] is False
        page = client.get(f"/ui/tasks/{result['task_id']}")
        assert "限定字段一致；整单来源仍不确定" in page.text
        before = support.dump(database_url)
        assert client.post(url, json=body).json()["operation_id"] == result["operation_id"]
        assert support.dump(database_url) == before


def test_http_commit_detects_complete_loss_then_recovers_without_business_replay(
    seeded, database_url, monkeypatch
):
    original = kernel._post_verify
    saved = []

    def lose_before_initial_read(factory, operation_id):
        with factory() as session:
            revision = session.get(Operation, operation_id).result_json["revision"]
        saved.append(save_and_erase(factory, revision))
        original(factory, operation_id)

    monkeypatch.setattr(kernel, "_post_verify", lose_before_initial_read)
    config = Settings(_env_file=None, database_url=database_url, verification_max_retries=1)
    with TestClient(create_app(config), base_url="http://127.0.0.1") as client:
        result, url, body = dispatch_commit(client, TEXT_BLACK_TO_WHITE)
        op_id = result["operation_id"]
        pending, detail = observation(seeded, op_id)
        assert pending and detail["status"] == "findings"
        assert {f["field"] for f in detail["execution"]["fields"] if f["status"] == "findings"} == {
            "revision_lines",
            "operation_links",
        }
        with seeded() as session:
            assert kernel._verification_problems(session) == []  # Actual old blind spot.
        view = client.get(f"/tasks/{result['task_id']}/view").json()
        assert view["card"]["status"] == "SUCCEEDED"
        assert view["result"]["verification"] == "pending"
        page = client.get(f"/ui/tasks/{result['task_id']}").text
        assert "EXPECTED_LINK_MISSING" in page and "REVISION_LINES_MISMATCH" in page
        monkeypatch.setattr(kernel, "_post_verify", original)
        business_before = business._contents_except_verification_flag(seeded)
        assert verification.retry_pending(seeded, op_id, now=NOW, settings=config) == 1
        assert fencing.operation(seeded, op_id)["verification_manual_required"]
        assert business._contents_except_verification_flag(seeded) == business_before
        # Restore only exact synthetic originals. No repair is performed by verifier.
        restore(seeded, saved[0])
        engine = make_engine(database_url)
        try:
            restarted = make_session_factory(engine)
            before = business._contents_except_verification_flag(restarted)
            assert verification.verify_manually(restarted, op_id, now=NOW, settings=config)
            assert business._contents_except_verification_flag(restarted) == before
        finally:
            engine.dispose()
        assert observation(seeded, op_id)[1]["status"] == "partial"
        before = support.dump(database_url)
        assert client.post(url, json=body).json()["operation_id"] == op_id
        assert support.dump(database_url) == before


@pytest.mark.parametrize("fault", ["missing", "corrupt", "legacy"])
def test_unknown_evidence_cannot_clear_pending(seeded, fault):
    result = support.commit(seeded)
    with seeded() as session, begin_immediate(session):
        op = session.get(Operation, result.operation_id)
        op.committed_verification_pending = True
        evidence = session.get(ExecutionEvidence, op.id)
        if fault == "missing":
            session.delete(evidence)
        elif fault == "corrupt":
            evidence.digest = "sha256:" + "0" * 64
        else:
            op.execution_evidence_digest = op.execution_evidence_version = None
            session.delete(evidence)
    before = business._contents_except_verification_flag(seeded)
    kernel._post_verify(seeded, result.operation_id)
    pending, detail = observation(seeded, result.operation_id)
    assert pending and detail["status"] == "inconclusive"
    assert detail["execution"]["reason_code"]
    assert business._contents_except_verification_flag(seeded) == before


def test_unknown_parent_before_capture_is_still_explicitly_inconclusive(seeded, database_url):
    design.erase_line_and_origin(seeded, 1)
    result = support.commit(seeded)
    _, detail = observation(seeded, result.operation_id)
    assert detail["status"] == "partial" and detail["order_graph"] == "inconclusive"
    assert detail["execution"]["parent_graph"] == "not_bound_by_approval"
    assert detail["execution"]["global_completeness"] == "not_assessed"


def test_manual_check_reopens_settled_operation_and_detects_later_loss(seeded):
    result = support.commit(seeded)
    assert not observation(seeded, result.operation_id)[0]
    save_and_erase(seeded, result.revision)
    before = business._contents_except_verification_flag(seeded)
    assert not verification.verify_manually(seeded, result.operation_id, now=NOW)
    assert observation(seeded, result.operation_id)[1]["status"] == "findings"
    assert fencing.operation(seeded, result.operation_id)["verification_manual_required"]
    assert business._contents_except_verification_flag(seeded) == before


def test_legacy_false_flag_is_unassessed_and_manual_check_does_not_certify_it(seeded, database_url):
    result = support.commit(seeded)
    with seeded() as session, begin_immediate(session):
        op = session.get(Operation, result.operation_id)
        op.verification_observation = None
        op.execution_evidence_version = op.execution_evidence_digest = None
        session.delete(session.get(ExecutionEvidence, op.id))
    with TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)), base_url="http://127.0.0.1"
    ) as client:
        view = client.get(f"/tasks/{result.task_id}/view").json()
        assert view["result"]["verification"] == "unassessed"
        assert view["result"]["verification_details"] is None
    assert not verification.verify_manually(seeded, result.operation_id, now=NOW)
    assert observation(seeded, result.operation_id)[1]["status"] == "inconclusive"


@pytest.mark.parametrize("takeover", [False, True])
def test_old_success_cannot_hide_complete_loss_or_new_owner_failure(seeded, monkeypatch, takeover):
    marker = kernel.verify_failpoint.set(True)
    try:
        result = support.commit(seeded)
    finally:
        kernel.verify_failpoint.reset(marker)
    arrived, release = Event(), Event()
    old_thread = []
    original = execution_audit.audit_in_session
    current = [NOW]
    config = Settings(_env_file=None, verification_lease_seconds=10, verification_max_retries=3)

    def paused(session, op_id):
        report = original(session, op_id)
        if old_thread and get_ident() == old_thread[0] and not arrived.is_set():
            assert report.status == "consistent_for_retained_fields"
            arrived.set()
            assert release.wait(15)
        return report

    monkeypatch.setattr(execution_audit, "audit_in_session", paused)

    def old():
        old_thread.append(get_ident())
        verification.retry_pending(
            seeded, result.operation_id, settings=config, clock=lambda: current[0]
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(old)
        try:
            assert arrived.wait(10)
            save_and_erase(seeded, result.revision)
            if takeover:
                current[0] += timedelta(seconds=11)
                assert (
                    verification.retry_pending(
                        seeded, result.operation_id, settings=config, clock=lambda: current[0]
                    )
                    == 1
                )
                newer = fencing.operation(seeded, result.operation_id)
                assert newer["verification_observation"]["status"] == "findings"
        finally:
            release.set()
        future.result(timeout=10)
    pending, detail = observation(seeded, result.operation_id)
    assert pending and detail["status"] == "findings"
    if takeover:
        assert fencing.operation(seeded, result.operation_id) == newer
