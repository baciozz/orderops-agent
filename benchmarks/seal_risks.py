"""Offline author/runner handoff mechanics. Does not generate or inspect risk text.

Run on an independently maintained bundle, not in the application process.
Hash commitments do not prove semantic novelty, reviewer identity or OS isolation.
This developer only tests mechanics with disposable development fixtures.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
POLICY = Path(__file__).with_name("scoring_policy_v1.json")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_file(root, name, prefix):
    if not isinstance(name, str) or not name:
        raise ValueError("a nonempty relative file name is required")
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != prefix:
        raise ValueError("only declared files in the correct public/private subtree are allowed")
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlink inputs are forbidden")
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("a regular, non-hardlinked declared file is required")
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("individual bundle files must stay within the 5 MiB input bound")
    return path


def seal(root, manifest_file, output):
    root = root.absolute()
    if root.resolve() != root or root.is_relative_to(REPO) or output.resolve().is_relative_to(REPO):
        raise ValueError("author material must stay outside the application checkout")
    manifest = json.loads(manifest_file.read_text())
    if manifest.get("synthetic") is not True or manifest.get("purpose") not in {
        "independent_risk_candidate",
        "development_mechanics_only",
    }:
        raise ValueError("declare synthetic material and intended use")
    envs = {key: manifest.get(key) for key in ("author_environment", "developer_environment")}
    if not all(envs.values()) or envs["author_environment"] == envs["developer_environment"]:
        raise ValueError(
            "record separate author/developer environments; metadata is not attestation"
        )
    files = {}
    cases = []
    case_ids = set()
    stories = set()
    inputs = set()
    oracles = set()
    for case in manifest["cases"]:
        if not case.get("case_id") or not case.get("story_id") or case.get("synthetic") is not True:
            raise ValueError("every case requires IDs and an explicit synthetic flag")
        if case["case_id"] in case_ids or case["story_id"] in stories:
            raise ValueError("risk cases must be distinct stories, not repeated IDs")
        case_ids.add(case["case_id"])
        stories.add(case["story_id"])
        if not case["input_files"]:
            raise ValueError("public inputs required")
        public = []
        for name in case["input_files"]:
            path = safe_file(root, name, "public_inputs")
            if name in files:
                raise ValueError("cases cannot share an asset path")
            files[name] = digest(path)
            inputs.add(files[name])
            public.append(name)
        oracle = case["oracle_file"]
        path = safe_file(root, oracle, "private_oracle")
        if oracle in files:
            raise ValueError("cases cannot share an oracle file")
        files[oracle] = digest(path)
        oracles.add(files[oracle])
        cases.append({"case_id": case["case_id"], "input_files": public, "oracle_file": oracle})
    if inputs & oracles:
        raise ValueError("oracle bytes cannot double as a public input")
    result = {
        "protocol": "isolated-risk-handoff-v1",
        "purpose": manifest["purpose"],
        **envs,
        "case_count": len(cases),
        "story_count": len(stories),
        "cases": cases,
        "files": files,
        "source_manifest_sha256": digest(manifest_file),
        "policy_sha256": digest(POLICY),
        "formal_qualified": False,
    }
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
    output.chmod(0o600)
    return {
        "case_count": len(cases),
        "seal_sha256": digest(output),
        "policy_sha256": result["policy_sha256"],
        "formal_qualified": False,
    }


def blockers(frozen, approval, freeze_sha):
    reasons = []
    if frozen["purpose"] != "independent_risk_candidate":
        reasons.append("development fixtures are not a formal risk set")
    if frozen["case_count"] < 30 or frozen["story_count"] < 30:
        reasons.append("at least 30 distinct risk stories required")
    if approval.get("freeze_sha256") != freeze_sha or approval.get("policy_sha256") != digest(
        POLICY
    ):
        reasons.append("approval must bind exact data and scoring-policy commitments")
    for field in ("reviewer_kind", "reviewer_code", "reviewed_at"):
        if not approval.get(field):
            reasons.append("independent human approval incomplete")
    if approval.get("reviewer_kind") != "human":
        reasons.append("AI cannot sign the human gate")
    for field in (
        "owner_verified",
        "reviewed_20_dev_validation_stories",
        "reviewed_all_new_risks",
        "semantically_new_stories_confirmed",
        "old_exposed_risks_reclassified",
        "scoring_definitions_confirmed",
        "formal_run_authorized",
        "independent_environment_boundary_verified",
        "no_developer_or_model_prefreeze_access_verified",
        "input_roles_and_mapping_checked",
    ):
        if approval.get(field) is not True:
            reasons.append(field)
    for field, length in (("code_sha", 40), ("prompt_sha256", 64), ("schema_sha256", 64)):
        value = approval.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value):
            reasons.append(field)
    if not approval.get("reader_versions") or not approval.get("shared_round_ledger_id"):
        reasons.append("reader versions and existing shared round ledger required")
    if not approval.get("model_alias"):
        reasons.append("model alias required; unknown concrete snapshot must be disclosed")
    slots = approval.get("remaining_round_call_slots")
    if type(slots) is not int or slots < frozen["case_count"] * 3 or slots > 1000:
        reasons.append("remaining call slots must cover all risk repetitions within the round cap")
    cap = approval.get("round_cap_usd")
    if type(cap) not in (int, float) or not 0 < cap <= 5:
        reasons.append("round cap must remain within the authorized 5 USD total")
    return sorted(set(reasons))


def export_inputs(root, frozen_file, approval_file, output):
    root = root.absolute()
    if root.resolve() != root or root.is_relative_to(REPO) or output.resolve().is_relative_to(REPO):
        raise ValueError("formal material must remain outside the development checkout")
    frozen = json.loads(frozen_file.read_text())
    approval = json.loads(approval_file.read_text())
    if frozen.get("protocol") != "isolated-risk-handoff-v1":
        raise ValueError("unsupported frozen handoff")
    if frozen["policy_sha256"] != digest(POLICY):
        raise ValueError("scoring definitions changed since freezing")
    reasons = blockers(frozen, approval, digest(frozen_file))
    if reasons:
        raise ValueError("formal input release blocked: " + ", ".join(reasons))
    declared = [p for c in frozen["cases"] for p in c["input_files"] + [c["oracle_file"]]]
    if (
        len(declared) != len(set(declared))
        or set(declared) != set(frozen["files"])
        or frozen["case_count"] != len(frozen["cases"])
    ):
        raise ValueError("inconsistent frozen file index")
    for case in frozen["cases"]:
        for name in case["input_files"]:
            safe_file(root, name, "public_inputs")
        safe_file(root, case["oracle_file"], "private_oracle")
    # Validate all commitments before copying anything, including private files.
    verified_inputs = {}
    total_bytes = 0
    for name, expected in frozen["files"].items():
        prefix = "public_inputs" if name.startswith("public_inputs/") else "private_oracle"
        content = safe_file(root, name, prefix).read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("bundle changed after freezing")
        if prefix == "public_inputs":
            total_bytes += len(content)
            if total_bytes > 256 * 1024 * 1024:
                raise ValueError("public batch exceeds the bounded export size")
            verified_inputs[name] = content
    output.mkdir(mode=0o700, exist_ok=False)
    jobs = []
    for index, case in enumerate(frozen["cases"], 1):
        folder = output / f"job-{index:03d}"
        folder.mkdir(mode=0o700)
        names = []
        for asset_index, name in enumerate(case["input_files"], 1):
            target = folder / (f"input-{asset_index}" + Path(name).suffix.lower())
            target.write_bytes(verified_inputs[name])
            target.chmod(0o600)
            names.append(str(target.relative_to(output)))
        jobs.append(
            {
                "job_id": folder.name,
                "files": names,
                "case_binding_sha256": hashlib.sha256(case["case_id"].encode()).hexdigest(),
            }
        )
    # This index is coordinator metadata, never a customer asset or model prompt.
    (output / "RUNNER_INDEX.json").write_text(json.dumps(jobs, indent=2))
    (output / "RUNNER_INDEX.json").chmod(0o600)
    return {"jobs": len(jobs), "oracle_files_exported": 0, "formal_metrics_passed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["seal", "export-inputs"])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--frozen", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "seal":
        if args.manifest is None:
            parser.error("--manifest required")
        result = seal(args.root, args.manifest, args.output)
    else:
        if args.frozen is None or args.approval is None:
            parser.error("--frozen and --approval required")
        result = export_inputs(args.root, args.frozen, args.approval, args.output)
    print(json.dumps(result))  # commitments/counts only, no texts or labels


if __name__ == "__main__":
    main()
