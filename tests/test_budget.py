"""Budget guard: no guessed prices, hard $5 ceiling, persistent ledger, no network."""

import socket
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import httpx
import pytest
from app.adapters.budget import BudgetLedger
from app.adapters.fixture_model import FixtureLLMClient
from app.adapters.live_model import LiveLLMClient
from app.adapters.llm import StructuredCallRequest, resolve_mode
from app.api.errors import error_payload
from app.config import HARD_BUDGET_CEILING_USD, Settings
from app.db.models import ModelUsage
from app.db.schema import init_db
from app.db.session import make_engine, make_session_factory
from app.errors import BudgetExceeded, CallLimitExceeded, LiveStartRefused, PricesNotConfigured
from app.main import create_app
from app.orchestration.inbox import accept_input
from fastapi.testclient import TestClient
from sqlalchemy import select


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


@pytest.fixture
def race_ledger(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'race.db'}", busy_timeout_ms=5000)
    init_db(engine)
    book = _ledger(
        make_session_factory(engine),
        cap_usd=Decimal("3"),
        price_in_per_mtok=Decimal("1000000"),
        price_out_per_mtok=Decimal("0"),
    )
    yield book, make_session_factory(engine)
    engine.dispose()


def _race_reservations(book, error, **kwargs):
    barrier = Barrier(12)

    def reserve(_):
        barrier.wait(timeout=10)
        try:
            reservation_id = book.reserve(provider="test", model="race", **kwargs)
        except error as exc:
            return exc
        assert book.spent() <= book.cap_usd
        return reservation_id

    with ThreadPoolExecutor(max_workers=12) as pool:
        return list(pool.map(reserve, range(12)))


def test_concurrent_reservations_cannot_exceed_budget(race_ledger):
    book, factory = race_ledger
    results = _race_reservations(book, BudgetExceeded, input_tokens=1, max_output_tokens=0)
    assert sum(isinstance(result, str) for result in results) == 3
    assert sum(isinstance(result, BudgetExceeded) for result in results) == 9
    assert book.spent() == Decimal("3.000000")
    with factory() as session:
        assert len(session.scalars(select(ModelUsage)).all()) == 3


def test_concurrent_reservations_cannot_exceed_task_call_limit(race_ledger):
    book, factory = race_ledger
    _, task_id, _ = accept_input(factory, thread_id="race", client_event_key="one", text="x")
    results = _race_reservations(
        book,
        CallLimitExceeded,
        input_tokens=0,
        max_output_tokens=0,
        task_id=task_id,
        max_calls_per_task=3,
    )
    assert sum(isinstance(result, str) for result in results) == 3
    assert sum(isinstance(result, CallLimitExceeded) for result in results) == 9
    assert book.calls_for_task(task_id) == 3
    assert book.spent() == Decimal("0.000000")


@pytest.mark.parametrize("actual_tokens", [10, 2000])
def test_settlement_updates_reservation_even_when_provider_exceeds_estimate(
    session_factory, actual_tokens
):
    book = _ledger(
        session_factory,
        cap_usd=Decimal("0.01"),
        price_in_per_mtok=Decimal("9"),
        price_out_per_mtok=Decimal("0"),
    )
    reservation_id = book.reserve(
        provider="test", model="mock", input_tokens=1000, max_output_tokens=0
    )
    assert book.spent() == Decimal("0.009000")
    row = book.settle(reservation_id, input_tokens=actual_tokens, output_tokens=0)
    assert row.id == reservation_id
    assert row.input_tokens == actual_tokens
    assert book.spent() == Decimal(actual_tokens) * Decimal("0.000009")
    with session_factory() as session:
        assert len(session.scalars(select(ModelUsage)).all()) == 1
    if actual_tokens == 2000:
        with pytest.raises(BudgetExceeded):
            book.reserve(provider="test", model="mock", input_tokens=1, max_output_tokens=0)


def test_prices_missing_refuses_without_guessing(session_factory) -> None:
    ledger = _ledger(session_factory, price_in_per_mtok=None, price_out_per_mtok=None)
    with pytest.raises(PricesNotConfigured):
        ledger.reserve(provider="test", model="cheap", input_tokens=10, max_output_tokens=10)
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
        ledger.reserve(provider="test", model="cheap", input_tokens=6, max_output_tokens=0)
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
    allowed = ledger.reserve(provider="test", model="cheap", input_tokens=1000, max_output_tokens=0)
    assert allowed.startswith("MU-")
    assert ledger.spent() == Decimal("0.009000")
    # Simulate a crash before settlement: the reservation survives a restart.
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
            again.reserve(provider="test", model="cheap", input_tokens=1000, max_output_tokens=0)
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
    health = TestClient(base_url="http://127.0.0.1", app=app).get("/health")
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
    page = TestClient(base_url="http://127.0.0.1", app=create_app(missing_key)).get(
        "/ui/tasks/T-STUB"
    )
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
    ledger.reserve(provider="example", model="cheap", input_tokens=1000, max_output_tokens=0)
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
