"""TEST ONLY: bounded lineage/approval model over disposable SQLite databases.

Trusted root pins are supplied by the synthetic fixture, never discovered from
surviving rows. This is not the application's approval/manifest protocol, parser,
migration, import policy, or implementation of scoped verification.
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from app.domain.hashing import canonical_hash


class ProofFailure(Exception):
    pass


@dataclass
class SyntheticWitnesses:
    roots: dict
    authorized_manifests: dict


@contextmanager
def connection(path, *, timeout=2):
    db = sqlite3.connect(path, isolation_level=None, timeout=timeout)
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
    finally:
        db.close()


def put_node(db, identity, body):
    digest = canonical_hash(body)
    db.execute("INSERT OR REPLACE INTO node VALUES (?, ?, ?)", (identity, json.dumps(body), digest))
    return digest


def node(db, identity):
    row = db.execute("SELECT body, digest FROM node WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise ProofFailure("missing_node")
    return json.loads(row[0]), row[1]


def seed_model(path):
    """Explicit synthetic creation evidence; no business database is inspected."""
    pins = SyntheticWitnesses({}, {})
    with connection(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            "CREATE TABLE root (id TEXT, generation INTEGER, digest TEXT, "
            "state TEXT, epoch INTEGER, "
            "PRIMARY KEY(id, generation));"
            "CREATE TABLE node (id TEXT PRIMARY KEY, body TEXT, digest TEXT);"
            "CREATE TABLE head (order_id TEXT PRIMARY KEY, node_id TEXT, digest TEXT);"
            "CREATE TABLE cancel (id TEXT PRIMARY KEY, body TEXT, digest TEXT);"
            "CREATE TABLE claim (kind TEXT, doc TEXT, owner TEXT, state TEXT, "
            "PRIMARY KEY(kind, doc));"
            "CREATE TABLE effect (id INTEGER PRIMARY KEY, owner TEXT);"
        )
        for order in ["A", "B"]:
            for generation in [1, 2]:
                root = f"R-{order}"
                digest = canonical_hash({"synthetic_creation": order, "generation": generation})
                pins.roots[(root, generation)] = (order, digest)
                db.execute(
                    "INSERT INTO root VALUES (?, ?, ?, 'TRUSTED', 1)",
                    (
                        root,
                        generation,
                        digest,
                    ),
                )
            parent = None
            for revision in [1, 2, 3]:
                identity = f"{order}{revision}"
                edges = []
                if order == "A" and revision >= 2:
                    certificate = {
                        "order": "A",
                        "revision": 2,
                        "operation": "OP-A2",
                        "node": "A2",
                        "root": ["R-A", 1],
                        "kind": "production_order",
                        "doc": "MO-A",
                        "state": "CANCELLED",
                    }
                    certificate_hash = canonical_hash(certificate)
                    if revision == 2:
                        db.execute(
                            "INSERT INTO cancel VALUES ('C-A2', ?, ?)",
                            (
                                json.dumps(certificate),
                                certificate_hash,
                            ),
                        )
                        db.execute(
                            "INSERT INTO claim VALUES ('production_order','MO-A','A','CANCELLED')"
                        )
                    edges = [
                        {
                            "kind": "production_order",
                            "doc": "MO-A",
                            "state": "CANCELLED",
                            "anchor": ["C-A2", certificate_hash],
                        }
                    ]
                body = {
                    "order": order,
                    "revision": revision,
                    "operation": f"OP-{identity}",
                    "parent": parent,
                    "root": [f"R-{order}", 2 if revision == 3 else 1],
                    "edges": edges,
                }
                digest = put_node(db, identity, body)
                pins.authorized_manifests[identity] = digest
                parent = [identity, digest]
            db.execute("INSERT INTO head VALUES (?, ?, ?)", (order, identity, digest))
    return pins


def proof(db, order, pins, *, depth_limit=16):
    head = db.execute("SELECT node_id, digest FROM head WHERE order_id=?", (order,)).fetchone()
    if head is None:
        raise ProofFailure("missing_head")
    roots, visited, chain, anchors = {}, set(), [], []

    def require_root(key):
        identity, generation = key
        pinned = pins.roots.get((identity, generation))
        row = db.execute(
            "SELECT digest, state, epoch FROM root WHERE id=? AND generation=?", key
        ).fetchone()
        if (
            type(generation) is not int
            or pinned is None
            or pinned[0] != order
            or row is None
            or row[0] != pinned[1]
            or row[1] != "TRUSTED"
        ):
            raise ProofFailure("untrusted_root")
        roots[(identity, generation)] = [identity, generation, row[0], row[2]]

    current, digest = head
    child = None
    while current is not None:
        if current in visited or len(visited) >= depth_limit:
            raise ProofFailure("cycle_or_limit")
        visited.add(current)
        body, stored = node(db, current)
        if stored != digest or canonical_hash(body) != digest or body["order"] != order:
            raise ProofFailure("node_binding")
        if pins.authorized_manifests.get(current) != digest:
            raise ProofFailure("missing_or_changed_authorization")
        require_root(body["root"])
        if type(body["revision"]) is not int or body["revision"] < 1:
            raise ProofFailure("revision")
        if child is not None:
            if child["revision"] != body["revision"] + 1:
                raise ProofFailure("revision_gap")
            # This model has no ownership transfer/reactivation/retention protocol.
            inherited = {(e["kind"], e["doc"]): e for e in child["edges"]}
            for edge in body["edges"]:
                later = inherited.get((edge["kind"], edge["doc"]))
                if later is None or (edge["state"] == "CANCELLED" and later != edge):
                    raise ProofFailure("anchor_not_inherited")
        seen = set()
        for edge in body["edges"]:
            key = (edge["kind"], edge["doc"])
            if key in seen:
                raise ProofFailure("duplicate_edge")
            seen.add(key)
            claim = db.execute("SELECT owner FROM claim WHERE kind=? AND doc=?", key).fetchone()
            if claim != (order,):
                raise ProofFailure("ownership_conflict_or_missing")
            if edge["state"] != "CANCELLED":
                raise ProofFailure("unsupported_edge_state")
            anchor = edge["anchor"]
            row = db.execute("SELECT body, digest FROM cancel WHERE id=?", (anchor[0],)).fetchone()
            if row is None:
                raise ProofFailure("missing_cancel_anchor")
            certificate = json.loads(row[0])
            if row[1] != anchor[1] or canonical_hash(certificate) != anchor[1]:
                raise ProofFailure("cancel_digest")
            if (
                certificate["order"] != order
                or (certificate["kind"], certificate["doc"]) != key
                or certificate["state"] != "CANCELLED"
                or type(certificate["revision"]) is not int
                or not 1 <= certificate["revision"] <= body["revision"]
            ):
                raise ProofFailure("cancel_identity")
            require_root(certificate["root"])
            anchors.append((certificate, anchor))
        chain.append([current, digest])
        child = body
        parent = body["parent"]
        if parent is None:
            if body["revision"] != 1:
                raise ProofFailure("unproved_checkpoint")
            break
        current, digest = parent
    for certificate, anchor in anchors:
        if certificate["node"] not in visited:
            raise ProofFailure("cancel_not_ancestor")
        origin, _ = node(db, certificate["node"])
        if (
            origin["revision"] != certificate["revision"]
            or origin["operation"] != certificate["operation"]
            or origin["root"] != certificate["root"]
            or not any(e["anchor"] == anchor for e in origin["edges"])
        ):
            raise ProofFailure("cancel_origin")
    return canonical_hash(
        {"head": list(head), "chain": chain, "roots": [roots[key] for key in sorted(roots)]}
    )


@dataclass(frozen=True)
class ApprovedBase:
    order: str
    proof_digest: str


def approve(path, order, pins):
    with connection(path) as db:
        db.execute("BEGIN")
        return ApprovedBase(order, proof(db, order, pins))


def commit_marker(path, approved, pins, *, after_check=lambda: None):
    """Only an experimental effect marker; never writes real business records."""
    with connection(path) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if proof(db, approved.order, pins) != approved.proof_digest:
                raise ProofFailure("stale_approval")
            after_check()
            db.execute("INSERT INTO effect(owner) VALUES (?)", (approved.order,))
            db.commit()
        except Exception:
            db.rollback()
            raise
