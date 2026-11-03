"""Compatible Chat providers are exercised only through MockTransport, with no sockets."""

import json
import logging
import socket
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from app.adapters.budget import BudgetLedger
from app.adapters.live_model import LiveLLMClient
from app.adapters.live_prompts import parser_schema
from app.adapters.llm import StructuredCallRequest, resolve_mode
from app.config import Settings
from app.db.models import Approval, DispatchStep, ModelUsage, Operation, SalesOrder
from app.db.schema import init_db
from app.db.session import make_engine, make_session_factory
from app.errors import AppError, BudgetExceeded, CallLimitExceeded, LiveStartRefused, SchemaInvalid
from app.orchestration.dispatch import dispatch_text
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from app.orchestration.inbox import accept_input
from app.orchestration.worker import run_once
from app.services.seed import load_seed
from app.services.task_view import build_task_view
from sqlalchemy import func, inspect, select

NOW = datetime(2026, 11, 3, tzinfo=UTC)
MOCK_KEY = "offline_compat_credential"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Compatible provider tests must never open sockets or resolve DNS")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def configured(**overrides):
    values = dict(
        _env_file=None,
        live_enabled=True,
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:11434/v1",
        llm_model="offline-model",
        price_in_per_mtok=Decimal("0.10"),
        price_out_per_mtok=Decimal("0.50"),
    )
    values.update(overrides)
    return Settings(**values)


def ledger(factory, settings):
    return BudgetLedger(
        factory,
        cap_usd=settings.effective_live_budget_usd,
        price_in_per_mtok=settings.price_in_per_mtok,
        price_out_per_mtok=settings.price_out_per_mtok,
    )


def request(**overrides):
    values = dict(
        response_schema_id="parser.output",
        prompt_ref="live://parser",
        system_prompt="系统：解析订单。",
        user_prompt=TEXT_BLACK_TO_WHITE,
        response_schema=parser_schema(),
        max_output_tokens=1000,
    )
    values.update(overrides)
    return StructuredCallRequest(**values)


def intent(**overrides):
    values = dict(
        order_candidates=["SO-1001"],
        customer_candidate=None,
        line_changes=[
            dict(
                mode="replace_remaining",
                sku_id=None,
                from_sku="A-BLACK-M",
                to_sku="A-WHITE-M",
                qty=40,
            )
        ],
        new_lines=[],
        missing_fields=[],
        conflicts=[],
        questions=[],
        event_type="CHANGE_ORDER",
        order_id="SO-1001",
        clarification_choice=None,
    )
    values.update(overrides)
    return values


def response_data():
    return {
        "choices": [{"message": {"content": json.dumps(intent())}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
    }


@pytest.mark.parametrize("strict", [False, True], ids=["json_object", "json_schema"])
@pytest.mark.parametrize(
    "base_url,key",
    [
        ("http://127.0.0.1:11434/v1/", None),
        ("http://localhost:8000/v1", None),
        ("http://[::1]:8000/v1", None),
        ("https://api.deepseek.com/v1", MOCK_KEY),
        ("http://localhost:8000/v1", MOCK_KEY),
    ],
)
def test_compat_request_shape_and_happy_parse(session_factory, caplog, strict, base_url, key):
    caplog.set_level(logging.DEBUG)
    settings = configured(
        llm_base_url=base_url,
        llm_api_key=key,
        llm_json_schema_supported=strict,
        model_timeout_s=17,
    )
    book = ledger(session_factory, settings)
    calls = []
    req = request()

    def handle(wire):
        calls.append(wire)
        assert str(wire.url) == base_url.rstrip("/") + "/chat/completions"
        assert wire.method == "POST"
        if key:
            assert wire.headers["authorization"] == f"Bearer {key}"
        else:
            assert "authorization" not in wire.headers
        body = json.loads(wire.content)
        assert set(body) == {
            "model",
            "messages",
            "max_tokens",
            "temperature",
            "stream",
            "response_format",
        }
        assert body["model"] == "offline-model"
        assert body["max_tokens"] == 1000
        assert body["temperature"] == 0
        assert body["stream"] is False
        system, user = body["messages"]
        assert user == {"role": "user", "content": req.user_prompt}
        assert system["role"] == "system"
        if strict:
            assert system["content"] == req.system_prompt
            assert body["response_format"] == {
                "type": "json_schema",
                "json_schema": {"name": "parser_output", "schema": parser_schema(), "strict": True},
            }
        else:
            assert body["response_format"] == {"type": "json_object"}
            assert system["content"].startswith(req.system_prompt)
            assert json.dumps(parser_schema()) in system["content"]
        assert wire.extensions["timeout"]["read"] == 17
        assert MOCK_KEY not in wire.content.decode()
        return httpx.Response(200, json=response_data())

    result = LiveLLMClient(
        settings, book, transport=httpx.MockTransport(handle), clock=lambda: NOW
    ).structured_call(req)
    assert len(calls) == 1
    assert result.payload == intent()
    assert result.usage.mode == "LIVE" and result.usage.provider == "openai_compatible"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (1000, 200)
    assert result.usage.cost_usd == "0.000200"
    assert result.usage.price_version == "in=0.10;out=0.50"
    with session_factory() as session:
        row = session.scalar(select(ModelUsage))
        assert (row.provider, row.model) == ("openai_compatible", "offline-model")
        assert row.at.replace(tzinfo=UTC) == NOW
        assert MOCK_KEY not in str(vars(row))
    assert MOCK_KEY not in result.model_dump_json() + caplog.text


@pytest.mark.parametrize("strict", [False, True], ids=["json_object", "json_schema"])
@pytest.mark.parametrize(
    "content",
    [
        "not JSON",
        "```json\n{}\n```",
        "[]",
        "null",
        json.dumps(intent(approved=True)),
        json.dumps(intent(order_id=123)),
        json.dumps(intent(questions="question")),
        json.dumps(intent(questions=[42])),
        json.dumps(intent(event_type="APPROVE")),
        json.dumps(intent(clarification_choice="approve")),
        json.dumps(intent(line_changes=[dict(intent()["line_changes"][0], qty=True)])),
        json.dumps(intent(line_changes=[dict(intent()["line_changes"][0], qty="40")])),
        json.dumps(intent(line_changes=[dict(intent()["line_changes"][0], approved=True)])),
        json.dumps({key: value for key, value in intent().items() if key != "order_id"}),
    ],
    ids=[
        "not_json",
        "markdown",
        "array",
        "null",
        "approval_field",
        "wrong_type",
        "not_array",
        "wrong_item_type",
        "enum",
        "nullable_enum",
        "bool_quantity",
        "string_quantity",
        "nested_extra",
        "required",
    ],
)
def test_compat_invalid_output_is_schema_invalid_and_billed(session_factory, strict, content):
    settings = configured(llm_json_schema_supported=strict)
    book = ledger(session_factory, settings)
    data = response_data()
    data["choices"][0]["message"]["content"] = content
    with pytest.raises(SchemaInvalid) as caught:
        LiveLLMClient(
            settings,
            book,
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data)),
        ).structured_call(request())
    assert caught.value.code == "SCHEMA_INVALID"
    assert caught.value.retryable is False
    with session_factory() as session:
        rows = session.scalars(select(ModelUsage)).all()
        assert len(rows) == 1
        assert (rows[0].input_tokens, rows[0].output_tokens) == (1000, 200)
    assert book.spent() == Decimal("0.000200")


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": True, "completion_tokens": 200},
        {"prompt_tokens": 1000, "completion_tokens": -1},
        {"prompt_tokens": "1000", "completion_tokens": 200},
    ],
    ids=["missing", "empty", "bool", "negative", "string"],
)
def test_compat_missing_or_invalid_usage_settles_pessimistically(session_factory, usage):
    settings = configured()
    book = ledger(session_factory, settings)
    authorized = []
    data = response_data()
    data["usage"] = usage
    req = request(input_tokens=1)

    def handle(wire):
        with session_factory() as session:
            row = session.scalar(select(ModelUsage))
            authorized.append((row.input_tokens, row.output_tokens, row.cost_usd))
        # The fallback's schema instructions must also be reserved before HTTP.
        system, user = json.loads(wire.content)["messages"]
        actual_prompt_bytes = len((system["content"] + user["content"]).encode("utf-8"))
        assert authorized[0][0] >= actual_prompt_bytes + 256
        return httpx.Response(200, json=data)

    with pytest.raises(SchemaInvalid):
        LiveLLMClient(settings, book, transport=httpx.MockTransport(handle)).structured_call(req)
    with session_factory() as session:
        rows = session.scalars(select(ModelUsage)).all()
        assert len(rows) == 1
        assert (rows[0].input_tokens, rows[0].output_tokens, rows[0].cost_usd) == authorized[0]


@pytest.mark.parametrize("reason", ["length", "content_filter", None])
def test_compat_incomplete_settles_reported_usage(session_factory, reason):
    settings = configured()
    book = ledger(session_factory, settings)
    data = response_data()
    data["choices"][0]["finish_reason"] = reason
    with pytest.raises(AppError) as caught:
        LiveLLMClient(
            settings,
            book,
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data)),
        ).structured_call(request())
    assert caught.value.code == "MODEL_INCOMPLETE"
    assert caught.value.retryable is False
    assert book.spent() == Decimal("0.000200")


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "MODEL_AUTH"),
        (403, "MODEL_AUTH"),
        (429, "MODEL_RATE_LIMIT"),
        (500, "MODEL_UPSTREAM"),
        (503, "MODEL_UPSTREAM"),
        (400, "MODEL_HTTP"),
    ],
)
@pytest.mark.parametrize("has_usage", [False, True])
def test_compat_http_mapping_no_retry_and_safe_metadata(
    session_factory, caplog, status, code, has_usage
):
    caplog.set_level(logging.DEBUG)
    settings = configured(llm_base_url="https://provider.example/v1", llm_api_key=MOCK_KEY)
    book = ledger(session_factory, settings)
    calls = []

    def handle(req):
        calls.append(req)
        data = {"error": {"type": MOCK_KEY, "code": "rejected", "message": MOCK_KEY}}
        if has_usage:
            data["usage"] = response_data()["usage"]
        return httpx.Response(status, json=data)

    with pytest.raises(AppError) as caught:
        LiveLLMClient(settings, book, transport=httpx.MockTransport(handle)).structured_call(
            request()
        )
    assert caught.value.code == code and caught.value.retryable is False
    assert caught.value.details == {
        "status_code": status,
        "error_type": "REDACTED",
        "error_code": "rejected",
    }
    assert len(calls) == 1
    assert MOCK_KEY not in str(caught.value) + str(caught.value.details) + caplog.text
    with session_factory() as session:
        rows = session.scalars(select(ModelUsage)).all()
        assert len(rows) == 1
        assert (rows[0].input_tokens, rows[0].output_tokens) == (
            (1000, 200) if has_usage else (0, 0)
        )
        assert MOCK_KEY not in str(vars(rows[0]))


def test_compat_budget_refuses_before_http(session_factory):
    settings = configured(live_budget_usd=Decimal("0.000001"))
    book = ledger(session_factory, settings)
    calls = []

    def handle(req):
        calls.append(req)
        return httpx.Response(200, json=response_data())

    with pytest.raises(BudgetExceeded):
        LiveLLMClient(settings, book, transport=httpx.MockTransport(handle)).structured_call(
            request()
        )
    assert calls == []
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0


@pytest.mark.parametrize("failure", ["http", "schema", "missing_usage", "length", "timeout"])
def test_compat_task_call_limit_counts_failures_across_clients(session_factory, failure):
    _, task_id, _ = accept_input(
        session_factory, thread_id="calls", client_event_key="one", text="x"
    )
    settings = configured(model_max_calls_per_task=1)
    calls = []

    def handle(req):
        calls.append(req)
        data = response_data()
        if failure == "http":
            return httpx.Response(503, json={"error": {"type": "rejected"}})
        if failure == "schema":
            data["choices"][0]["message"]["content"] = json.dumps(intent(approved=True))
        if failure == "missing_usage":
            del data["usage"]
        if failure == "length":
            data["choices"][0]["finish_reason"] = "length"
        if failure == "timeout":
            raise httpx.ReadTimeout("offline", request=req)
        return httpx.Response(200, json=data)

    for attempt in range(2):
        client = LiveLLMClient(
            settings, ledger(session_factory, settings), transport=httpx.MockTransport(handle)
        )
        with pytest.raises(CallLimitExceeded if attempt else AppError):
            client.structured_call(request(task_id=task_id))
    assert len(calls) == 1
    assert ledger(session_factory, settings).calls_for_task(task_id) == 1
    with session_factory() as session:
        view = build_task_view(session, task_id, request_id="r", correlation_id="c", now=NOW)
        assert view.card.mode == "LIVE"


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"live_enabled": False}, "LIVE_DISABLED"),
        ({"llm_base_url": None}, "BASE_URL_UNSUPPORTED"),
        ({"llm_base_url": ""}, "BASE_URL_UNSUPPORTED"),
        ({"llm_model": None}, "MODEL_NOT_CONFIGURED"),
        ({"llm_model": " "}, "MODEL_NOT_CONFIGURED"),
        ({"llm_base_url": "https://provider.example/v1"}, "KEY_NOT_CONFIGURED"),
        (
            {"llm_base_url": "http://provider.example/v1", "llm_api_key": MOCK_KEY},
            "BASE_URL_INSECURE",
        ),
        ({"llm_base_url": "http://localhost.evil.example/v1"}, "BASE_URL_INSECURE"),
        ({"llm_base_url": "https://user:pass@provider.example/v1"}, "BASE_URL_UNSUPPORTED"),
        ({"llm_base_url": "http://user@localhost/v1"}, "BASE_URL_UNSUPPORTED"),
        ({"llm_base_url": "http://localhost/v1?x=1"}, "BASE_URL_UNSUPPORTED"),
        ({"llm_base_url": "http://localhost/v1#fragment"}, "BASE_URL_UNSUPPORTED"),
        ({"llm_base_url": "http://localhost/v1?"}, "BASE_URL_UNSUPPORTED"),
        ({"llm_base_url": "http://localhost:invalid/v1"}, "BASE_URL_UNSUPPORTED"),
        ({"llm_base_url": "http://[::1/v1"}, "BASE_URL_UNSUPPORTED"),
        ({"llm_base_url": "http://localhost\\@evil.example/v1"}, "BASE_URL_UNSUPPORTED"),
        ({"llm_base_url": "http://local\nhost/v1"}, "BASE_URL_UNSUPPORTED"),
        ({"llm_base_url": "file:///v1"}, "BASE_URL_UNSUPPORTED"),
        ({"llm_base_url": "ftp://localhost/v1"}, "BASE_URL_INSECURE"),
        ({"price_in_per_mtok": None}, "PRICES_NOT_CONFIGURED"),
        ({"price_out_per_mtok": None}, "PRICES_NOT_CONFIGURED"),
    ],
)
def test_compat_invalid_config_stays_fixture_and_refuses_start(session_factory, overrides, reason):
    settings = configured(**overrides)
    resolution = resolve_mode(settings)
    assert resolution.mode == "FIXTURE"
    assert resolution.live_block_reason == (None if reason == "LIVE_DISABLED" else reason)
    with pytest.raises(LiveStartRefused) as caught:
        LiveLLMClient(
            settings,
            ledger(session_factory, settings),
            transport=httpx.MockTransport(lambda req: pytest.fail("Invalid config reached HTTP")),
        )
    assert caught.value.reason == reason
    assert MOCK_KEY not in str(caught.value) + str(caught.value.details)


def test_compat_requires_explicit_base_url_and_env_mapping(monkeypatch):
    monkeypatch.setenv("OOS_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OOS_LIVE_ENABLED", "true")
    monkeypatch.setenv("OOS_LLM_MODEL", "offline-model")
    monkeypatch.setenv("OOS_PRICE_IN_PER_MTOK", "0")
    monkeypatch.setenv("OOS_PRICE_OUT_PER_MTOK", "0")
    assert Settings(_env_file=None).llm_base_url is None
    assert resolve_mode(Settings(_env_file=None)).mode == "FIXTURE"
    monkeypatch.setenv("OOS_LLM_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("OOS_LLM_JSON_SCHEMA_SUPPORTED", "true")
    settings = Settings(_env_file=None)
    assert settings.llm_json_schema_supported is True
    assert settings.llm_api_key_value() is None
    assert resolve_mode(settings).mode == "LIVE"
    monkeypatch.delenv("OOS_LLM_BASE_URL")
    assert (
        Settings(_env_file=None, llm_provider="openai").llm_base_url == "https://api.openai.com/v1"
    )


def test_compat_explicit_zero_prices_are_allowed_and_still_count_calls(session_factory):
    settings = configured(price_in_per_mtok=Decimal("0"), price_out_per_mtok=Decimal("0"))
    book = ledger(session_factory, settings)
    result = LiveLLMClient(
        settings,
        book,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response_data())),
    ).structured_call(request())
    assert result.usage.cost_usd == "0.000000"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 1


@pytest.mark.parametrize("failure", ["echo", "timeout", "transport"])
def test_compat_key_never_reaches_errors_logs_or_stored_data(session_factory, caplog, failure):
    caplog.set_level(logging.DEBUG)
    settings = configured(llm_api_key=MOCK_KEY)
    book = ledger(session_factory, settings)

    def handle(req):
        if failure in {"timeout", "transport"}:
            error = httpx.ReadTimeout if failure == "timeout" else httpx.ConnectError
            raise error(f"unsafe {MOCK_KEY}", request=req)
        data = response_data()
        data["choices"][0]["message"]["content"] = json.dumps(intent(questions=[MOCK_KEY]))
        return httpx.Response(200, json=data)

    with pytest.raises(AppError) as caught:
        LiveLLMClient(settings, book, transport=httpx.MockTransport(handle)).structured_call(
            request()
        )
    assert (
        caught.value.code
        == {"echo": "SCHEMA_INVALID", "timeout": "MODEL_TIMEOUT", "transport": "MODEL_TRANSPORT"}[
            failure
        ]
    )
    assert caught.value.retryable is False
    assert MOCK_KEY not in str(caught.value) + str(caught.value.details) + caplog.text
    with session_factory() as session:
        rows = session.scalars(select(ModelUsage)).all()
        assert len(rows) == 1 and rows[0].cost_usd > 0
        assert MOCK_KEY not in str(vars(rows[0]))


def test_compat_http_options_and_redirect_refusal(session_factory, monkeypatch):
    settings = configured(llm_api_key=MOCK_KEY, model_timeout_s=17)
    real_client = httpx.Client
    options, calls = [], []

    def client(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs)

    def handle(req):
        calls.append(req)
        return httpx.Response(307, headers={"Location": "https://other.example/v1"})

    monkeypatch.setattr(httpx, "Client", client)
    with pytest.raises(AppError) as caught:
        LiveLLMClient(
            settings, ledger(session_factory, settings), transport=httpx.MockTransport(handle)
        ).structured_call(request())
    assert caught.value.code == "MODEL_HTTP"
    assert len(calls) == len(options) == 1
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert options[0]["timeout"] == 17


@pytest.mark.parametrize("strict", [False, True])
def test_dispatch_compat_matches_openai_kernel_outcome(tmp_path, strict, caplog, monkeypatch):
    outcomes = []
    caplog.set_level(logging.DEBUG)
    # Inject transport at the HTTP boundary so dispatch uses its real client factory.
    real_client = httpx.Client
    for provider in ("openai", "openai_compatible"):
        url = f"sqlite:///{tmp_path / (provider + '.db')}"
        engine = make_engine(url)
        init_db(engine)
        factory = make_session_factory(engine)
        try:
            load_seed(factory, database_url=url)
            settings = configured(
                llm_provider=provider,
                llm_base_url="https://api.openai.com/v1"
                if provider == "openai"
                else "http://localhost:8000/v1",
                llm_api_key=MOCK_KEY,
                llm_json_schema_supported=strict,
            )
            calls = []

            def handle(req, calls=calls):
                calls.append(req)
                if req.url.path.endswith("/responses"):
                    return httpx.Response(
                        200,
                        json={
                            "status": "completed",
                            "usage": {"input_tokens": 1000, "output_tokens": 200},
                            "output": [
                                {
                                    "type": "message",
                                    "content": [
                                        {"type": "output_text", "text": json.dumps(intent())}
                                    ],
                                }
                            ],
                        },
                    )
                assert req.url.path == "/v1/chat/completions"
                return httpx.Response(200, json=response_data())

            def client(handler=handle, **kwargs):
                assert kwargs["transport"] is None
                kwargs["transport"] = httpx.MockTransport(handler)
                return real_client(**kwargs)

            monkeypatch.setattr(httpx, "Client", client)
            _, task_id, _ = accept_input(
                factory,
                text=TEXT_BLACK_TO_WHITE,
                thread_id=provider,
                client_event_key=provider,
                clock=lambda: NOW,
            )
            outcome = run_once(factory, settings=settings, clock=lambda: NOW)
            assert outcome.task_id == task_id
            assert outcome.status == "AWAITING_APPROVAL"
            assert outcome.requires_human_approval is True
            assert outcome.error_code is None
            assert len(calls) == 1
            with factory() as session:
                assert session.scalar(select(func.count()).select_from(Approval)) == 0
                assert session.scalar(select(func.count()).select_from(Operation)) == 0
                assert session.get(SalesOrder, "SO-1001").current_revision == 1
                row = session.scalar(select(ModelUsage))
                assert row.provider == provider
                step = session.scalar(select(DispatchStep).where(DispatchStep.agent_id == "parser"))
                assert step.tokens == 1200 and step.cost_usd == Decimal("0.000200")
                view = build_task_view(
                    session, outcome.task_id, request_id="r", correlation_id="c", now=NOW
                )
                assert view.card.mode == "LIVE"
                # Scan every persisted column, including plans/audit/task fields.
                for table in inspect(engine).get_table_names():
                    rows = session.connection().exec_driver_sql(f'SELECT * FROM "{table}"').all()
                    assert MOCK_KEY not in str(rows)
                outcomes.append((outcome.status, outcome.requires_human_approval, view.line_diffs))
        finally:
            engine.dispose()
    assert outcomes[0] == outcomes[1]
    assert MOCK_KEY not in caplog.text


def test_compat_missing_prices_dispatch_card_stays_fixture(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    settings = configured(price_in_per_mtok=None)
    accept_input(
        session_factory,
        text=TEXT_BLACK_TO_WHITE,
        thread_id="fixture",
        client_event_key="fixture",
        clock=lambda: NOW,
    )
    outcome = run_once(
        session_factory,
        settings=settings,
        clock=lambda: NOW,
        client_factory=lambda *_: pytest.fail("Missing prices must not construct a live client"),
    )
    assert outcome.status == "AWAITING_APPROVAL"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0
        view = build_task_view(
            session, outcome.task_id, request_id="r", correlation_id="c", now=NOW
        )
        assert view.card.mode == "FIXTURE"


@pytest.mark.parametrize("key", [None, MOCK_KEY])
def test_dispatch_unknown_provider_refused_before_http(session_factory, key):
    settings = configured(llm_provider="unknown", llm_api_key=key)
    with pytest.raises(LiveStartRefused) as caught:
        dispatch_text(
            session_factory, text=TEXT_BLACK_TO_WHITE, client_event_key="unknown", settings=settings
        )
    assert caught.value.reason == "PROVIDER_UNSUPPORTED"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0


@pytest.mark.parametrize(
    "outcome",
    ["success", "schema", "missing_usage", "length", "http", "http_usage", "timeout", "transport"],
)
def test_compat_reserves_before_http_and_settles_once(session_factory, monkeypatch, outcome):
    settings = configured()
    book = ledger(session_factory, settings)
    reservations, settlements = [], []
    reserve, settle = book.reserve, book.settle

    def record_reserve(**kwargs):
        row_id = reserve(**kwargs)
        reservations.append(row_id)
        return row_id

    def record_settle(row_id, **kwargs):
        settlements.append(row_id)
        return settle(row_id, **kwargs)

    monkeypatch.setattr(book, "reserve", record_reserve)
    monkeypatch.setattr(book, "settle", record_settle)
    estimates = []

    def handle(req):
        assert len(reservations) == 1 and settlements == []
        with session_factory() as session:
            row = session.get(ModelUsage, reservations[0])
            assert row.cost_usd > 0
            estimates.append((row.input_tokens, row.output_tokens))
        if outcome in {"timeout", "transport"}:
            error = httpx.ReadTimeout if outcome == "timeout" else httpx.ConnectError
            raise error("offline", request=req)
        data = response_data()
        if outcome == "schema":
            data["choices"][0]["message"]["content"] = "not JSON"
        if outcome == "missing_usage":
            del data["usage"]
        if outcome == "length":
            data["choices"][0]["finish_reason"] = "length"
        if outcome == "http":
            return httpx.Response(500, text="not JSON")
        return httpx.Response(500 if outcome == "http_usage" else 200, json=data)

    client = LiveLLMClient(settings, book, transport=httpx.MockTransport(handle))
    if outcome == "success":
        client.structured_call(request())
    else:
        with pytest.raises(AppError):
            client.structured_call(request())
    assert len(reservations) == 1 and settlements == reservations
    with session_factory() as session:
        rows = session.scalars(select(ModelUsage)).all()
        assert len(rows) == 1 and rows[0].id == reservations[0]
        expected = (
            estimates[0]
            if outcome in {"timeout", "transport", "missing_usage"}
            else (0, 0)
            if outcome == "http"
            else (1000, 200)
        )
        assert (rows[0].input_tokens, rows[0].output_tokens) == expected


@pytest.mark.parametrize(
    "overrides", [{"max_output_tokens": 1001}, {"max_output_tokens": -1}, {"input_tokens": -1}]
)
def test_compat_token_limits_refuse_before_reservation_or_http(session_factory, overrides):
    settings = configured()
    book = ledger(session_factory, settings)
    with pytest.raises(AppError) as caught:
        LiveLLMClient(
            settings,
            book,
            transport=httpx.MockTransport(lambda req: pytest.fail("Token limit reached HTTP")),
        ).structured_call(request(**overrides))
    assert caught.value.code == "TOKEN_LIMIT"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ModelUsage)) == 0


@pytest.mark.parametrize(
    "schema,payload,valid",
    [
        (
            {
                "type": "object",
                "properties": {
                    "value": {"anyOf": [{"type": "null"}, {"type": "string", "const": "ok"}]}
                },
                "required": ["value"],
                "additionalProperties": False,
            },
            {"value": None},
            True,
        ),
        (
            {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            {"value": True},
            False,
        ),
        (
            {"type": "object", "properties": {"value": {"$ref": "https://schema.example/schema"}}},
            {"value": "ok"},
            False,
        ),
        (
            {"type": "object", "properties": {"value": {"type": "integer", "minimum": 10}}},
            {"value": 1},
            False,
        ),
    ],
    ids=["custom_nullable_const", "strict_bool", "remote_ref_refused", "unsupported_constraint"],
)
def test_compat_validates_the_requested_schema_locally(session_factory, schema, payload, valid):
    settings = configured()
    book = ledger(session_factory, settings)
    data = response_data()
    data["choices"][0]["message"]["content"] = json.dumps(payload)
    client = LiveLLMClient(
        settings, book, transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data))
    )
    if valid:
        assert client.structured_call(request(response_schema=schema)).payload == payload
    else:
        with pytest.raises(SchemaInvalid):
            client.structured_call(request(response_schema=schema))
    assert book.spent() == Decimal("0.000200")
