"""S3 offline rescoring: rebuild per-arm, per-repetition metrics from saved run state.

Zero model calls. Reads each arm's ``state.json`` written by
``run_live_eval --arms`` at the frozen runner (ff97cb1), the frozen live_v1
oracle, and optionally the shared ledger (opened read-only). Scoring reuses the
frozen functions in ``benchmarks.score_review`` and ``benchmarks.run_live_eval``
unchanged; this module only aggregates per repetition and compares arms by the
pre-registered rule. Output is deterministic (sorted keys, no wall-clock stamp).

Usage (repository root)::

    env -u OOS_LLM_API_KEY -u OPENAI_API_KEY OOS_LIVE_ENABLED=false \\
      python -m benchmarks.rescore_comparison \\
        --inputs reports/agent_comparison_v1/inputs \\
        --ledger-db /workspace/oos-ledger/ledger.db \\
        --out evals/agent_comparison_v1/results
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.run_live_eval import (  # noqa: E402
    MANIFEST_PATH,
    ROOT,
    S3_MAIN_PLANNED_CALLS,
    _percentile,
    automatic_dangerous_effective,
    automatic_dangerous_raw,
    build_comparison_index,
    build_comparison_summary,
    load_oracle,
    rules_caught_dangerous,
    verify_manifest,
)
from benchmarks.score_review import handoff_recall_report, intent, score  # noqa: E402

FROZEN_CODE_SHA = "ff97cb13a21665082ab0b62f95e016c6d78befac"
MAIN_ARMS = ("single", "double_same_prompt", "double_roles")
REPETITIONS = (1, 2, 3)
MIN_SAMPLE_DIFF = 3
# 挂起判定：账本 reserved→settled 墙钟远超客户端单调时钟 elapsed_s（挂起期间单调时钟不走）。
SUSPEND_GAP_S = 300
SENSITIVITY_EXCLUDE = ("s3-single-r1-lv1-c10-04",)
FROZEN_SCORING_PATHS = (
    "app",
    "benchmarks/score_review.py",
    "benchmarks/run_live_eval.py",
    "benchmarks/compare_readers.py",
    "evals/live_v1",
)
_CHANGE_KEYS = ("mode", "sku_id", "to_sku", "quantity", "unit", "before_qty", "after_qty")
_ROW_KEYS = ("sku_id", "old_qty", "new_qty", "delta")
_SCORE_KEYS = (
    "raw_strict_intent_match",
    "effective_rows_and_status_match",
    "dangerous_by_code_rule",
    "handoff_outcome",
    "error",
)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def provenance() -> dict:
    script = "benchmarks/rescore_comparison.py"
    unchanged = (
        subprocess.run(
            [
                "git",
                "-C",
                str(ROOT),
                "diff",
                "--quiet",
                FROZEN_CODE_SHA,
                "--",
                *FROZEN_SCORING_PATHS,
            ]
        ).returncode
        == 0
    )
    return {
        "frozen_runner_code_sha": FROZEN_CODE_SHA,
        "rescored_with_code_sha": _git("log", "-1", "--format=%H", "--", script) or None,
        "rescore_script_blob_sha": _git("hash-object", script),
        "frozen_scoring_paths_unchanged": unchanged,
        "frozen_scoring_paths": list(FROZEN_SCORING_PATHS),
        "model_calls": 0,
    }


def _ratio(num: int, den: int) -> dict:
    return {"numerator": num, "denominator": den, "value": round(num / den, 6) if den else None}


def _clean_proposal(payload) -> dict | None:
    if not isinstance(payload, dict):
        return None
    return {
        "disposition": payload.get("disposition"),
        "order_id": payload.get("order_id"),
        "changes": [
            {k: c.get(k) for k in _CHANGE_KEYS if k in c}
            for c in payload.get("changes") or []
            if isinstance(c, dict)
        ],
        "question_count": len(payload.get("questions") or []),
    }


def _clean_check(check) -> dict | None:
    if not isinstance(check, dict):
        return None
    return {
        "status": check.get("status"),
        "issue_count": len(check.get("issues") or []),
        "rows": [
            {k: r.get(k) for k in _ROW_KEYS if k in r}
            for r in check.get("rows") or []
            if isinstance(r, dict)
        ],
    }


def load_arm(inputs: Path, arm: str) -> dict:
    state = json.loads((inputs / arm / "state.json").read_text(encoding="utf-8"))
    if state.get("code_sha") != FROZEN_CODE_SHA:
        raise ValueError(f"{arm}: state code_sha {state.get('code_sha')} != frozen")
    return state


def _references(rows: list[dict]) -> dict[str, dict]:
    refs = {}
    for item in rows:
        if item["sample_id"] in refs:
            continue
        reference = load_oracle(ROOT, item["oracle_path"], item["sample_id"])
        reference["sample_id"] = item["sample_id"]
        reference.setdefault("category", item["category"])
        refs[item["sample_id"]] = reference
    return refs


def score_arm(state: dict, rows: list[dict], refs: dict[str, dict], arm: str) -> list[dict]:
    """One entry per scheduled observation with merged and per-call scores."""
    entries = state["observations"]
    paths = 1 if arm == "single" else 2
    out = []
    for item in rows:
        entry = entries.get(item["observation_id"])
        if entry is None or not entry.get("completed"):
            raise ValueError(f"{item['observation_id']}: missing or incomplete observation")
        if entry.get("code_sha") != FROZEN_CODE_SHA or entry.get("arm") != arm:
            raise ValueError(f"{item['observation_id']}: provenance mismatch")
        reference = refs[item["sample_id"]]
        merged = score(entry["observation"], reference)
        call_obs = [
            entry["call_observations"].get(f"{item['observation_id']}#{i}") for i in range(paths)
        ]
        calls = [score(c, reference) if c else None for c in call_obs]
        out.append(
            {
                "item": item,
                "entry": entry,
                "reference": reference,
                "merged": merged,
                "call_obs": call_obs,
                "calls": calls,
            }
        )
    return out


def _danger(scored: dict, observation: dict, reference: dict) -> tuple[bool, bool, bool]:
    raw = automatic_dangerous_raw(scored, observation.get("raw_proposal"), reference)
    check = observation.get("check")
    return raw, automatic_dangerous_effective(raw, check), rules_caught_dangerous(raw, check)


def _disagreement(call_obs: list) -> str | None:
    """Mirror compare_readers.run_arm: agreement needs two valid, error-free, checked calls."""
    if len(call_obs) < 2:
        return None
    if any(
        c is None or not c.get("schema_valid") or c.get("error") or not c.get("check")
        for c in call_obs
    ):
        return "call_failed_or_invalid"
    if intent(call_obs[0]["raw_proposal"]) != intent(call_obs[1]["raw_proposal"]):
        return "intent_differs"
    return None


def _latency(o: dict) -> float | None:
    values = [c.get("elapsed_s") for c in o["entry"]["record"].get("calls") or []]
    values = [float(v) for v in values if isinstance(v, (int, float))]
    return max(values) if values else None


def metrics(scored: list[dict], arm: str) -> dict:
    n = len(scored)
    raw = sum(o["merged"]["raw_strict_intent_match"] for o in scored)
    eff = sum(bool(o["merged"]["effective_rows_and_status_match"]) for o in scored)
    danger = [_danger(o["merged"], o["entry"]["observation"], o["reference"]) for o in scored]
    clarify = [o for o in scored if o["merged"]["category"] == "clarify"]
    unnecessary: Counter[str] = Counter()
    for o in scored:
        payload = o["entry"]["observation"].get("raw_proposal")
        if (
            isinstance(payload, dict)
            and payload.get("disposition") == "clarify"
            and not o["merged"]["raw_strict_intent_match"]
        ):
            unnecessary[o["merged"]["category"]] += 1
    by_category: dict[str, dict] = {}
    for o in scored:
        bucket = by_category.setdefault(o["merged"]["category"], {"n": 0, "raw": 0, "eff": 0})
        bucket["n"] += 1
        bucket["raw"] += int(o["merged"]["raw_strict_intent_match"])
        bucket["eff"] += int(bool(o["merged"]["effective_rows_and_status_match"]))
    obs_errors: Counter[str] = Counter()
    call_errors: Counter[str] = Counter()
    for o in scored:
        obs_errors[o["entry"].get("error") or "none"] += 1
        for c in o["entry"]["record"].get("calls") or []:
            call_errors[c.get("error") or "none"] += 1
    result = {
        "n": n,
        "raw_accuracy": _ratio(raw, n),
        "effective_accuracy": _ratio(eff, n),
        "needs_human": _ratio(n - eff, n),
        "dangerous": {
            "raw": sum(d[0] for d in danger),
            "effective": sum(d[1] for d in danger),
            "rules_caught": sum(d[2] for d in danger),
        },
        "clarification_recall": _ratio(
            sum(o["merged"]["raw_strict_intent_match"] for o in clarify), len(clarify)
        ),
        "unnecessary_clarification_by_category": dict(sorted(unnecessary.items())),
        "unnecessary_clarification": sum(unnecessary.values()),
        "handoff": handoff_recall_report([o["merged"] for o in scored]),
        "by_category": {
            k: {"raw": _ratio(v["raw"], v["n"]), "effective": _ratio(v["eff"], v["n"])}
            for k, v in sorted(by_category.items())
        },
        "pack_unit_secondary_raw_excluding_pack_unit": _ratio(
            raw - by_category.get("pack_unit", {}).get("raw", 0),
            n - by_category.get("pack_unit", {}).get("n", 0),
        ),
        "observation_errors": dict(sorted(obs_errors.items())),
        "call_errors": dict(sorted(call_errors.items())),
        "model_incomplete_calls": call_errors.get("MODEL_INCOMPLETE", 0),
    }
    if arm != "single":
        per_path = {}
        for i in (0, 1):
            rows = [o["calls"][i] for o in scored if o["calls"][i] is not None]
            danger_i = [
                automatic_dangerous_raw(
                    o["calls"][i], o["call_obs"][i].get("raw_proposal"), o["reference"]
                )
                for o in scored
                if o["calls"][i] is not None
            ]
            per_path[f"path_{i}"] = {
                "raw_accuracy": _ratio(sum(r["raw_strict_intent_match"] for r in rows), n),
                "effective_accuracy": _ratio(
                    sum(bool(r["effective_rows_and_status_match"]) for r in rows), n
                ),
                "dangerous_raw": sum(danger_i),
            }
        result["per_path"] = per_path
        kinds = {o["item"]["observation_id"]: _disagreement(o["call_obs"]) for o in scored}
        flagged = sorted(k for k, v in kinds.items() if v)
        merged_none = sum(
            1 for o in scored if o["entry"]["observation"].get("raw_proposal") is None
        )
        result["disagreement"] = {
            **_ratio(len(flagged), n),
            "by_type": dict(sorted(Counter(kinds[k] for k in flagged).items())),
            "sample_ids": [k.rsplit("-r", 1)[-1].split("-", 1)[-1] for k in flagged],
            "no_merged_candidate": merged_none,
        }
    return result


def latency(scored: list[dict]) -> dict:
    all_v = [v for v in (_latency(o) for o in scored) if v is not None]
    ok_v = [v for o in scored if not o["entry"].get("error") and (v := _latency(o)) is not None]
    return {
        "definition": "每条观测取各路 calls[].elapsed_s 的最大值（并行两路≈墙钟，不含本地合并）",
        "all_attempts": {
            "p50": _percentile(all_v, 50),
            "p95": _percentile(all_v, 95),
            "n": len(all_v),
        },
        "success_only": {
            "p50": _percentile(ok_v, 50),
            "p95": _percentile(ok_v, 95),
            "n": len(ok_v),
        },
    }


def consistency(scored: list[dict]) -> dict:
    by_sample: dict[str, dict[int, dict]] = {}
    for o in scored:
        by_sample.setdefault(o["item"]["sample_id"], {})[o["item"]["repetition"]] = o
    same_outcome = same_intent = same_payload = 0
    for reps in by_sample.values():
        os_ = [reps[r] for r in REPETITIONS]
        same_outcome += len({o["merged"]["raw_strict_intent_match"] for o in os_}) == 1
        same_intent += (
            len({json.dumps(intent(o["entry"]["observation"].get("raw_proposal"))) for o in os_})
            == 1
        )
        same_payload += len({o["merged"]["proposal_sha256"] for o in os_}) == 1
    n = len(by_sample)
    return {
        "samples": n,
        "same_correctness_all_3": _ratio(same_outcome, n),
        "same_business_intent_all_3": _ratio(same_intent, n),
        "identical_payload_all_3": _ratio(same_payload, n),
    }


def ledger_rows(ledger_db: Path | None) -> list[dict]:
    if ledger_db is None:
        return []
    conn = sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT id, agent_id, model, status, cost_usd, input_tokens, output_tokens, "
            "reserved_at, settled_at FROM model_usage"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        conn.close()


def arm_ledger(rows: list[dict], arm: str, model: str) -> list[dict]:
    prefix = f"EVAL-{arm}-"
    return [r for r in rows if r["model"] == model and str(r["agent_id"]).startswith(prefix)]


def ledger_summary(mine: list[dict]) -> dict:
    by_status: dict[str, dict] = {}
    for r in mine:
        b = by_status.setdefault(r["status"], {"rows": 0, "cost_usd": Decimal("0")})
        b["rows"] += 1
        b["cost_usd"] += Decimal(str(r["cost_usd"]))
    return {
        "rows": len(mine),
        "cost_usd": format(sum((Decimal(str(r["cost_usd"])) for r in mine), Decimal("0")), "f"),
        "input_tokens": sum(int(r["input_tokens"] or 0) for r in mine),
        "output_tokens": sum(int(r["output_tokens"] or 0) for r in mine),
        "by_status": {
            k: {"rows": v["rows"], "cost_usd": format(v["cost_usd"], "f")}
            for k, v in sorted(by_status.items())
        },
    }


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def timeout_events(scored: list[dict], rows: list[dict]) -> list[dict]:
    by_id = {r["id"]: r for r in rows}
    events = []
    for o in scored:
        for call in o["entry"]["record"].get("calls") or []:
            if call.get("error") != "MODEL_TIMEOUT":
                continue
            ledger = [by_id.get(x["id"]) for x in o["entry"]["record"].get("ledger") or []]
            ledger = [x for x in ledger if x]
            row = ledger[0] if ledger else None
            reserved, settled = _ts(row and row["reserved_at"]), _ts(row and row["settled_at"])
            wall = (settled - reserved).total_seconds() if reserved and settled else None
            elapsed = call.get("elapsed_s")
            events.append(
                {
                    "observation_id": o["item"]["observation_id"],
                    "ledger_id": row and row["id"],
                    "ledger_status": row and row["status"],
                    "cost_usd": row and str(row["cost_usd"]),
                    "reserved_at_utc": row and row["reserved_at"],
                    "settled_at_utc": row and row["settled_at"],
                    "ledger_wall_s": round(wall, 1) if wall is not None else None,
                    "client_monotonic_elapsed_s": round(elapsed, 2) if elapsed else None,
                    "suspend_signature": bool(
                        wall is not None and elapsed is not None and wall - elapsed > SUSPEND_GAP_S
                    ),
                }
            )
    return events


def compare(per_arm: dict[str, dict], key: str) -> list[dict]:
    """Pre-registered rule: 'differs' only when all 3 repetitions agree in direction
    and every repetition differs by at least MIN_SAMPLE_DIFF samples."""
    out = []
    for i, a in enumerate(MAIN_ARMS):
        for b in MAIN_ARMS[i + 1 :]:
            diffs = [
                per_arm[a]["by_repetition"][str(r)][key]["numerator"]
                - per_arm[b]["by_repetition"][str(r)][key]["numerator"]
                for r in REPETITIONS
            ]
            same_dir = all(d > 0 for d in diffs) or all(d < 0 for d in diffs)
            big = all(abs(d) >= MIN_SAMPLE_DIFF for d in diffs)
            out.append(
                {
                    "a": a,
                    "b": b,
                    "metric": key,
                    "diff_samples_by_repetition": diffs,
                    "same_direction_all_3": same_dir,
                    "all_abs_diff_ge_3": big,
                    "verdict": "有差异" if same_dir and big else "无结论（视为无差异）",
                }
            )
    return out


def _runner_crosscheck(arm, state, rows, scored, refs, saved: dict) -> dict:
    """Rebuild the frozen runner summary from state; compare to the saved summary."""
    rebuilt = build_comparison_summary(
        arm=arm,
        model=saved["model_id"],
        schedule_rows=rows,
        scored_merged=[o["merged"] for o in scored],
        scored_calls=[c for o in scored for c in o["calls"] if c is not None],
        records_by_id={o["item"]["observation_id"]: o["entry"]["record"] for o in scored},
        observations={o["item"]["observation_id"]: o["entry"]["observation"] for o in scored},
        references=refs,
        book=None,
        stop_reason=saved.get("stop_reason"),
        mode=saved.get("mode") or "full",
        evidence_kind=saved.get("evidence_kind") or "real_provider",
        provider=saved.get("provider"),
        price_in_per_mtok=saved.get("price_in_per_mtok"),
        price_out_per_mtok=saved.get("price_out_per_mtok"),
        prompt_version=saved.get("prompt_version"),
        schema_digest=saved.get("schema_sha256"),
        repo_sha=saved.get("code_sha"),
        worktree_dirty=bool(saved.get("worktree_dirty")),
        manifest_digest=saved.get("manifest_sha256"),
        full_index_digest=saved.get("schedule_sha256"),
        arm_index_digest=saved.get("arm_schedule_sha256"),
        seed=int(saved.get("seed")),
        cap_usd=Decimal(saved["cost"]["cap_usd"]),
        run_at=saved.get("run_at"),
        run_id=saved.get("run_id"),
        model_max_tokens=int(saved.get("model_max_tokens")),
        pre_run_spent=None,
        projected_main_usd=None,
        projected_strong_usd=None,
        resumes=None,
    )
    keys = (
        "raw_accuracy_merged",
        "effective_accuracy_merged",
        "raw_accuracy_per_call",
        "effective_accuracy_per_call",
        "by_repetition",
        "dangerous",
        "clarification_recall",
        "unnecessary_clarification_by_category",
        "by_category",
        "repeat_consistency",
        "model_incomplete",
    )
    mismatched = [k for k in keys if rebuilt.get(k) != saved.get(k)]
    for k in ("ledger_spent_usd", "input_tokens", "output_tokens", "ledger_calls"):
        if rebuilt["cost"].get(k) != saved["cost"].get(k):
            mismatched.append(f"cost.{k}")
    if rebuilt["handoff_recall"]["primary_20"] != saved["handoff_recall"]["primary_20"]:
        mismatched.append("handoff_recall.primary_20")
    return {"fields_compared": len(keys) + 5, "mismatched": mismatched}


def sanitize(o: dict) -> dict:
    record = o["entry"]["record"]
    ledger = record.get("ledger") or []
    return {
        "observation_id": o["item"]["observation_id"],
        "sample_id": o["item"]["sample_id"],
        "category": o["item"]["category"],
        "repetition": o["item"]["repetition"],
        "error": o["entry"].get("error"),
        "merged_proposal": _clean_proposal(o["entry"]["observation"].get("raw_proposal")),
        "merged_post_rule": _clean_check(o["entry"]["observation"].get("check")),
        "scoring": {k: o["merged"].get(k) for k in _SCORE_KEYS},
        "calls": [
            {
                "proposal": _clean_proposal(c.get("raw_proposal")) if c else None,
                "post_rule": _clean_check(c.get("check")) if c else None,
                "error": c.get("error") if c else None,
                "raw_strict_intent_match": s["raw_strict_intent_match"] if s else None,
                "elapsed_s": round(rc.get("elapsed_s") or 0, 3),
            }
            for c, s, rc in zip(o["call_obs"], o["calls"], record.get("calls") or [], strict=False)
        ],
        "input_tokens": sum(int(x.get("input_tokens") or 0) for x in ledger),
        "output_tokens": sum(int(x.get("output_tokens") or 0) for x in ledger),
        "cost_usd": format(sum((Decimal(str(x["cost_usd"])) for x in ledger), Decimal("0")), "f"),
    }


def smoke_summary(inputs: Path, rows: list[dict]) -> dict | None:
    path = inputs / "sol_smoke" / "state.json"
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    errors: Counter[str] = Counter()
    tokens = 0
    ids = set()
    for entry in state["observations"].values():
        for c in entry["record"].get("calls") or []:
            errors[c.get("error") or "none"] += 1
        for x in entry["record"].get("ledger") or []:
            ids.add(x["id"])
            tokens += int(x.get("input_tokens") or 0) + int(x.get("output_tokens") or 0)
    cost = sum((Decimal(str(r["cost_usd"])) for r in rows if r["id"] in ids), Decimal("0"))
    return {
        "model": "gpt-6.1-sol",
        "code_sha": state.get("code_sha"),
        "observations": len(state["observations"]),
        "call_errors": dict(sorted(errors.items())),
        "tokens": tokens,
        "ledger_rows": len(ids),
        "ledger_cost_usd": format(cost, "f") if rows else None,
        "status": "冒烟因参数不兼容失败，待决定",
    }


def rescore(inputs: Path, ledger_db: Path | None, out: Path | None) -> dict:
    manifest, manifest_digest = verify_manifest(MANIFEST_PATH, None)
    rows_ledger = ledger_rows(ledger_db)
    per_arm: dict[str, dict] = {}
    published: dict[str, list[dict]] = {}
    for arm in MAIN_ARMS:
        state = load_arm(inputs, arm)
        rows, full_digest, arm_digest = build_comparison_index(manifest, arm=arm)
        refs = _references(rows)
        scored = score_arm(state, rows, refs, arm)
        saved = json.loads((inputs / arm / "summary.json").read_text(encoding="utf-8"))
        reps = {
            str(r): metrics([o for o in scored if o["item"]["repetition"] == r], arm)
            for r in REPETITIONS
        }
        overall = metrics(scored, arm)
        calls = sum(len(o["entry"]["record"].get("calls") or []) for o in scored)
        record_cost = sum(
            (
                Decimal(str(x["cost_usd"]))
                for o in scored
                for x in o["entry"]["record"].get("ledger") or []
            ),
            Decimal("0"),
        )
        raw_total = overall["raw_accuracy"]["numerator"]
        per_arm[arm] = {
            "observations": len(scored),
            "client_calls": calls,
            "planned_calls": S3_MAIN_PLANNED_CALLS[arm],
            "schedule_sha256": full_digest,
            "arm_schedule_sha256": arm_digest,
            "run_id": state.get("run_id"),
            "resumes": state.get("resumes") or [],
            "pre_run_ledger_usd": state.get("pre_run_ledger_usd"),
            "saved_summary_worktree_dirty": saved.get("worktree_dirty"),
            "saved_summary_cap_usd": saved["cost"]["cap_usd"],
            "by_repetition": reps,
            "all_repetitions": overall,
            "mean_raw_accuracy": round(
                sum(reps[str(r)]["raw_accuracy"]["value"] for r in REPETITIONS) / 3, 6
            ),
            "mean_effective_accuracy": round(
                sum(reps[str(r)]["effective_accuracy"]["value"] for r in REPETITIONS) / 3, 6
            ),
            "repeat_consistency": consistency(scored),
            "latency_s": latency(scored),
            "cost": {
                "record_attached_usd": format(record_cost, "f"),
                "ledger": ledger_summary(arm_ledger(rows_ledger, arm, "gpt-6-luna"))
                if rows_ledger
                else None,
                "usd_per_observation": format(record_cost / len(scored), "f"),
                "usd_per_raw_correct_observation": (
                    format(record_cost / raw_total, "f") if raw_total else None
                ),
            },
            "timeouts": timeout_events(scored, rows_ledger),
            "runner_crosscheck": _runner_crosscheck(arm, state, rows, scored, refs, saved),
        }
        if arm == "single":
            kept = [o for o in scored if o["item"]["observation_id"] not in SENSITIVITY_EXCLUDE]
            per_arm[arm]["sensitivity_excluding"] = {
                "excluded": list(SENSITIVITY_EXCLUDE),
                "note": "仅供参考；正式分以含该失败为准",
                "by_repetition": {
                    str(r): {
                        k: metrics([o for o in kept if o["item"]["repetition"] == r], arm)[k]
                        for k in ("raw_accuracy", "effective_accuracy")
                    }
                    for r in REPETITIONS
                },
            }
        published[arm] = [sanitize(o) for o in scored]
    comparisons = [
        c for key in ("raw_accuracy", "effective_accuracy") for c in compare(per_arm, key)
    ]
    main_ledger = [r for r in rows_ledger if r["model"] == "gpt-6-luna"]
    summary = {
        "kind": "agent_comparison_v1 offline rescore; development diagnostics, not formal accuracy",
        "formal_qualified": False,
        "references_review": "参考答案由小八复核，不是 Qi 人工复核",
        "manifest_sha256": manifest_digest,
        "provenance": provenance(),
        "rule": (
            "总体指标：3 次重复方向一致且每次差 ≥3 个样本才记「有差异」，否则「无结论」。"
            "分母 ≤20 的指标只报数字，不下结论。"
        ),
        "arms": per_arm,
        "comparisons": comparisons,
        "ledger_totals": {
            "gpt-6-luna": ledger_summary(main_ledger) if rows_ledger else None,
        },
        "strong_model_smoke": smoke_summary(inputs, rows_ledger),
    }
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "rescore_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for arm, items in published.items():
            with (out / f"observations_{arm}.jsonl").open("w", encoding="utf-8") as handle:
                for item in items:
                    handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--ledger-db", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    summary = rescore(args.inputs, args.ledger_db, args.out)
    for arm, data in summary["arms"].items():
        reps = data["by_repetition"]
        print(
            arm,
            [reps[str(r)]["raw_accuracy"]["numerator"] for r in REPETITIONS],
            [reps[str(r)]["effective_accuracy"]["numerator"] for r in REPETITIONS],
            data["runner_crosscheck"]["mismatched"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
