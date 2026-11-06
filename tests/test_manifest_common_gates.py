"""Common safety prerequisites, not a production manifest or scope-policy rollout."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from app.db.models import DownstreamLink, Operation
from app.db.session import begin_immediate
from app.domain.hashing import canonical_hash
from app.services import kernel
from sqlalchemy import select, text
from tests import manifest_lineage_model as model
from tests import test_link_manifest_design as legacy

seeded = legacy.seeded


@pytest.fixture()
def lineage(tmp_path):
    path = tmp_path / "lineage.db"
    return path, model.seed_model(path)


def dump(path):
    with model.connection(path) as db:
        return "\n".join(db.iterdump())


def reseal(db, order="A", *, witnesses=None):
    """Fault injection may preserve internal hashes; hashes are not signatures."""
    parent = None
    for revision in [1, 2, 3]:
        identity = f"{order}{revision}"
        body, _ = model.node(db, identity)
        body["parent"] = parent
        digest = model.put_node(db, identity, body)
        if witnesses is not None:
            # A deliberately authorized malformed fixture tests structural checks
            # independently of authorization. NEVER a proposed production workflow.
            witnesses.authorized_manifests[identity] = digest
        parent = [identity, digest]
    db.execute("UPDATE head SET node_id=?, digest=? WHERE order_id=?", (*parent, order))


def test_two_root_generations_and_inherited_cancel_anchor_have_a_valid_control(lineage):
    path, pins = lineage
    approved = model.approve(path, "A", pins)
    model.commit_marker(path, approved, pins)
    with model.connection(path) as db:
        assert db.execute("SELECT owner FROM effect").fetchall() == [("A",)]
        current, _ = model.node(db, "A3")
        assert current["root"] == ["R-A", 2]
        certificate = json.loads(db.execute("SELECT body FROM cancel").fetchone()[0])
        assert certificate["root"] == ["R-A", 1]
        assert certificate["revision"] == 2


@pytest.mark.parametrize(
    "fault",
    [
        "missing_parent",
        "missing_grandparent",
        "tampered_grandparent",
        "foreign_parent",
        "old_root_missing",
        "old_root_revoked",
        "root_identity_reused",
        "history_truncated",
        "cycle",
    ],
)
def test_trusted_leaf_never_substitutes_for_complete_parent_and_root_evidence(lineage, fault):
    path, pins = lineage
    approved = model.approve(path, "A", pins)
    with model.connection(path) as db:
        if fault.startswith("missing_"):
            db.execute(
                "DELETE FROM node WHERE id=?", ("A2" if fault == "missing_parent" else "A1",)
            )
        elif fault == "tampered_grandparent":
            body, _ = model.node(db, "A1")
            body["operation"] = "altered"
            db.execute("UPDATE node SET body=? WHERE id='A1'", (json.dumps(body),))
        elif fault == "foreign_parent":
            body, _ = model.node(db, "A1")
            body["order"] = "B"
            model.put_node(db, "A1", body)
            reseal(db, witnesses=pins)
        elif fault == "old_root_missing":
            db.execute("DELETE FROM root WHERE id='R-A' AND generation=1")
        elif fault == "old_root_revoked":
            db.execute("UPDATE root SET state='REVOKED' WHERE id='R-A' AND generation=1")
        elif fault == "root_identity_reused":
            db.execute(
                "UPDATE root SET digest='new-unrelated-root' WHERE id='R-A' AND generation=1"
            )
        else:
            body, digest = model.node(db, "A3")
            body["parent"] = None if fault == "history_truncated" else ["A3", digest]
            digest = model.put_node(db, "A3", body)
            pins.authorized_manifests["A3"] = digest
            db.execute("UPDATE head SET digest=? WHERE order_id='A'", (digest,))
    before = dump(path)
    reason = {
        "missing_parent": "missing_node",
        "missing_grandparent": "missing_node",
        "tampered_grandparent": "node_binding",
        "foreign_parent": "node_binding",
        "old_root_missing": "untrusted_root",
        "old_root_revoked": "untrusted_root",
        "root_identity_reused": "untrusted_root",
        "history_truncated": "unproved_checkpoint",
        "cycle": "cycle_or_limit",
    }[fault]
    with pytest.raises(model.ProofFailure, match=reason):
        model.commit_marker(path, approved, pins)
    assert dump(path) == before


def test_root_revocation_and_reacceptance_invalidates_old_approval_even_with_same_head(lineage):
    path, pins = lineage
    old = model.approve(path, "A", pins)
    with model.connection(path) as db:
        db.execute("UPDATE root SET state='REVOKED', epoch=2 WHERE id='R-A' AND generation=1")
        db.execute("UPDATE root SET state='TRUSTED', epoch=3 WHERE id='R-A' AND generation=1")
    with pytest.raises(model.ProofFailure, match="stale_approval"):
        model.commit_marker(path, old, pins)
    fresh = model.approve(path, "A", pins)
    assert fresh != old
    model.commit_marker(path, fresh, pins)


def test_unbound_valid_parent_change_requires_reapproval(lineage):
    path, pins = lineage
    old = model.approve(path, "B", pins)
    with model.connection(path) as db:
        body, _ = model.node(db, "B1")
        body["operation"] = "different-retained-origin"
        model.put_node(db, "B1", body)
        reseal(db, "B", witnesses=pins)
    with pytest.raises(model.ProofFailure, match="stale_approval"):
        model.commit_marker(path, old, pins)


def test_preflight_success_cannot_replace_in_transaction_revalidation(lineage):
    path, pins = lineage
    approved = model.approve(path, "A", pins)
    with model.connection(path) as old_reader:
        old_reader.execute("BEGIN")
        assert model.proof(old_reader, "A", pins) == approved.proof_digest
        with model.connection(path) as writer:
            writer.execute("DELETE FROM root WHERE id='R-A' AND generation=1")
        assert model.proof(old_reader, "A", pins) == approved.proof_digest  # Old WAL snapshot.
    before = dump(path)
    with pytest.raises(model.ProofFailure, match="untrusted_root"):
        model.commit_marker(path, approved, pins)
    assert dump(path) == before


def test_writer_cannot_change_ancestry_between_locked_recheck_and_effect(lineage):
    path, pins = lineage
    approved = model.approve(path, "A", pins)

    def competing_write():
        with model.connection(path, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("DELETE FROM node WHERE id='A1'")

    with ThreadPoolExecutor(max_workers=1) as pool:
        model.commit_marker(
            path, approved, pins, after_check=lambda: pool.submit(competing_write).result(timeout=5)
        )
    with model.connection(path) as db:
        assert db.execute("SELECT count(*) FROM effect").fetchone() == (1,)
        db.execute("DELETE FROM node WHERE id='A1'")  # After commit still needs post-verification.
        with pytest.raises(model.ProofFailure, match="missing_node"):
            model.proof(db, "A", pins)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "drop_in_successor",
        "future_revision",
        "wrong_operation",
        "wrong_generation",
        "wrong_ancestor",
        "wrong_owner",
        "wrong_doc",
    ],
)
def test_cancellation_requires_exact_ancestor_identity_across_root_generations(lineage, fault):
    path, pins = lineage
    with model.connection(path) as db:
        if fault == "missing":
            db.execute("DELETE FROM cancel")
        elif fault == "drop_in_successor":
            body, _ = model.node(db, "A3")
            body["edges"] = []
            model.put_node(db, "A3", body)
            reseal(db, witnesses=pins)
        else:
            certificate = json.loads(db.execute("SELECT body FROM cancel").fetchone()[0])
            field, value = {
                "future_revision": ("revision", 999),
                "wrong_operation": ("operation", "OP-OTHER"),
                "wrong_generation": ("root", ["R-A", 2]),
                "wrong_ancestor": ("node", "B2"),
                "wrong_owner": ("order", "B"),
                "wrong_doc": ("doc", "MO-OTHER"),
            }[fault]
            certificate[field] = value
            digest = canonical_hash(certificate)
            db.execute("UPDATE cancel SET body=?, digest=?", (json.dumps(certificate), digest))
            for identity in ["A2", "A3"]:
                body, _ = model.node(db, identity)
                body["edges"][0]["anchor"][1] = digest
                model.put_node(db, identity, body)
            reseal(db, witnesses=pins)  # Even authorized bad identity cannot pass.
    with pytest.raises(model.ProofFailure):
        model.approve(path, "A", pins)


def test_missing_older_root_is_not_repaired_by_a_new_root_or_readable_leaf(lineage):
    path, pins = lineage
    with model.connection(path) as db:
        db.execute("DELETE FROM root WHERE id='R-A' AND generation=1")
        body, digest = model.node(db, "A3")
        assert canonical_hash(body) == digest and body["root"] == ["R-A", 2]
        with pytest.raises(model.ProofFailure, match="untrusted_root"):
            model.proof(db, "A", pins)


@pytest.mark.parametrize("fault", ["missing_approval", "coherent_rewrite"])
def test_structural_hash_chain_cannot_replace_per_node_authorization(lineage, fault):
    path, witnesses = lineage
    if fault == "missing_approval":
        del witnesses.authorized_manifests["B1"]
    else:
        with model.connection(path) as db:
            body, _ = model.node(db, "B1")
            body["operation"] = "unapproved-rewrite"
            model.put_node(db, "B1", body)
            reseal(db, "B")
    with pytest.raises(model.ProofFailure, match="missing_or_changed_authorization"):
        model.approve(path, "B", witnesses)


@pytest.mark.parametrize("foreign_keys", [False, True])
def test_exclusive_typed_document_claim_survives_concurrent_orders_and_fk_off(
    lineage, foreign_keys
):
    path, _ = lineage
    barrier = Barrier(2)

    def claim(owner):
        with model.connection(path) as db:
            db.execute(f"PRAGMA foreign_keys={int(foreign_keys)}")
            barrier.wait(timeout=5)
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("INSERT INTO effect(owner) VALUES (?)", (owner,))
                db.execute(
                    "INSERT INTO claim VALUES ('purchase_order', 'NEW-PO', ?, 'ACTIVE')", (owner,)
                )
                db.commit()
                return owner
            except sqlite3.IntegrityError:
                db.rollback()
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim, owner) for owner in ["A", "B"]]
        results = [f.result(timeout=10) for f in futures]
    with model.connection(path) as db:
        winner = [result for result in results if result is not None]
        assert len(winner) == 1
        assert db.execute("SELECT owner FROM effect").fetchall() == [(winner[0],)]
        assert db.execute("SELECT owner FROM claim WHERE doc='NEW-PO'").fetchone() == (winner[0],)


def test_cancellation_does_not_release_identity_for_another_order(lineage):
    path, _ = lineage
    with model.connection(path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO claim VALUES ('production_order', 'MO-A', 'B', 'ACTIVE')")
        # Same string in a different document namespace is a different typed identity.
        db.execute("INSERT INTO claim VALUES ('purchase_order', 'MO-A', 'B', 'ACTIVE')")


def test_another_order_claim_invalidates_owner_even_if_local_manifest_hashes_match(lineage):
    path, pins = lineage
    with model.connection(path) as db:
        db.execute("UPDATE claim SET owner='B' WHERE kind='production_order' AND doc='MO-A'")
    with pytest.raises(model.ProofFailure, match="ownership_conflict"):
        model.approve(path, "A", pins)


def test_chain_depth_limit_is_uncertainty_never_a_truncated_success(lineage):
    path, pins = lineage
    with model.connection(path) as db:
        with pytest.raises(model.ProofFailure, match="cycle_or_limit"):
            model.proof(db, "A", pins, depth_limit=2)


def test_existing_pending_verification_is_not_a_global_business_commit_lock(seeded):
    token = kernel.verify_failpoint.set(True)
    try:
        first = legacy.commit(seeded)
        second = legacy.commit(seeded, 2)
    finally:
        kernel.verify_failpoint.reset(token)
    with seeded() as session:
        assert [first.revision, second.revision] == [2, 3]
        assert all(
            session.get(Operation, r.operation_id).committed_verification_pending
            for r in [first, second]
        )


def test_existing_old_verifier_can_publish_despite_future_schema_marker(seeded):
    result = legacy.commit(seeded)
    legacy.erase_line_and_origin(seeded, 2)
    with seeded() as session, begin_immediate(session):
        session.get(Operation, result.operation_id).committed_verification_pending = True
        session.execute(text("UPDATE alembic_version SET version_num='future-manifest-probe'"))
    kernel._post_verify(seeded, result.operation_id)
    with seeded() as session:
        assert not session.get(Operation, result.operation_id).committed_verification_pending


def test_existing_link_schema_does_not_reserve_document_ownership_globally(seeded):
    with seeded() as session, begin_immediate(session):
        link = session.scalar(select(DownstreamLink).where(DownstreamLink.doc_id == "MO-2002"))
        session.add(
            DownstreamLink(
                id="synthetic-foreign-owner",
                doc_type=link.doc_type,
                doc_id=link.doc_id,
                order_id="SO-1002",
                order_revision=1,
                order_line_sku="C-BLACK-L",
                link_seq=999,
            )
        )
    with seeded() as session:
        assert set(
            session.scalars(
                select(DownstreamLink.order_id).where(
                    DownstreamLink.doc_type == "production_order",
                    DownstreamLink.doc_id == "MO-2002",
                )
            )
        ) == {"SO-1001", "SO-1002"}


def test_fk_off_connection_bypasses_proposed_pointer_constraint(tmp_path):
    path = tmp_path / "writer.db"
    with model.connection(path) as db:
        db.executescript(
            "CREATE TABLE header(id TEXT PRIMARY KEY);"
            "CREATE TABLE orders(id TEXT PRIMARY KEY, "
            "manifest TEXT NOT NULL REFERENCES header(id));"
            "INSERT INTO header VALUES ('known'); INSERT INTO orders VALUES ('A','known');"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE orders SET manifest='missing'")
    with sqlite3.connect(path) as old:
        old.execute("PRAGMA foreign_keys=OFF")
        old.execute("UPDATE orders SET manifest='missing'")
    with model.connection(path) as db:
        assert db.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert db.execute("PRAGMA foreign_key_check").fetchall()  # ON does not undo old damage.


def test_epoch_trigger_can_reject_old_updates_but_is_not_a_writer_identity_boundary(tmp_path):
    path = tmp_path / "epoch.db"
    with model.connection(path) as db:
        db.executescript(
            "CREATE TABLE policy(epoch INTEGER); INSERT INTO policy VALUES (2);"
            "CREATE TABLE result(id TEXT PRIMARY KEY, pending INTEGER, writer_epoch INTEGER);"
            "INSERT INTO result VALUES ('op',1,1);"
            "CREATE TRIGGER fence BEFORE UPDATE ON result "
            "WHEN NEW.writer_epoch != (SELECT epoch FROM policy) "
            "BEGIN SELECT RAISE(ABORT, 'old writer'); END;"
        )
    with sqlite3.connect(path) as old:
        old.execute("PRAGMA foreign_keys=OFF")
        with pytest.raises(sqlite3.IntegrityError, match="old writer"):
            old.execute("UPDATE result SET pending=0")
        # A direct writable connection can copy the public epoch. It is not authorization.
        old.execute("UPDATE result SET pending=0, writer_epoch=2")
    with model.connection(path) as db:
        assert db.execute("SELECT pending FROM result").fetchone() == (0,)
