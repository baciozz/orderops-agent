"""Evaluation-only single/dual-reader arms; never a production multi-Agent claim.

Clients and the existing shared ledger are supplied by the authorized coordinator.
No credentials, provider creation, CLI live mode, automatic retry or business write.
"""

import json
import random
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from uuid import uuid4

from app.adapters.llm import validated_live_provider
from app.db.models import ModelUsage
from app.errors import AppError
from app.review.contracts import Proposal, Snapshot, model_schema
from app.review.service import MAX_PROMPT_TOKEN_BOUND, PROMPT_VERSION, SYSTEM_PROMPT
from pydantic import ValidationError
from sqlalchemy import select

from benchmarks.score_review import intent, sha

ARMS = ("single", "double_same_prompt", "double_roles")
ROLES = (
    "你是独立提取角色A：逐项读取客户要求、数量语义与来源，忠实输出约定提案。仍无批准和执行权限。",
    "你是独立复核角色B：自行阅读同一资料，核对客户要求范围、数量及来源，独立输出约定提案。"
    "不能见到另一角色答案，不能替客户缩小意图；仍无批准和执行权限。",
)


def schedule(sample_ids, repetitions, seed):
    """Freeze this metadata before inference; never send it to a model."""
    if len(set(sample_ids)) != len(sample_ids) or repetitions < 1:
        raise ValueError("distinct sample IDs and positive repetitions required")
    rng = random.Random(seed)
    samples = list(sample_ids)
    rng.shuffle(samples)
    result = []
    for repetition in range(repetitions):
        for index, sample in enumerate(samples):
            offset = (repetition + index) % len(ARMS)
            for arm in ARMS[offset:] + ARMS[:offset]:
                result.append({"sample_id": sample, "repetition": repetition + 1, "arm": arm})
    return result


def validate_public_request(request):
    if (
        request.system_prompt != SYSTEM_PROMPT
        or request.prompt_ref != PROMPT_VERSION
        or request.response_schema != model_schema()
    ):
        raise ValueError(
            "start from the frozen product prompt/schema, not a label-enriched request"
        )
    raw = json.loads(request.user_prompt)
    if set(raw) != {"selected_order_id", "snapshot", "INPUT"}:
        raise ValueError("only application public input fields may reach model context")
    snapshot = Snapshot.model_validate(raw["snapshot"])
    if not snapshot.synthetic:
        raise ValueError("synthetic inputs only")
    allowed = {
        "line",
        "text",
        "source_id",
        "locator",
        "human",
        "source_note",
        "source_page",
        "source_extracted_line",
    }
    if not raw["INPUT"] or any(
        not isinstance(r, dict) or not set(r) <= allowed for r in raw["INPUT"]
    ):
        raise ValueError("INPUT metadata contains an unsupported field")
    if any(r.get("human") is not False for r in raw["INPUT"]):
        raise ValueError("human transcription is a separate assisted arm, not automatic extraction")
    for line in raw["INPUT"]:
        positions = {"source_page", "source_extracted_line"} & line.keys()
        if positions:
            page, number = line.get("source_page"), line.get("source_extracted_line")
            if (
                positions != {"source_page", "source_extracted_line"}
                or type(page) is not int
                or type(number) is not int
                or page < 1
                or number < 1
                or not isinstance(line.get("source_id"), str)
                or not line.get("source_id")
                or line.get("source_id") == "INPUT"
                or line.get("locator") != f"PDF第{page}页，提取行{number}"
            ):
                raise ValueError("PDF extraction positions must match their source locator")
    return sha(raw)


def run_arm(request, *, arm, clients, settings, sessions, check, evidence_kind):
    if arm not in ARMS or evidence_kind not in {"real_provider", "mock_transport"}:
        raise ValueError("declare an arm and real/mock provenance")
    count = 1 if arm == "single" else 2
    if len(clients) != count or any(c.mode != "LIVE" for c in clients) or not callable(check):
        raise ValueError("supply budgeted clients and the same read-only rule check for each arm")
    input_hash = validate_public_request(request)
    provider = validated_live_provider(settings)
    prepared = []
    for index in range(count):
        suffix = "\n" + ROLES[index] if arm == "double_roles" else ""
        candidate = request.model_copy(
            update={
                "agent_id": f"EVAL-{arm}-" + uuid4().hex,
                "task_id": None,
                "fixture_key": None,
                "system_prompt": request.system_prompt + suffix,
            }
        )
        wire = provider.build_request(candidate, candidate.response_schema)
        bound = len((wire.prompt_text + json.dumps(candidate.response_schema)).encode()) + 256
        if bound > MAX_PROMPT_TOKEN_BOUND:
            raise ValueError("role-adjusted request exceeds the same conservative prompt bound")
        prepared.append(candidate)

    def call(index):
        started = perf_counter()
        result = {
            "agent_id": prepared[index].agent_id,
            "payload": None,
            "usage": None,
            "error": None,
            "schema_valid": False,
            "check": None,
            "system_sha256": sha(prepared[index].system_prompt),
        }
        try:
            response = clients[index].structured_call(prepared[index])
            result.update(payload=response.payload, usage=response.usage.model_dump())
            parsed = Proposal.model_validate(response.payload)
            result["schema_valid"] = intent(response.payload) is not None
            if result["schema_valid"]:
                result["check"] = check(parsed)
            else:
                result["error"] = "INCONSISTENT_PROPOSAL"
        except ValidationError:
            result["error"] = "MODEL_SCHEMA_INVALID"
        except Exception as exc:
            result["error"] = exc.code if isinstance(exc, AppError) else "MODEL_CALL_FAILED"
        result["elapsed_s"] = perf_counter() - started
        return result

    started = perf_counter()
    if count == 1:
        results = [call(0)]
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            # Await both, including failures: no hidden winner selection or retry.
            results = list(pool.map(call, range(2)))
    valid = all(r["schema_valid"] and not r["error"] and r["check"] for r in results)
    agreement = bool(
        valid and all(intent(r["payload"]) == intent(results[0]["payload"]) for r in results)
    )

    def check_key(value):
        return {"status": value.get("status"), "rows": value.get("rows")}

    same_check = agreement and all(
        sha(check_key(r["check"])) == sha(check_key(results[0]["check"])) for r in results
    )
    # No partial action intersection, LLM arbitration or acceptance of one failed reader.
    # Human clarification wording/source support remain separately assessable.
    merged = results[0]["payload"] if same_check else None
    merged_check = results[0]["check"] if same_check else None
    if merged is not None and merged["disposition"] == "clarify" and count == 2:
        merged = dict(merged)
        merged["questions"] = list(
            dict.fromkeys(q for r in results for q in r["payload"]["questions"])
        )
        merged_check = check(Proposal.model_validate(merged))
    wall = perf_counter() - started
    ids = [r.agent_id for r in prepared]
    with sessions() as session:
        ledger = [
            {
                "id": row.id,
                "agent_id": row.agent_id,
                "provider": row.provider,
                "model": row.model,
                "status": row.status,
                "cost_usd": str(row.cost_usd),
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "price_version": row.price_version,
            }
            for row in session.scalars(select(ModelUsage).where(ModelUsage.agent_id.in_(ids)))
        ]
    return {
        "protocol": "readonly-reader-comparison-v1",
        "arm": arm,
        "evidence_kind": evidence_kind,
        "input_sha256": input_hash,
        "schema_sha256": sha(request.response_schema),
        "prompt_version": request.prompt_ref,
        "max_output_tokens": request.max_output_tokens,
        "client_call_attempts": count,
        "transport_requests": None,
        "calls": results,
        "ledger": ledger,
        "ledger_complete": len(ledger) == count,
        "wall_s": wall,
        "summed_client_s": sum(r["elapsed_s"] for r in results),
        "business_intent_agreement": agreement,
        "merged_proposal": merged,
        "merged_check": merged_check,
        "needs_manual_resolution": merged is None
        or merged_check.get("status") not in {"ready", "no_change"},
        "requires_human_confirmation": True,
        "business_execution": "NOT_EXECUTED",
        "accuracy": None,
        "advantage": None,
        "timing_scope": "model clients plus deterministic merge; not browser end-to-end",
        "cache_status": "unknown unless separately captured by coordinator",
    }
