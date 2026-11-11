"""Observe one authorized extraction; compare its raw proposal with its rule result.

This module does not create a provider, load credentials, choose samples or read
reference answers. The caller supplies the existing budgeted client. The CLI
only summarizes saved records, and never calls a model.
"""

import argparse
import hashlib
import json
import math
import re
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from app.adapters.llm import LLMClient, StructuredCallRequest, StructuredCallResult
from app.db.models import ModelUsage
from app.errors import AppError
from app.review import service
from app.review.contracts import Proposal
from pydantic import ValidationError
from sqlalchemy import select


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class Observation:
    def __init__(self, client: LLMClient):
        self.client = client
        self.mode = client.mode
        self.calls: list[dict] = []

    def structured_call(self, request: StructuredCallRequest) -> StructuredCallResult:
        call = {
            "prompt_version": request.prompt_ref,
            "system_sha256": digest(request.system_prompt),
            "input_sha256": digest(json.loads(request.user_prompt)),
            "schema_sha256": digest(request.response_schema),
            "max_output_tokens": request.max_output_tokens,
            "payload": None,
            "usage": None,
            "error": None,
        }
        self.calls.append(call)
        started = perf_counter()
        try:
            result = self.client.structured_call(request)
            call.update(payload=deepcopy(result.payload), usage=result.usage.model_dump())
            return result
        except Exception as exc:
            call["error"] = exc.code if isinstance(exc, AppError) else "MODEL_CALL_FAILED"
            raise
        finally:
            call["elapsed_s"] = perf_counter() - started


def run_once(sessions, settings, review_id, request, *, client: LLMClient, metadata: dict) -> dict:
    """Run the actual service once on a fresh synthetic workspace, with no retries.

    Metadata stays outside model input. A caller-declared real/mock classification
    is provenance, not independent proof. Human corrections and replayed results
    cannot masquerade as raw baseline answers.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", metadata.get("code_sha", "")) or not re.fullmatch(
        r"[0-9a-f]{64}", metadata.get("dataset_sha256", "")
    ):
        raise ValueError("exact code and dataset hashes required")
    if metadata.get("evidence_kind") not in {"real_provider", "mock_transport"}:
        raise ValueError("declare real_provider or mock_transport provenance")
    before = service.view(sessions, review_id)
    data = before["data"]
    if (
        not data["input"]["snapshot"].get("synthetic")
        or data["attempts"]
        or data.get("human")
        or data.get("proposal") is not None
        or before["status"] != "DRAFT"
    ):
        raise ValueError("a fresh synthetic workspace is required for every repetition")
    observed = Observation(client)
    error = None
    started = perf_counter()
    try:
        service.extract(sessions, settings, review_id, request, client=observed)
    except Exception as exc:
        error = exc.code if isinstance(exc, AppError) else "EXTRACTION_FAILED"
    elapsed = perf_counter() - started
    after = service.view(sessions, review_id)
    calls = observed.calls
    raw = calls[0]["payload"] if len(calls) == 1 else None
    schema_valid = False
    if raw is not None:
        try:
            Proposal.model_validate(raw)
            schema_valid = True
        except ValidationError:
            pass
    accepted = (
        schema_valid
        and not after["data"].get("human")
        and after["data"].get("proposal") == raw
        and after["status"] == "NEEDS_REVIEW"
    )
    # Read the same ledger, including failed calls and pessimistic reservations.
    # No new ledger/client is constructed and no second extraction is requested.
    with sessions() as session:
        ledger = [
            {
                "id": row.id,
                "provider": row.provider,
                "model": row.model,
                "status": row.status,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cost_usd": str(row.cost_usd),
                "price_version": row.price_version,
            }
            for row in session.scalars(select(ModelUsage).where(ModelUsage.agent_id == review_id))
        ]
    return {
        "protocol": "paired-single-extraction-v1",
        "metadata": deepcopy(metadata),
        "review_id": review_id,
        "call_count": len(calls),
        "call_count_definition": "structured_call attempts; not verified HTTP transmissions",
        "transport_requests": None,
        "calls": calls,
        "raw_schema_valid": schema_valid,
        "raw_disposition": raw.get("disposition") if schema_valid else None,
        "full_status": after["status"],
        "full_check": deepcopy(after["data"].get("check")) if accepted else None,
        "error": error or after["data"].get("error"),
        "result_discarded": bool(raw is not None and not accepted),
        "timing_s": {
            "service_extract": elapsed,
            "model_client": sum(c["elapsed_s"] for c in calls),
            "parse_ocr": None,
            "rules_only": None,
            "human_wait": None,
            "browser_end_to_end": None,
        },
        "ledger": ledger,
        "cost_complete": len(ledger) == len(calls) and bool(calls),
        "quality": None,
        "quality_reason": "No independently reviewed reference labels were supplied.",
    }


def summarize(records: list[dict]) -> dict:
    if not records:
        raise ValueError("keep every attempted case, including failures")
    if any(r.get("protocol") != "paired-single-extraction-v1" for r in records):
        raise ValueError("unsupported record protocol")
    identities = [r["review_id"] for r in records]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate workspace records would double count cost")
    groups = {
        (r["metadata"]["code_sha"], r["metadata"]["dataset_sha256"], r["metadata"]["evidence_kind"])
        for r in records
    }
    if len(groups) != 1:
        raise ValueError("summarize versions, datasets and real/mock evidence separately")
    configurations = {
        (c["prompt_version"], c["system_sha256"], c["schema_sha256"], c["max_output_tokens"])
        for r in records
        for c in r["calls"]
    }
    models = {(row["provider"], row["model"]) for r in records for row in r["ledger"]}
    if len(configurations) > 1 or len(models) > 1:
        raise ValueError("summarize model and prompt configurations separately")

    def distribution(field):
        values = sorted(r["timing_s"][field] for r in records)
        return {f"p{p}": values[max(0, math.ceil(len(values) * p / 100) - 1)] for p in (50, 95)}

    return {
        "protocol": "paired-single-extraction-v1-summary",
        "code_sha": records[0]["metadata"]["code_sha"],
        "dataset_sha256": records[0]["metadata"]["dataset_sha256"],
        "evidence_kind": records[0]["metadata"]["evidence_kind"],
        "model_configurations": sorted([list(item) for item in models]),
        "prompt_configurations": sorted([list(item) for item in configurations]),
        "attempts": len(records),
        "client_call_attempts": sum(r["call_count"] for r in records),
        "transport_requests": None,
        "raw_schema_valid": sum(r["raw_schema_valid"] for r in records),
        "raw_propose": sum(r["raw_disposition"] == "propose" for r in records),
        "rule_ready": sum((r["full_check"] or {}).get("status") == "ready" for r in records),
        "rule_blocked": sum((r["full_check"] or {}).get("status") == "blocked" for r in records),
        "clarify": sum((r["full_check"] or {}).get("status") == "clarify" for r in records),
        "errors": sum(bool(r["error"]) for r in records),
        "discarded": sum(r["result_discarded"] for r in records),
        "service_extract_s": distribution("service_extract"),
        "model_client_s": distribution("model_client"),
        "cost_complete": all(r["cost_complete"] for r in records),
        "ledger_cost_usd": str(
            sum((Decimal(row["cost_usd"]) for r in records for row in r["ledger"]), Decimal(0))
        ),
        "accuracy": None,
        "multi_agent_advantage": None,
        "limits": [
            "Paired raw/rule observations share one extraction and one cost; do not add arm costs.",
            "No truth scoring; ready/blocked rates are not accuracy.",
            "Ledger coverage is not provider billing proof; failed calls may use cost bounds.",
            "Timing excludes upload/OCR, human waiting and browser export; rule-only time unknown.",
            "No second model Agent, role specialization or parallel speed comparison exists.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description="Offline summary; never calls a provider")
    parser.add_argument("--records", required=True, type=Path, help="JSONL records")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = summarize([json.loads(line) for line in args.records.read_text().splitlines() if line])
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
