"""Registry startup failures and explicit CAPABILITY_MISSING routing."""

import pytest
from app.config import Settings
from app.errors import RegistryError, ToolNotAllowed
from app.main import create_app
from app.orchestration.defaults import build_default_registry, parser_spec
from app.orchestration.registry import Registry, check_tool_whitelist, load_registry
from app.orchestration.router import Router
from schemas.subagent import SchemaRef


def test_R17_duplicate_schema_and_missing_capability(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    registry = Registry()
    registry.register(parser_spec())
    with pytest.raises(RegistryError) as duplicate:
        registry.register(parser_spec())
    assert duplicate.value.code == "REGISTRY_DUPLICATE"

    broken = parser_spec().model_copy(
        update={
            "version": "1.0.0",
            "input_schema": SchemaRef(model_id="missing.schema", model_version="1.0"),
        }
    )
    with pytest.raises(RegistryError) as missing:
        Registry().register(broken)
    assert missing.value.code == "SCHEMA_MISSING"

    def fail_startup() -> Registry:
        raise RegistryError("PRIVATE_REGISTRY_DETAIL", code="REGISTRY_DUPLICATE")

    monkeypatch.setattr("app.main.build_default_registry", fail_startup)
    with pytest.raises(RuntimeError) as startup:
        create_app(Settings(_env_file=None))
    assert "PRIVATE_REGISTRY_DETAIL" not in str(startup.value) + caplog.text
    assert any(
        getattr(record, "event", None) == "startup.failed"
        and getattr(record, "error_code", None) == "REGISTRY_DUPLICATE"
        for record in caplog.records
    )

    failure = Router(Registry()).plan("new_order")
    assert failure.ok is False
    assert failure.code == "CAPABILITY_MISSING"
    assert failure.capability == "check.inventory"
    assert "CAPABILITY_MISSING" in failure.message
    assert "不能静默跳过" in failure.message


def test_default_registry_routes_by_capability() -> None:
    router = Router(build_default_registry())
    change = router.plan("change_order", has_downstream=True)
    assert change.ok is True
    assert [step.capability for step in change.steps] == ["check.inventory", "check.downstream"]
    assert [step.agent_id for step in change.steps] == ["inventory-check", "doc-check"]
    assert all(not step.skipped for step in change.steps)

    skipped = router.plan("cancel_remaining", has_downstream=False)
    assert skipped.ok is True
    assert skipped.steps[0].agent_id == "inventory-check"
    assert skipped.steps[1].skipped is True
    assert skipped.steps[1].skip_reason == "无下游单据"
    assert skipped.steps[1].agent_id == "doc-check"

    created = router.plan("new_order")
    assert [step.capability for step in created.steps] == ["check.inventory"]
    parsed = router.plan("unstructured", input_mode="xlsx")
    assert parsed.steps[0].capability == "parse.xlsx"
    assert parsed.steps[0].agent_id == "parser"
    query = router.plan("query")
    assert query.disposition == "read_service"
    assert query.steps == []


def test_ambiguous_capability_is_explicit() -> None:
    first = parser_spec()
    second = parser_spec().model_copy(update={"agent_id": "parser-b", "version": "0.1.0"})
    registry = load_registry([first, second])
    with pytest.raises(RegistryError) as exc:
        registry.find_by_capability("parse.text")
    assert exc.value.code == "AMBIGUOUS_CAPABILITY"


def test_external_agent_is_refused() -> None:
    external = parser_spec().model_copy(update={"kind": "external", "agent_id": "erp-sim"})
    with pytest.raises(RegistryError) as exc:
        Registry().register(external)
    assert exc.value.code == "EXTERNAL_NOT_ENABLED"


def test_tool_whitelist_rejects_commit() -> None:
    spec = parser_spec()
    with pytest.raises(ToolNotAllowed):
        check_tool_whitelist(spec, "commit")
    with pytest.raises(ToolNotAllowed):
        check_tool_whitelist(spec, "approve_plan")
    check_tool_whitelist(spec, "source.read")
