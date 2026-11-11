"""Local call-trace records: hashes and metadata only, never raw prompts or secrets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.llm import StructuredCallRequest
from app.db.models import CallTrace
from app.db.session import begin_immediate

_HASH_LENGTH = 64


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_canonical(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(raw)


@dataclass(frozen=True)
class CallTraceDraft:
    """Metadata collected around a structured call; contains no raw prompt text."""

    review_id: str | None
    task_id: str | None
    agent_id: str | None
    provider: str
    model: str
    prompt_version: str | None
    prompt_sha256: str
    schema_sha256: str
    input_sha256: str
    started_at: datetime
    elapsed_ms: int
    status: str
    error_code: str | None
    schema_valid: bool


def draft_from_request(
    request: StructuredCallRequest,
    *,
    provider: str,
    model: str,
    started_at: datetime,
    elapsed_ms: int,
    status: str,
    error_code: str | None,
    schema_valid: bool,
    review_id: str | None = None,
) -> CallTraceDraft:
    prompt = f"{request.system_prompt}\n{request.user_prompt}"
    return CallTraceDraft(
        review_id=review_id,
        task_id=request.task_id,
        agent_id=request.agent_id,
        provider=provider,
        model=model,
        prompt_version=request.prompt_ref or request.response_schema_id,
        prompt_sha256=sha256_text(prompt),
        schema_sha256=sha256_canonical(request.response_schema),
        input_sha256=sha256_text(request.user_prompt),
        started_at=started_at,
        elapsed_ms=max(0, elapsed_ms),
        status=status,
        error_code=error_code,
        schema_valid=schema_valid,
    )


def build_call_trace(
    draft: CallTraceDraft,
    *,
    ledger_row_id: str | None,
    input_tokens: int,
    output_tokens: int,
    cost_usd: Decimal,
) -> CallTrace:
    return CallTrace(
        id=f"CT-{uuid4().hex[:16]}",
        ledger_row_id=ledger_row_id,
        review_id=draft.review_id,
        task_id=draft.task_id,
        agent_id=draft.agent_id,
        provider=draft.provider,
        model=draft.model,
        prompt_version=draft.prompt_version,
        prompt_sha256=draft.prompt_sha256,
        schema_sha256=draft.schema_sha256,
        input_sha256=draft.input_sha256,
        started_at=draft.started_at,
        elapsed_ms=draft.elapsed_ms,
        status=draft.status,
        error_code=draft.error_code,
        schema_valid=draft.schema_valid,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )


class CallTraceStore:
    """Writes fixture traces without touching the live model_usage ledger."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def record_fixture(self, draft: CallTraceDraft) -> None:
        session = self._sessions()
        try:
            with begin_immediate(session):
                session.add(
                    build_call_trace(
                        draft,
                        ledger_row_id=None,
                        input_tokens=0,
                        output_tokens=0,
                        cost_usd=Decimal("0"),
                    )
                )
        finally:
            session.close()


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def trace_to_export_row(row: CallTrace) -> dict[str, Any]:
    cost = row.cost_usd if isinstance(row.cost_usd, Decimal) else Decimal(str(row.cost_usd))
    return {
        "id": row.id,
        "ledger_row_id": row.ledger_row_id,
        "review_id": row.review_id,
        "task_id": row.task_id,
        "agent_id": row.agent_id,
        "provider": row.provider,
        "model": row.model,
        "prompt_version": row.prompt_version,
        "prompt_sha256": row.prompt_sha256,
        "schema_sha256": row.schema_sha256,
        "input_sha256": row.input_sha256,
        "started_at": _iso(row.started_at),
        "elapsed_ms": row.elapsed_ms,
        "status": row.status,
        "error_code": row.error_code,
        "schema_valid": bool(row.schema_valid),
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "cost_usd": format(cost, "f"),
    }


def parse_since(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def export_traces(
    session_factory: sessionmaker[Session],
    *,
    since: datetime,
    out_path: Path,
) -> int:
    """Write JSONL metadata to a new file. Refuses to overwrite or include raw text."""
    if out_path.exists():
        raise FileExistsError("输出文件已存在，拒绝覆盖")
    since = since if since.tzinfo is not None else since.replace(tzinfo=UTC)
    session = session_factory()
    created = False
    try:
        rows = list(
            session.scalars(
                select(CallTrace)
                .where(CallTrace.started_at >= since)
                .order_by(CallTrace.started_at, CallTrace.id)
            )
        )
        payload = [trace_to_export_row(row) for row in rows]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(out_path.name + ".tmp")
        tmp.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in payload
            ),
            encoding="utf-8",
        )
        created = True
        tmp.replace(out_path)
        return len(payload)
    except Exception:
        if created:
            out_path.unlink(missing_ok=True)
        tmp = out_path.with_name(out_path.name + ".tmp")
        tmp.unlink(missing_ok=True)
        raise
    finally:
        session.close()


def assert_export_has_no_raw_secrets(
    text: str, forbidden: Mapping[str, str] | list[str] | tuple[str, ...] = ()
) -> None:
    """Test helper: exported text must not contain raw prompts, keys, or 'sk-'."""
    lowered = text
    if "sk-" in lowered:
        raise AssertionError("export contains sk-")
    items = forbidden.values() if isinstance(forbidden, Mapping) else forbidden
    for item in items:
        if item and item in text:
            raise AssertionError("export contains forbidden raw text")


# Keep a named length check used by tests and the report checker for hash fields.
def is_sha256_hex(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _HASH_LENGTH
        and all(ch in "0123456789abcdef" for ch in value)
    )
