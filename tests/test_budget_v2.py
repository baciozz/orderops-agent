"""v2 budget: $30 ceiling, 3000-call ledger limit, per-model prices, unpriced models refused.

All prices here are synthetic test values, not provider prices. No network.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from app.adapters.budget import BudgetLedger
from app.adapters.live_model import LiveLLMClient
from app.adapters.llm import StructuredCallRequest, resolve_mode
from app.config import HARD_BUDGET_CEILING_USD, HARD_MAX_TOTAL_CALLS, Settings
from app.db.models import ModelUsage
from app.errors import BudgetExceeded, CallLimitExceeded, LiveStartRefused, PricesNotConfigured
from pydantic import ValidationError
from sqlalchemy import func, select

MAIN = "gpt-6-luna"
STRONG = "gpt-6.1-sol"
PRICES = {
    MAIN: {"in_per_mtok": "2", "out_per_mtok": "8"},
    STRONG: {"in_per_mtok": "20", "out_per_mtok": "80"},
}


def _settings(**overrides) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "live_enabled": True,
        "llm_api_key": "mock-credential-not-real",
        "llm_provider": "openai",
        "llm_model": MAIN,
        "model_prices": PRICES,
    }
    values.update(overrides)
    return Settings(**values)


def _request() -> StructuredCallRequest:
    return StructuredCallRequest(
        input_tokens=1,
        max_output_tokens=10,
        response_schema_id="parser.output",
        prompt_ref="prompts/demo",
    )


def _respond(calls: list[httpx.Request], *, input_tokens: int, output_tokens: int):
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}],
            },
        )

    return handle


def _row_count(session_factory) -> int:
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(ModelUsage)) or 0


def test_ceiling_is_thirty_dollars_and_default_budget_stays_five(session_factory) -> None:
    # $30 is only the code ceiling; each stage must set its cap explicitly at runtime.
    assert HARD_BUDGET_CEILING_USD == Decimal("30.00")
    assert Settings(_env_file=None).effective_live_budget_usd == Decimal("5.00")
    configured = _settings(
        live_budget_usd=Decimal("100"),
        model_prices={MAIN: {"in_per_mtok": "1000000", "out_per_mtok": "0"}},
    )
    assert configured.effective_live_budget_usd == Decimal("30.00")
    ledger = BudgetLedger.from_settings(session_factory, configured)
    assert ledger.cap_usd == Decimal("30.00")
    ledger.reserve(provider="openai", model=MAIN, input_tokens=29, max_output_tokens=0)
    ledger.reserve(provider="openai", model=MAIN, input_tokens=1, max_output_tokens=0)
    assert ledger.spent() == Decimal("30.000000")
    with pytest.raises(BudgetExceeded):
        ledger.reserve(provider="openai", model=MAIN, input_tokens=1, max_output_tokens=0)
    assert _row_count(session_factory) == 2


def test_ledger_refuses_the_3001st_call_even_if_caller_asks_for_more(session_factory) -> None:
    assert HARD_MAX_TOTAL_CALLS == 3000
    free = _settings(model_prices={MAIN: {"in_per_mtok": "0", "out_per_mtok": "0"}})
    for requested in (None, 5000):
        assert (
            BudgetLedger.from_settings(
                session_factory, free, max_total_calls=requested
            ).max_total_calls
            == 3000
        )
    ledger = BudgetLedger.from_settings(session_factory, free, max_total_calls=10_000)
    now = datetime.now(UTC)
    with session_factory() as session, session.begin():
        session.add_all(
            ModelUsage(
                id=f"MU-seed-{index:05d}",
                provider="openai",
                model=MAIN,
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal("0"),
                price_version="in=0;out=0",
                at=now,
                status="SETTLED",
            )
            for index in range(2999)
        )
    ledger.reserve(provider="openai", model=MAIN, input_tokens=0, max_output_tokens=0)
    assert _row_count(session_factory) == 3000
    with pytest.raises(CallLimitExceeded) as exc:
        ledger.reserve(provider="openai", model=MAIN, input_tokens=0, max_output_tokens=0)
    assert exc.value.details == {"calls": 3000, "limit": 3000}
    # The legacy constructor is bound by the same ledger-level limit.
    legacy = BudgetLedger(
        session_factory, cap_usd=Decimal("1"), price_in_per_mtok=0, price_out_per_mtok=0
    )
    with pytest.raises(CallLimitExceeded):
        legacy.reserve(provider="openai", model="any", input_tokens=0, max_output_tokens=0)
    assert _row_count(session_factory) == 3000


def test_two_models_are_billed_at_their_own_prices(session_factory, monkeypatch) -> None:
    monkeypatch.setenv("OOS_MODEL_PRICES", json.dumps(PRICES))
    monkeypatch.setenv("OOS_LLM_MODEL", MAIN)
    configured = Settings(_env_file=None)
    assert configured.prices_for(MAIN) == (Decimal("2"), Decimal("8"))
    assert configured.prices_for(STRONG) == (Decimal("20"), Decimal("80"))
    ledger = BudgetLedger.from_settings(session_factory, _settings())
    calls: list[httpx.Request] = []
    results = {}
    for model in (MAIN, STRONG):
        client = LiveLLMClient(
            _settings(llm_model=model),
            ledger,
            transport=httpx.MockTransport(
                _respond(calls, input_tokens=100_000, output_tokens=10_000)
            ),
        )
        results[model] = client.structured_call(_request())
    assert len(calls) == 2
    assert [json.loads(call.content)["model"] for call in calls] == [MAIN, STRONG]
    # 0.1M input + 0.01M output: 0.2 + 0.08 and 2 + 0.8.
    assert results[MAIN].usage.cost_usd == "0.280000"
    assert results[STRONG].usage.cost_usd == "2.800000"
    assert ledger.spent() == Decimal("3.080000")
    with session_factory() as session:
        rows = {row.model: row for row in session.scalars(select(ModelUsage))}
    assert (rows[MAIN].price_in_per_mtok, rows[MAIN].price_out_per_mtok) == (2, 8)
    assert (rows[STRONG].price_in_per_mtok, rows[STRONG].price_out_per_mtok) == (20, 80)
    assert all(row.status == "SETTLED" for row in rows.values())


def test_unpriced_model_is_refused_without_any_request(session_factory) -> None:
    # No default price exists for any model, including the strong model.
    assert Settings(_env_file=None).model_prices == {}
    assert Settings(_env_file=None).prices_for(STRONG) is None
    only_main = {MAIN: PRICES[MAIN]}
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(_respond(calls, input_tokens=1, output_tokens=1))

    unpriced = _settings(llm_model=STRONG, model_prices=only_main)
    assert resolve_mode(unpriced).live_block_reason == "PRICES_NOT_CONFIGURED"
    with pytest.raises(LiveStartRefused):
        LiveLLMClient(
            unpriced, BudgetLedger.from_settings(session_factory, unpriced), transport=transport
        )

    # A client whose own settings price the model still cannot spend from a
    # ledger that does not: the ledger refuses before the transport is touched.
    strict = BudgetLedger.from_settings(session_factory, _settings(model_prices=only_main))
    client = LiveLLMClient(_settings(llm_model=STRONG), strict, transport=transport)
    with pytest.raises(PricesNotConfigured) as exc:
        client.structured_call(_request())
    assert exc.value.details == {"model": STRONG}
    with pytest.raises(PricesNotConfigured):
        strict.reserve(provider="openai", model=STRONG, input_tokens=1, max_output_tokens=1)

    # The legacy pair prices the configured model only, never another one.
    legacy = _settings(
        model_prices={}, price_in_per_mtok=Decimal("2"), price_out_per_mtok=Decimal("8")
    )
    assert legacy.prices_for(STRONG) is None
    with pytest.raises(PricesNotConfigured):
        BudgetLedger.from_settings(session_factory, legacy).reserve(
            provider="openai", model=STRONG, input_tokens=1, max_output_tokens=1
        )
    assert calls == []
    assert _row_count(session_factory) == 0


def test_legacy_price_variables_still_price_the_configured_model(
    session_factory, monkeypatch
) -> None:
    monkeypatch.setenv("OOS_LIVE_ENABLED", "true")
    monkeypatch.setenv("OOS_LLM_MODEL", MAIN)
    monkeypatch.setenv("OOS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OOS_LLM_API_KEY", "mock-credential-not-real")
    monkeypatch.setenv("OOS_PRICE_IN_PER_MTOK", "2")
    monkeypatch.setenv("OOS_PRICE_OUT_PER_MTOK", "8")
    monkeypatch.delenv("OOS_MODEL_PRICES", raising=False)
    configured = Settings(_env_file=None)
    assert configured.model_prices == {}
    assert configured.priced_models() == {MAIN: (Decimal("2"), Decimal("8"))}
    assert resolve_mode(configured).mode == "LIVE"
    ledger = BudgetLedger.from_settings(session_factory, configured)
    calls: list[httpx.Request] = []
    client = LiveLLMClient(
        configured,
        ledger,
        transport=httpx.MockTransport(_respond(calls, input_tokens=1_000_000, output_tokens=0)),
    )
    assert client.structured_call(_request()).usage.cost_usd == "2.000000"
    assert len(calls) == 1
    # Same price in both places is fine; different prices are a refusal, not a guess.
    _settings(price_in_per_mtok=Decimal("2.0"), price_out_per_mtok=Decimal("8"))
    with pytest.raises(ValidationError):
        _settings(price_in_per_mtok=Decimal("3"), price_out_per_mtok=Decimal("8"))


@pytest.mark.parametrize(
    "table",
    [
        {MAIN: {"in_per_mtok": "-1", "out_per_mtok": "1"}},
        {MAIN: {"in_per_mtok": "1"}},
        {MAIN: {"in_per_mtok": "abc", "out_per_mtok": "1"}},
        {MAIN: {"in_per_mtok": "1", "out_per_mtok": "1", "cached": "1"}},
        {" ": {"in_per_mtok": "1", "out_per_mtok": "1"}},
    ],
)
def test_malformed_model_price_table_is_rejected(table) -> None:
    with pytest.raises(ValidationError):
        _settings(model_prices=table)


def test_malformed_model_price_json_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("OOS_MODEL_PRICES", "{not json")
    with pytest.raises(ValueError):
        Settings(_env_file=None)
