"""Executable scope contract: synthetic witnesses, no runtime rollout or migration."""

import json
import sqlite3
from dataclasses import asdict

import pytest
from tests import manifest_lineage_model as lineage
from tests import scoped_verification_model as scope


@pytest.fixture()
def scoped(tmp_path):
    path = tmp_path / "scope.db"
    return path, scope.seed_scope(path)


def report(scoped, requested=("A",), **options):
    return scope.observe(*scoped[:1], requested, scoped[1], **options)


def dump(path):
    with lineage.connection(path) as db:
        return "\n".join(db.iterdump())


def test_independent_unknown_order_does_not_prevent_explicit_limited_conclusion(scoped):
    before = dump(scoped[0])
    result = report(scoped)
    assert result.covered == ("A",)
    assert result.coverage == "complete_for_requested"
    assert result.outside_request == ("B", "LEGACY")
    assert result.unassessed_objects == (
        ("B", "OUTSIDE_REQUEST_AND_DEPENDENCIES"),
        ("LEGACY", "OUTSIDE_REQUEST_AND_DEPENDENCIES"),
    )
    assert result.dependencies == ("doc:production_order:MO-A", "stock:S-A")
    assert result.global_completeness == "not_assessed"
    assert result.inventory_basis == "observed_catalog_only"
    assert result.authorizes_business_action is False
    assert dump(scoped[0]) == before


def test_report_keeps_unknown_requested_object_and_reason(scoped):
    result = report(scoped, ("LEGACY", "A"))
    assert result.coverage == "partial_for_requested"
    assert result.covered == ("A",)
    unknown = result.targets[1]
    assert unknown.order == "LEGACY" and unknown.evidence_digest is None
    assert set(unknown.reasons) == {
        "order:LEGACY:missing_head",
        "order:LEGACY:DEPENDENCY_DECLARATION_UNKNOWN",
    }
    assert result.requested == ("A", "LEGACY")


@pytest.mark.parametrize("requested", [(), ("missing",), ("LEGACY",)])
def test_empty_or_unknown_scope_is_not_vacuous_success(scoped, requested):
    result = report(scoped, requested)
    assert result.coverage == "none" and result.covered == ()
    assert result.global_completeness == "not_assessed"


@pytest.mark.parametrize("fault", ["parent", "approval", "old_root", "cancel", "claim"])
def test_narrowing_scope_never_promotes_unknown_parent_or_cancel_evidence(scoped, fault):
    path, witnesses = scoped
    with lineage.connection(path) as db:
        if fault == "parent":
            db.execute("DELETE FROM node WHERE id='A1'")
        elif fault == "approval":
            del witnesses.lineage.authorized_manifests["A1"]
        elif fault == "old_root":
            db.execute("UPDATE root SET state='REVOKED', epoch=2 WHERE id='R-A' AND generation=1")
        elif fault == "cancel":
            db.execute("DELETE FROM cancel")
        else:
            db.execute("UPDATE claim SET owner='LEGACY'")
    narrow = report(scoped)
    broad = report(scoped, ("A", "B"))
    assert narrow.coverage == "none"
    assert broad.covered == ("B",)
    assert narrow.targets[0] == broad.targets[0]


@pytest.mark.parametrize("known_membership", [False, True])
def test_unknown_shared_inventory_contributor_taints_only_dependent_targets(
    scoped, known_membership
):
    path, witnesses = scoped
    with lineage.connection(path) as db:
        db.execute("INSERT INTO member VALUES ('stock:S-A','LEGACY')")
    if known_membership:
        # Even an independently witnessed membership does not grant a parent root.
        witnesses.members["stock:S-A"].add("LEGACY")
        witnesses.dependencies["LEGACY"] = {"stock:S-A"}
    result = report(scoped, ("A", "B"))
    assert result.covered == ("B",)
    assert "order:LEGACY" in result.dependencies
    assert "order:LEGACY:missing_head" in result.targets[0].reasons
    assert "LEGACY" in result.outside_request  # Assessed dependency, not requested target.


def test_trusted_shared_dependency_closure_is_explicit_and_bound(scoped):
    path, witnesses = scoped
    with lineage.connection(path) as db:
        db.execute("INSERT INTO member VALUES ('stock:S-A','B')")
    witnesses.members["stock:S-A"].add("B")
    witnesses.dependencies["B"].add("stock:S-A")
    first = report(scoped)
    assert first.covered == ("A",)
    assert "order:B" in first.dependencies and "stock:S-B" in first.dependencies
    assert ("B", "OUTSIDE_REQUEST_AND_DEPENDENCIES") not in first.unassessed_objects
    with lineage.connection(path) as db:
        db.execute("UPDATE root SET epoch=epoch+1 WHERE id='R-B' AND generation=1")
    second = report(scoped)
    assert second.covered == ("A",)
    assert first.targets[0].evidence_digest != second.targets[0].evidence_digest


def test_cross_scope_document_link_conflict_is_not_hidden_by_correct_claim(scoped):
    path, _ = scoped
    with lineage.connection(path) as db:
        # Claim still says A. The global fact link must also be inspected.
        db.execute("INSERT INTO member VALUES ('doc:production_order:MO-A','LEGACY')")
    result = report(scoped)
    assert "doc:production_order:MO-A:OWNERSHIP_CONFLICT" in result.targets[0].reasons
    assert result.covered == ()


@pytest.mark.parametrize("fault", ["declaration", "membership", "fact", "both_rows", "extra"])
def test_missing_and_extra_dependencies_do_not_turn_into_empty_scope(scoped, fault):
    path, witnesses = scoped
    with lineage.connection(path) as db:
        if fault == "declaration":
            del witnesses.dependencies["A"]
        elif fault == "membership":
            del witnesses.members["stock:S-A"]
        elif fault == "fact":
            db.execute("UPDATE fact SET digest='wrong' WHERE id='stock:S-A'")
        elif fault == "both_rows":
            db.execute("DELETE FROM fact WHERE id='stock:S-A'")
            db.execute("DELETE FROM member WHERE resource='stock:S-A'")
        else:
            db.execute("INSERT INTO member VALUES ('stock:unexpected','A')")
    result = report(scoped)
    assert not result.covered and result.targets[0].reasons


def test_bound_limit_reports_incomplete_closure(scoped):
    result = report(scoped, limit=1)
    assert result.coverage == "none"
    assert "CLOSURE_LIMIT" in result.targets[0].reasons


def test_same_snapshot_old_success_is_rechecked_before_publication(scoped):
    path, witnesses = scoped
    with lineage.connection(path) as reader:
        reader.execute("BEGIN")
        assert scope.assess(reader, ("A",), witnesses).covered == ("A",)
        with lineage.connection(path) as writer:
            writer.execute("DELETE FROM fact WHERE id='stock:S-A'")
        # WAL permits this old result; it cannot be accepted for publication.
        assert scope.assess(reader, ("A",), witnesses).covered == ("A",)
    published = scope.publish_probe(path, ("A",), witnesses, token=1, clock=lambda: 10)
    assert published.coverage == "none"


def test_old_success_cannot_overwrite_new_owners_failure(scoped):
    path, witnesses = scoped
    old = report(scoped)
    assert old.covered == ("A",)
    with lineage.connection(path) as db:
        db.execute("DELETE FROM fact WHERE id='stock:S-A'")
        db.execute("UPDATE publication SET token=2, until=200")
    failed = scope.publish_probe(path, ("A",), witnesses, token=2, clock=lambda: 110)
    assert failed.coverage == "none"
    before = dump(path)
    with pytest.raises(lineage.ProofFailure, match="STALE_RESULT_OWNER"):
        scope.publish_probe(path, ("A",), witnesses, token=1, clock=lambda: 110)
    assert dump(path) == before


def test_lease_expiring_during_closure_check_cannot_publish(scoped):
    path, witnesses = scoped
    now = [99]
    before = dump(path)
    with pytest.raises(lineage.ProofFailure, match="STALE_RESULT_OWNER"):
        scope.publish_probe(
            path,
            ("A",),
            witnesses,
            token=1,
            clock=lambda: now[0],
            after_check=lambda: now.__setitem__(0, 100),
        )
    assert dump(path) == before


def test_recovery_requires_exact_retained_evidence_and_never_repairs_facts(scoped):
    path, witnesses = scoped
    before = report(scoped)
    with lineage.connection(path) as db:
        original = db.execute("SELECT * FROM fact WHERE id='stock:S-A'").fetchone()
        db.execute("DELETE FROM fact WHERE id='stock:S-A'")
    damaged = dump(path)
    assert report(scoped).covered == ()
    assert report(scoped).covered == ()
    assert dump(path) == damaged
    with lineage.connection(path) as db:
        db.execute("INSERT INTO fact VALUES (?,?)", original)
    assert report(scoped) == before


def test_publication_is_separate_from_business_marker_and_global_pending(scoped):
    path, witnesses = scoped
    result = scope.publish_probe(path, ("A",), witnesses, token=1, clock=lambda: 10)
    with lineage.connection(path) as db:
        assert db.execute("SELECT * FROM effect").fetchall() == []
        saved = json.loads(db.execute("SELECT report FROM publication").fetchone()[0])
    assert saved == json.loads(json.dumps(asdict(result)))
    assert "pending" not in saved and "business_allowed" not in saved
    assert saved["authorizes_business_action"] is False


def test_even_all_observed_orders_covered_is_not_whole_history_proof(scoped):
    path, _ = scoped
    with lineage.connection(path) as db:
        # A missing catalog entry is not evidence that no such historical order existed.
        db.execute("DELETE FROM catalog WHERE id='LEGACY'")
    result = report(scoped, ("A", "B"))
    assert result.coverage == "complete_for_requested" and result.outside_request == ()
    assert result.global_completeness == "not_assessed"


@pytest.mark.parametrize("change", ["root_epoch", "shared_member", "fact", "missing_parent"])
def test_explicit_approval_rechecks_entire_closure_inside_business_transaction(scoped, change):
    path, witnesses = scoped
    approved = scope.approve_probe(path, "A", witnesses)
    with lineage.connection(path) as db:
        if change == "root_epoch":
            db.execute("UPDATE root SET epoch=epoch+1 WHERE id='R-A' AND generation=1")
        elif change == "shared_member":
            db.execute("INSERT INTO member VALUES ('stock:S-A','B')")
            witnesses.dependencies["B"].add("stock:S-A")
            witnesses.members["stock:S-A"].add("B")
        elif change == "fact":
            db.execute("UPDATE fact SET digest='changed' WHERE id='stock:S-A'")
        else:
            db.execute("DELETE FROM node WHERE id='A1'")
    before = dump(path)
    with pytest.raises(lineage.ProofFailure, match="STALE_OR_UNPROVEN_DEPENDENCIES"):
        scope.commit_probe(path, approved, witnesses)
    assert dump(path) == before


def test_separate_approval_can_cover_independent_order_but_cannot_approve_unknown(scoped):
    path, witnesses = scoped
    approved = scope.approve_probe(path, "A", witnesses)
    with pytest.raises(lineage.ProofFailure, match="UNPROVEN_DEPENDENCIES"):
        scope.approve_probe(path, "LEGACY", witnesses)
    scope.commit_probe(path, approved, witnesses)
    with lineage.connection(path) as db:
        assert db.execute("SELECT owner FROM effect").fetchall() == [("A",)]


def test_concurrent_closure_mutation_cannot_enter_check_write_window(scoped):
    path, witnesses = scoped
    approved = scope.approve_probe(path, "A", witnesses)

    def compete():
        with lineage.connection(path, timeout=0.01) as writer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.execute("DELETE FROM member WHERE resource='stock:S-A'")

    scope.commit_probe(path, approved, witnesses, after_check=compete)


def test_explicit_approval_interruption_does_not_leave_effect(scoped):
    path, witnesses = scoped
    approved = scope.approve_probe(path, "A", witnesses)
    before = dump(path)

    def crash():
        raise RuntimeError("synthetic interruption")

    with pytest.raises(RuntimeError, match="synthetic interruption"):
        scope.commit_probe(path, approved, witnesses, after_check=crash)
    assert dump(path) == before
