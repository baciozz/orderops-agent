"""Disposable development fixtures and mocked attestations, never new held-out data."""

import json
import os

import pytest
from benchmarks.seal_risks import POLICY, blockers, digest, export_inputs, seal


def author_bundle(tmp_path, count=2, purpose="development_mechanics_only"):
    root = tmp_path / "independent-author-fixture"
    root.mkdir()
    cases = []
    for n in range(count):
        public = root / "public_inputs" / str(n)
        public.mkdir(parents=True)
        (public / "request.txt").write_text(f"DEVELOPMENT MECHANICS PUBLIC {n}")
        private = root / "private_oracle"
        private.mkdir(exist_ok=True)
        (private / f"{n}.json").write_text(json.dumps({"PRIVATE_REFERENCE_SENTINEL": n}))
        cases.append(
            {
                "case_id": f"unit-{n}",
                "story_id": f"unit-story-{n}",
                "synthetic": True,
                "input_files": [f"public_inputs/{n}/request.txt"],
                "oracle_file": f"private_oracle/{n}.json",
            }
        )
    manifest = tmp_path / "author.json"
    manifest.write_text(
        json.dumps(
            {
                "purpose": purpose,
                "synthetic": True,
                "author_environment": "mock-author-environment",
                "developer_environment": "mock-developer-environment",
                "cases": cases,
            }
        )
    )
    return root, manifest


def mock_approval(frozen_file):
    # Synthetic metadata for transfer mechanics; no real human was involved.
    return {
        "freeze_sha256": digest(frozen_file),
        "policy_sha256": digest(POLICY),
        "reviewer_kind": "human",
        "reviewer_code": "synthetic-unit-fixture-only",
        "reviewed_at": "2026-01-01T00:00:00Z",
        "owner_verified": True,
        "reviewed_20_dev_validation_stories": True,
        "reviewed_all_new_risks": True,
        "semantically_new_stories_confirmed": True,
        "old_exposed_risks_reclassified": True,
        "scoring_definitions_confirmed": True,
        "formal_run_authorized": True,
        "independent_environment_boundary_verified": True,
        "no_developer_or_model_prefreeze_access_verified": True,
        "input_roles_and_mapping_checked": True,
        "model_alias": "mock-not-a-real-model",
        "remaining_round_call_slots": 100,
        "code_sha": "a" * 40,
        "prompt_sha256": "b" * 64,
        "schema_sha256": "c" * 64,
        "reader_versions": {"test": "mock"},
        "shared_round_ledger_id": "mock-existing-ledger",
        "round_cap_usd": 5,
    }


def test_candidate_commitments_do_not_qualify_development_or_missing_humans(tmp_path):
    root, manifest = author_bundle(tmp_path)
    frozen = tmp_path / "frozen.json"
    result = seal(root, manifest, frozen)
    assert result["case_count"] == 2 and result["formal_qualified"] is False
    reasons = blockers(
        json.loads(frozen.read_text()),
        {"code_sha": None, "prompt_sha256": None, "schema_sha256": None},
        digest(frozen),
    )
    assert "AI cannot sign the human gate" in reasons
    assert "code_sha" in reasons and "prompt_sha256" in reasons and "schema_sha256" in reasons
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(mock_approval(frozen)))
    with pytest.raises(ValueError, match="development fixtures"):
        export_inputs(root, frozen, approval, tmp_path / "public-export")
    assert not (tmp_path / "public-export").exists()


def test_public_export_never_contains_reference_bytes_and_detects_drift(tmp_path):
    root, manifest = author_bundle(tmp_path, 30, "independent_risk_candidate")
    frozen = tmp_path / "frozen.json"
    seal(root, manifest, frozen)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(mock_approval(frozen)))
    out = tmp_path / "public-export"
    result = export_inputs(root, frozen, approval, out)
    assert result["jobs"] == 30 and result["oracle_files_exported"] == 0
    assert all(
        b"PRIVATE_REFERENCE_SENTINEL" not in p.read_bytes() for p in out.rglob("*") if p.is_file()
    )
    assert not any("private_oracle" in str(p) for p in out.rglob("*"))
    (root / "public_inputs/0/request.txt").write_text("changed after freeze")
    with pytest.raises(ValueError, match="changed after freezing"):
        export_inputs(root, frozen, approval, tmp_path / "another-export")
    assert not (tmp_path / "another-export").exists()


@pytest.mark.parametrize(
    "fault", ["traversal", "symlink", "hardlink", "oracle_alias", "same_environment"]
)
def test_unsafe_author_handoffs_rejected(tmp_path, fault):
    root, manifest = author_bundle(tmp_path)
    data = json.loads(manifest.read_text())
    file = root / "public_inputs/0/request.txt"
    oracle = root / "private_oracle/0.json"
    if fault == "traversal":
        data["cases"][0]["input_files"] = ["public_inputs/../../escape"]
    elif fault == "same_environment":
        data["author_environment"] = data["developer_environment"]
    elif fault == "oracle_alias":
        file.write_bytes(oracle.read_bytes())
    else:
        file.unlink()
        if fault == "symlink":
            file.symlink_to(oracle)
        else:
            os.link(oracle, file)
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        seal(root, manifest, tmp_path / "frozen.json")
