"""TEST ONLY: scope semantics over synthetic independently witnessed dependencies.

No production imports this model. Witnesses are supplied by the fixture, not
learned from surviving rows or accepted by a user-facing endpoint. In particular
the membership witness is stronger than today's database can establish.
"""

import json
from dataclasses import asdict, dataclass, field

from app.domain.hashing import canonical_hash
from tests import manifest_lineage_model as lineage


@dataclass
class ScopeWitnesses:
    lineage: lineage.SyntheticWitnesses
    # Complete dependency declarations and membership, independently witnessed.
    dependencies: dict[str, set[str]]
    members: dict[str, set[str]]
    facts: dict[str, str]


@dataclass(frozen=True)
class TargetResult:
    order: str
    closure: tuple[str, ...]
    reasons: tuple[str, ...]
    evidence_digest: str | None


@dataclass(frozen=True)
class Report:
    requested: tuple[str, ...]
    targets: tuple[TargetResult, ...]
    covered: tuple[str, ...]
    coverage: str
    outside_request: tuple[str, ...]
    dependencies: tuple[str, ...]
    unassessed_objects: tuple[tuple[str, str], ...]
    # These fields are constants, not a caller's choice.
    protocol: str = field(default="synthetic-scope-v1", init=False)
    inventory_basis: str = field(default="observed_catalog_only", init=False)
    observation: str = field(default="single_read_snapshot", init=False)
    global_completeness: str = field(default="not_assessed", init=False)
    authorizes_business_action: bool = field(default=False, init=False)


def seed_scope(path):
    roots = lineage.seed_model(path)
    # LEGACY is deliberately missing a trusted creation root. Its existence
    # does not block independent A/B, but a dependency on it does.
    dependencies = {"A": {"doc:production_order:MO-A", "stock:S-A"}, "B": {"stock:S-B"}}
    members = {
        "doc:production_order:MO-A": {"A"},
        "stock:S-A": {"A"},
        "stock:S-B": {"B"},
    }
    facts = {
        "doc:production_order:MO-A": canonical_hash({"state": "CANCELLED", "revision": 2}),
        "stock:S-A": canonical_hash({"on_hand": 100, "reserved": 10, "revision": 1}),
        "stock:S-B": canonical_hash({"on_hand": 200, "reserved": 20, "revision": 1}),
    }
    with lineage.connection(path) as db:
        db.executescript(
            "CREATE TABLE catalog(id TEXT PRIMARY KEY);"
            "INSERT INTO catalog VALUES ('A'),('B'),('LEGACY');"
            "CREATE TABLE member(resource TEXT, owner TEXT, PRIMARY KEY(resource,owner));"
            "CREATE TABLE fact(id TEXT PRIMARY KEY, digest TEXT);"
            "CREATE TABLE publication(id INTEGER PRIMARY KEY, token INTEGER, until INTEGER, "
            "report TEXT); INSERT INTO publication VALUES (1,1,100,NULL);"
        )
        db.executemany("INSERT INTO fact VALUES (?,?)", facts.items())
        db.executemany(
            "INSERT INTO member VALUES (?,?)",
            [(key, owner) for key, owners in members.items() for owner in owners],
        )
    return ScopeWitnesses(roots, dependencies, members, facts)


def assess(db, requested, witnesses, *, limit=64):
    """Caller establishes one read transaction. Missing closure never means empty."""
    requested = tuple(sorted(set(requested)))
    catalog = {row[0] for row in db.execute("SELECT id FROM catalog")}
    targets = []
    for target in requested:
        todo, visited, reasons, evidence = ["order:" + target], set(), set(), {}
        while todo:
            key = todo.pop()
            if key in visited:
                continue
            if len(visited) >= limit:
                reasons.add("CLOSURE_LIMIT")
                break
            visited.add(key)
            if key.startswith("order:"):
                owner = key.removeprefix("order:")
                if owner not in catalog:
                    reasons.add(key + ":CATALOG_MISSING")
                try:
                    evidence[key] = lineage.proof(db, owner, witnesses.lineage)
                except lineage.ProofFailure as exc:
                    reasons.add(key + ":" + str(exc))
                # Never accept today's surviving links as a complete declaration.
                deps = witnesses.dependencies.get(owner)
                if deps is None:
                    reasons.add(key + ":DEPENDENCY_DECLARATION_UNKNOWN")
                else:
                    todo.extend(sorted(deps))
                    evidence[key + ":dependencies"] = sorted(deps)
                # Inspect all actual memberships, even for non-requested owners.
                actual = {
                    r[0] for r in db.execute("SELECT resource FROM member WHERE owner=?", (owner,))
                }
                if deps is not None and actual != deps:
                    reasons.add(key + ":DEPENDENCY_SET_MISMATCH")
                todo.extend(sorted(actual))
            else:
                expected = witnesses.members.get(key)
                actual = {
                    r[0] for r in db.execute("SELECT owner FROM member WHERE resource=?", (key,))
                }
                if expected is None:
                    reasons.add(key + ":MEMBERSHIP_UNKNOWN")
                elif expected != actual:
                    reasons.add(key + ":MEMBERSHIP_MISMATCH")
                if key.startswith("doc:") and len(actual) > 1:
                    reasons.add(key + ":OWNERSHIP_CONFLICT")
                todo.extend("order:" + owner for owner in sorted(actual | (expected or set())))
                fact = db.execute("SELECT digest FROM fact WHERE id=?", (key,)).fetchone()
                pinned = witnesses.facts.get(key)
                if pinned is None:
                    reasons.add(key + ":FACT_EVIDENCE_UNKNOWN")
                elif fact != (pinned,):
                    reasons.add(key + ":FACT_MISMATCH_OR_MISSING")
                evidence[key] = [
                    sorted(actual),
                    fact,
                    sorted(expected) if expected is not None else None,
                ]
        targets.append(
            TargetResult(
                target,
                tuple(sorted(visited)),
                tuple(sorted(reasons)),
                None if reasons else canonical_hash(evidence),
            )
        )
    covered = tuple(t.order for t in targets if not t.reasons)
    coverage = (
        "complete_for_requested"
        if requested and len(covered) == len(requested)
        else "partial_for_requested"
        if covered
        else "none"
    )
    dependencies = {key for t in targets for key in t.closure} - {"order:" + r for r in requested}
    assessed_orders = {key[6:] for t in targets for key in t.closure if key.startswith("order:")}
    # A failed closure cannot prove that an unvisited object is unrelated. It
    # may be waiting in the worklist, or hidden behind missing declarations.
    unassessed_reason = (
        "NOT_ASSESSED_CLOSURE_INCOMPLETE"
        if any(t.reasons for t in targets)
        else "OUTSIDE_REQUEST_AND_DEPENDENCIES"
    )
    return Report(
        requested,
        tuple(targets),
        covered,
        coverage,
        tuple(sorted(catalog - set(requested))),
        tuple(sorted(dependencies)),
        tuple((order, unassessed_reason) for order in sorted(catalog - assessed_orders)),
    )


def observe(path, requested, witnesses, **options):
    with lineage.connection(path) as db:
        db.execute("BEGIN")
        return assess(db, requested, witnesses, **options)


@dataclass(frozen=True)
class SyntheticApproval:
    order: str
    evidence_digest: str


def approve_probe(path, order, witnesses):
    """Explicit synthetic authorization step, not a report's allowed flag."""
    result = observe(path, (order,), witnesses).targets[0]
    if result.reasons:
        raise lineage.ProofFailure("UNPROVEN_DEPENDENCIES")
    return SyntheticApproval(order, result.evidence_digest)


def commit_probe(path, approval, witnesses, *, after_check=lambda: None):
    """Marker only. Production approval identity/consumption is NOT modeled here."""
    with lineage.connection(path) as db:
        db.execute("BEGIN IMMEDIATE")
        result = assess(db, (approval.order,), witnesses).targets[0]
        if result.reasons or result.evidence_digest != approval.evidence_digest:
            raise lineage.ProofFailure("STALE_OR_UNPROVEN_DEPENDENCIES")
        after_check()
        db.execute("INSERT INTO effect(owner) VALUES (?)", (approval.order,))
        db.commit()


def publish_probe(path, requested, witnesses, *, token, clock, after_check=lambda: None):
    """Synthetic publication only. No Operation/pending/business API is present.

    Recompute under the write lock, including the dependency closure. A cached
    read result is deliberately not an accepted argument. Actual deployment
    writer isolation is a prerequisite, not something this epoch row proves.
    """
    with lineage.connection(path) as db:
        db.execute("BEGIN IMMEDIATE")

        def owns():
            row = db.execute("SELECT token, until FROM publication WHERE id=1").fetchone()
            if row[0] != token or row[1] <= clock():
                raise lineage.ProofFailure("STALE_RESULT_OWNER")

        owns()
        report = assess(db, requested, witnesses)
        after_check()
        owns()
        db.execute("UPDATE publication SET report=? WHERE id=1", (json.dumps(asdict(report)),))
        db.commit()
        return report
