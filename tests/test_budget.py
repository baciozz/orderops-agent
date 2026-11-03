"""Budget guard: no guessed prices, hard $5 ceiling, persistent ledger, no network."""

import socket
from decimal import Decimal

import httpx
import pytest
from app.adapters.budget import BudgetLedger
from app.adapters.fixture_model import FixtureLLMClient
from app.adapters.live_model import LiveLLMClient
from app.adapters.llm import StructuredCallRequest, resolve_mode
from app.api.errors import error_payload
from app.config import HARD_BUDGET_CEILING_USD, Settings
from app.errors import BudgetExceeded, LiveStartRefused, PricesNotConfigured
from app.main import create_app
from fastapi.testclient import TestClient


def _ledger(session_factory, **kwargs) -> BudgetLedger:
    defaults = {
        "cap_usd": Decimal("5.00"),
        "price_in_per_mtok": Decimal("1"),
        "price_out_per_mtok": Decimal("2"),
    }
    defaults.update(kwargs)
    return BudgetLedger(session_factory, **defaults)


def _request(**kwargs) -> StructuredCallRequest:
    body = {
        "fixture_key": "demo",
        "input_tokens": 1,
        "max_output_tokens": 1,
        "response_schema_id": "parser.output",
        "prompt_ref": "prompts/demo",
    }
    body.update(kwargs)
    return StructuredCallRequest(**body)


def test_prices_missing_refuses_without_guessing(session_factory) -> None:
    ledger = _ledger(session_factory, price_in_per_mtok=None, price_out_per_mtok=None)
    with pytest.raises(PricesNotConfigured):
        ledger.authorize(input_tokens=10, max_output_tokens=10)
    assert ledger.spent() == Decimal("0.000000")


def test_code_ceiling_is_five_dollars_even_if_config_is_higher(session_factory) -> None:
    settings = Settings(_env_file=None, live_budget_usd=Decimal("100"))
    assert settings.live_budget_usd == Decimal("100")
    assert settings.effective_live_budget_usd == HARD_BUDGET_CEILING_USD
    ledger = _ledger(
        session_factory,
        cap_usd=Decimal("100"),
        price_in_per_mtok=Decimal("1000000"),
        price_out_per_mtok=Decimal("0"),
    )
    assert ledger.cap_usd == Decimal("5.00")
    with pytest.raises(BudgetExceeded) as exc:
        ledger.authorize(input_tokens=6, max_output_tokens=0)
    assert exc.value.code == "BUDGET_EXCEEDED"
    assert exc.value.http_status == 429
    payload = error_payload(exc.value, request_id="r", correlation_id="c")
    assert payload["error"]["code"] == "BUDGET_EXCEEDED"


def test_R22_budget_fuse_persists_across_restart(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'budget.db'}"
    from app.db.schema import init_db
    from app.db.session import make_engine, make_session_factory

    engine = make_engine(url, busy_timeout_ms=200)
    init_db(engine)
    factory = make_session_factory(engine)
    ledger = BudgetLedger(
        factory,
        cap_usd=Decimal("0.01"),
        price_in_per_mtok=Decimal("9"),
        price_out_per_mtok=Decimal("0"),
    )
    allowed = ledger.authorize(input_tokens=1000, max_output_tokens=0)
    assert allowed == Decimal("0.009000")
    ledger.record(provider="test", model="cheap", input_tokens=1000, output_tokens=0)
    engine.dispose()

    restarted = make_engine(url, busy_timeout_ms=200)
    try:
        again = BudgetLedger(
            make_session_factory(restarted),
            cap_usd=Decimal("0.01"),
            price_in_per_mtok=Decimal("9"),
            price_out_per_mtok=Decimal("0"),
        )
        assert again.spent() == Decimal("0.009000")
        with pytest.raises(BudgetExceeded) as exc:
            again.authorize(input_tokens=1000, max_output_tokens=0)
        assert exc.value.http_status == 429
        assert exc.value.code == "BUDGET_EXCEEDED"
        assert again.spent() == Decimal("0.009000")
    finally:
        restarted.dispose()


def test_R23_live_without_price_or_key_stays_fixture() -> None:
    missing_price = Settings(
        _env_file=None,
        live_enabled=True,
        llm_api_key="super-secret-key",
    )
    price_mode = resolve_mode(missing_price)
    assert price_mode.mode == "FIXTURE"
    assert price_mode.live_block_reason == "PRICES_NOT_CONFIGURED"
    app = create_app(missing_price)
    health = TestClient(app).get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["mode"] == "FIXTURE"
    assert body["live_block_reason"] == "PRICES_NOT_CONFIGURED"
    assert "super-secret-key" not in health.text

    missing_key = Settings(
        _env_file=None,
        live_enabled=True,
        price_in_per_mtok=Decimal("1"),
        price_out_per_mtok=Decimal("1"),
    )
    key_mode = resolve_mode(missing_key)
    assert key_mode.mode == "FIXTURE"
    assert key_mode.live_block_reason == "KEY_NOT_CONFIGURED"
    page = TestClient(create_app(missing_key)).get("/ui/tasks/T-STUB")
    assert page.status_code == 200
    assert "FIXTURE" in page.text
    assert "LIVE" not in page.text.split("运行模式：", 1)[1]


def test_live_client_refuses_to_construct_without_prices(session_factory) -> None:
    settings = Settings(_env_file=None, live_enabled=True, llm_api_key="super-secret-key")
    ledger = _ledger(session_factory, price_in_per_mtok=None, price_out_per_mtok=None)
    with pytest.raises(LiveStartRefused) as exc:
        LiveLLMClient(settings, ledger)
    assert exc.value.code == "PRICES_NOT_CONFIGURED"
    assert "super-secret-key" not in str(exc.value)


def test_over_budget_live_call_does_not_touch_the_network(session_factory, monkeypatch) -> None:
    def refuse_network(*_args, **_kwargs):
        raise AssertionError("测试不应建立网络连接")

    monkeypatch.setattr(socket, "socket", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    settings = Settings(
        _env_file=None,
        live_enabled=True,
        live_budget_usd=Decimal("0.01"),
        price_in_per_mtok=Decimal("9"),
        price_out_per_mtok=Decimal("0"),
        llm_api_key="super-secret-key",
        llm_provider="openai",
        llm_model="cheap",
    )
    ledger = BudgetLedger(
        session_factory,
        cap_usd=settings.effective_live_budget_usd,
        price_in_per_mtok=settings.price_in_per_mtok,
        price_out_per_mtok=settings.price_out_per_mtok,
    )
    ledger.record(provider="example", model="cheap", input_tokens=1000, output_tokens=0)
    client = LiveLLMClient(settings, ledger)
    with pytest.raises(BudgetExceeded):
        client.structured_call(_request(input_tokens=1000, max_output_tokens=0))


def test_under_budget_live_call_uses_mock_transport(session_factory) -> None:
    settings = Settings(
        _env_file=None,
        live_enabled=True,
        price_in_per_mtok=Decimal("1"),
        price_out_per_mtok=Decimal("1"),
        llm_api_key="super-secret-key",
        llm_provider="openai",
        llm_model="mock-model",
    )
    ledger = _ledger(session_factory)

    def respond(request):
        assert request.url.path == "/v1/responses"
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}],
            },
        )

    client = LiveLLMClient(settings, ledger, transport=httpx.MockTransport(respond))
    result = client.structured_call(_request())
    assert result.usage.mode == "LIVE"
    assert "super-secret-key" not in result.model_dump_json()
    assert ledger.spent() == Decimal("0.000003")


def test_fixture_call_does_not_touch_the_network(monkeypatch) -> None:
    def refuse_network(*_args, **_kwargs):
        raise AssertionError("fixture 不应建立网络连接")

    monkeypatch.setattr(socket, "socket", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    client = FixtureLLMClient(fixtures={"demo": {"event_type": "query"}})
    result = client.structured_call(_request())
    assert result.usage.mode == "FIXTURE"
    assert result.payload == {"event_type": "query"}
