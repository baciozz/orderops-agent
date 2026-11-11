"""Persistent call traces for live settlement and fixture calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from app.adapters.budget import BudgetLedger
from app.adapters.call_trace import (
    CallTraceStore,
    assert_export_has_no_raw_secrets,
    export_traces,
    parse_since,
)
from app.adapters.fixture_model import FixtureLLMClient
from app.adapters.live_model import LiveLLMClient
from app.cli import main as cli_main
from app.db.models import CallTrace, ModelUsage
from app.errors import AppError, FixtureNotFound
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from tests.test_stage4_live import ledger, request, response, settings

CUSTOMER_SPEECH = "客户原话：请把订单 PO-SECRET-CUST 全部取消"
FAKE_KEY = "sk-test-not-a-real-secret-value"


def _client(session_factory, handler, configured=None):
    configured = configured or settings()
    return LiveLLMClient(
        configured,
        ledger(session_factory, configured),
        transport=httpx.MockTransport(handler),
    )


def _row_blob(row):
    return json.dumps(
        {column: getattr(row, column) for column in row.__table__.columns.keys()},
        default=str,
    )


def _traces(session_factory):
    with session_factory() as session:
        return list(session.scalars(select(CallTrace).order_by(CallTrace.id)))


def _usage(session_factory):
    with session_factory() as session:
        return list(session.scalars(select(ModelUsage).order_by(ModelUsage.id)))


@pytest.mark.parametrize(
    ("handler", "expect_status", "error_code", "schema_valid"),
    [
        (
            lambda _: response(payload={"ok": True}),
            "ok",
            None,
            True,
        ),
        (
            lambda _: httpx.Response(401, json={"error": {"code": "invalid_api_key"}}),
            "error",
            "MODEL_AUTH",
            False,
        ),
        (
            lambda _: (_ for _ in ()).throw(httpx.ReadTimeout("timeout")),
            "error",
            "MODEL_TIMEOUT",
            False,
        ),
        (
            lambda _: httpx.Response(
                200,
                json={
                    "status": "completed",
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "not-json"}],
                        }
                    ],
                },
            ),
            "error",
            "SCHEMA_INVALID",
            False,
        ),
    ],
    ids=["ok", "auth", "timeout", "bad-json"],
)
def test_live_outcomes_each_write_one_trace(
    session_factory, handler, expect_status, error_code, schema_valid
):
    client = _client(session_factory, handler)
    call = request(user_prompt=CUSTOMER_SPEECH, system_prompt=FAKE_KEY)
    if expect_status == "ok":
        client.structured_call(call)
    else:
        with pytest.raises(AppError) as caught:
            client.structured_call(call)
        assert caught.value.code == error_code
    traces = _traces(session_factory)
    usage = _usage(session_factory)
    assert len(traces) == len(usage) == 1
    row = traces[0]
    account = usage[0]
    assert row.ledger_row_id == account.id
    assert account.status == "SETTLED"
    assert row.status == expect_status
    assert row.error_code == error_code
    assert row.schema_valid is schema_valid
    blob = _row_blob(row)
    assert CUSTOMER_SPEECH not in blob
    assert FAKE_KEY not in blob
    assert "sk-" not in blob


def test_settlement_rolls_back_when_trace_insert_fails(session_factory, monkeypatch):
    from app.db.models import CallTrace
    from sqlalchemy.orm import Session

    added = Session.add

    def add(session, obj, *rest, **kwargs):
        if isinstance(obj, CallTrace):
            raise RuntimeError("trace write failed")
        return added(session, obj, *rest, **kwargs)

    monkeypatch.setattr(Session, "add", add)
    client = _client(session_factory, lambda _: response(payload={"ok": True}))
    with pytest.raises(RuntimeError, match="trace write failed"):
        client.structured_call(request())
    usage = _usage(session_factory)
    assert len(usage) == 1 and usage[0].status == "RESERVED"
    assert _traces(session_factory) == []


def test_usage_update_failure_after_trace_flush_leaves_neither(session_factory, monkeypatch):
    from app.db.models import CallTrace

    def settle(self, reservation_id, **kwargs):
        session = self._sessions()
        try:
            from app.adapters.call_trace import build_call_trace
            from app.db.session import begin_immediate

            with begin_immediate(session):
                row = session.get(ModelUsage, reservation_id)
                assert row is not None
                trace = kwargs.get("trace")
                assert trace is not None
                session.add(
                    build_call_trace(
                        trace,
                        ledger_row_id=row.id,
                        input_tokens=kwargs["input_tokens"],
                        output_tokens=kwargs["output_tokens"],
                        cost_usd=Decimal("0"),
                    )
                )
                session.flush()
                assert (
                    session.get(CallTrace, session.scalars(select(CallTrace)).first().id)
                    is not None
                )
                raise RuntimeError("ledger write failed after trace flush")
        finally:
            session.close()

    monkeypatch.setattr(BudgetLedger, "settle", settle)
    client = _client(session_factory, lambda _: response(payload={"ok": True}))
    with pytest.raises(RuntimeError, match="ledger write failed after trace flush"):
        client.structured_call(request())
    usage = _usage(session_factory)
    assert len(usage) == 1 and usage[0].status == "RESERVED"
    assert _traces(session_factory) == []


def test_orphan_trace_without_ledger_is_rejected(session_factory):
    from app.adapters.call_trace import CallTraceDraft, build_call_trace
    from app.db.session import begin_immediate

    draft = CallTraceDraft(
        review_id=None,
        task_id="t1",
        agent_id="parser",
        provider="openai",
        model="gpt-6-luna",
        prompt_version="v",
        prompt_sha256="a" * 64,
        schema_sha256="b" * 64,
        input_sha256="c" * 64,
        started_at=datetime.now(UTC),
        elapsed_ms=1,
        status="ok",
        error_code=None,
        schema_valid=True,
    )
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            with begin_immediate(session):
                session.add(
                    build_call_trace(
                        draft,
                        ledger_row_id="MU-missing",
                        input_tokens=0,
                        output_tokens=0,
                        cost_usd=Decimal("0"),
                    )
                )
        session.rollback()
    assert _traces(session_factory) == []
    assert _usage(session_factory) == []


def test_fixture_mode_writes_trace_without_ledger(session_factory):
    client = FixtureLLMClient(
        {"demo": {"event_type": "query"}},
        traces=CallTraceStore(session_factory),
    )
    result = client.structured_call(
        request(fixture_key="demo", user_prompt=CUSTOMER_SPEECH, system_prompt=FAKE_KEY)
    )
    assert result.payload == {"event_type": "query"}
    traces = _traces(session_factory)
    assert len(traces) == 1
    row = traces[0]
    assert row.ledger_row_id is None
    assert row.provider == "fixture" and row.model == "fixture"
    assert row.status == "ok" and row.schema_valid is True
    assert row.cost_usd == Decimal("0")
    assert _usage(session_factory) == []
    blob = _row_blob(row)
    assert CUSTOMER_SPEECH not in blob and FAKE_KEY not in blob and "sk-" not in blob


def test_fixture_missing_key_writes_error_trace(session_factory):
    client = FixtureLLMClient({}, traces=CallTraceStore(session_factory))
    with pytest.raises(FixtureNotFound):
        client.structured_call(request(fixture_key="missing"))
    traces = _traces(session_factory)
    assert len(traces) == 1
    assert traces[0].status == "error" and traces[0].error_code == "FIXTURE_NOT_FOUND"
    assert traces[0].ledger_row_id is None
    assert _usage(session_factory) == []


def test_export_trace_omits_raw_text_and_refuses_overwrite(session_factory, tmp_path, monkeypatch):
    client = _client(
        session_factory,
        lambda _: response(payload={"ok": True}),
    )
    client.structured_call(request(user_prompt=CUSTOMER_SPEECH, system_prompt=FAKE_KEY))
    out = tmp_path / "traces.jsonl"
    count = export_traces(session_factory, since=datetime(2020, 1, 1, tzinfo=UTC), out_path=out)
    assert count == 1
    text = out.read_text(encoding="utf-8")
    assert_export_has_no_raw_secrets(text, [CUSTOMER_SPEECH, FAKE_KEY])
    assert "sk-" not in text
    assert CUSTOMER_SPEECH not in text
    row = json.loads(text)
    assert row["status"] == "ok"
    assert row["prompt_sha256"] and "prompt" not in row or "prompt_sha256" in row
    assert "user_prompt" not in row and "system_prompt" not in row and "payload" not in row
    with pytest.raises(FileExistsError):
        export_traces(session_factory, since=datetime(2020, 1, 1, tzinfo=UTC), out_path=out)


def test_export_trace_cli(session_factory, database_url, tmp_path, monkeypatch):
    FixtureLLMClient(
        {"demo": {"event_type": "query"}},
        traces=CallTraceStore(session_factory),
    ).structured_call(
        request(fixture_key="demo", user_prompt=CUSTOMER_SPEECH, system_prompt=FAKE_KEY)
    )
    out = tmp_path / "cli-traces.jsonl"
    monkeypatch.setenv("OOS_DATABASE_URL", database_url)
    # Avoid leftover live flags if any.
    monkeypatch.setenv("OOS_LIVE_ENABLED", "false")
    from app.config import reset_settings_cache

    reset_settings_cache()
    try:
        assert (
            cli_main(["export-trace", "--since", "2020-01-01T00:00:00+00:00", "--out", str(out)])
            == 0
        )
    finally:
        reset_settings_cache()
    text = out.read_text(encoding="utf-8")
    assert_export_has_no_raw_secrets(text, [CUSTOMER_SPEECH, FAKE_KEY])
    assert "sk-" not in text
    assert (
        cli_main(["export-trace", "--since", "2020-01-01T00:00:00+00:00", "--out", str(out)]) == 2
    )
    assert (
        cli_main(["export-trace", "--since", "not-a-date", "--out", str(tmp_path / "other.jsonl")])
        == 2
    )


def test_parse_since_accepts_naive_as_utc():
    assert parse_since("2026-11-11T12:00:00") == datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def test_trace_langfuse_defaults_off_and_is_not_imported():
    import sys

    from app.config import Settings

    assert Settings(_env_file=None).trace_langfuse is False
    assert not any(name == "langfuse" or name.startswith("langfuse.") for name in sys.modules)


def test_export_helpers_reject_raw_secrets_and_validate_hashes():
    from app.adapters.call_trace import is_sha256_hex

    with pytest.raises(AssertionError, match="sk-"):
        assert_export_has_no_raw_secrets("prefix sk-test suffix")
    with pytest.raises(AssertionError, match="forbidden"):
        assert_export_has_no_raw_secrets("visible customer text", ["customer text"])
    with pytest.raises(AssertionError, match="forbidden"):
        assert_export_has_no_raw_secrets("visible customer text", {"note": "customer text"})
    assert_export_has_no_raw_secrets("safe digest only")
    assert is_sha256_hex("ab" * 32) is True
    assert is_sha256_hex("AB" * 32) is False
    assert is_sha256_hex("short") is False


def test_export_traces_cleans_up_when_replace_fails(session_factory, tmp_path, monkeypatch):
    FixtureLLMClient(
        {"demo": {"event_type": "query"}},
        traces=CallTraceStore(session_factory),
    ).structured_call(request(fixture_key="demo"))
    out = tmp_path / "broken.jsonl"

    def fail(self, target):
        raise RuntimeError("replace failed")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(RuntimeError, match="replace failed"):
        export_traces(session_factory, since=datetime(2020, 1, 1, tzinfo=UTC), out_path=out)
    assert not out.exists()
    assert not (tmp_path / "broken.jsonl.tmp").exists()


def test_live_unexpected_exception_is_traced_as_internal_error(session_factory, monkeypatch):
    configured = settings()
    client = _client(session_factory, lambda _: response(payload={"ok": True}), configured)

    def explode(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(client._provider, "parse_response", explode)
    with pytest.raises(RuntimeError, match="provider exploded"):
        client.structured_call(request())
    traces = _traces(session_factory)
    usage = _usage(session_factory)
    assert len(traces) == 1 and traces[0].status == "error"
    assert traces[0].error_code == "INTERNAL_ERROR"
    assert traces[0].ledger_row_id == usage[0].id
    assert usage[0].status == "SETTLED"
