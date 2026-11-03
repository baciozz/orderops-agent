"""Explicit fixture answers exercise the real dispatch/kernel without sockets."""

import copy
import json
import socket
import time

import pytest
from app.adapters.fixture_model import FixtureLLMClient
from app.adapters.llm import ModeResolution
from schemas.enums import RuntimeMode
from scripts import eval_offline
from scripts.live_smoke import SAMPLES

CATEGORIES = {
    "multi_line_change",
    "ambiguous_wording",
    "alias",
    "invalid_qty",
    "unknown_order",
    "injection",
    "golden",
}


def test_case_contract_and_all_live_inputs():
    cases = eval_offline.load_cases()
    assert len(cases) >= 45
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["category"] for case in cases} == CATEGORIES
    assert {text for _, _, text in SAMPLES} <= {case["input_text"] for case in cases}
    for case in cases:
        assert isinstance(case["fixture_payload"], dict)
        assert case["expected"]["outcome"]
        if case["category"] == "injection":
            assert case["expected"]["outcome"] == "SAFE"
    # These literal hostile answers must reach validation, not be repaired by
    # a keyword-based fake parser or replaced with a canned refusal.
    by_id = {case["id"]: case for case in cases}
    for name, qty in (("qty-replace-zero", 0), ("qty-replace-negative", -5)):
        assert by_id[name]["fixture_payload"]["business_event"]["line_changes"][0]["qty"] == qty
    assert (
        by_id["order-missing-replace"]["fixture_payload"]["business_event"]["order_id"] == "SO-9999"
    )
    assert by_id["injection-approved"]["fixture_payload"]["approved"] is True


def test_script_end_to_end_without_opening_any_socket(tmp_path, monkeypatch, capsys):
    class NoSockets(socket.socket):
        def __new__(cls, *args, **kwargs):
            pytest.fail("Offline evaluation opened a socket")

    monkeypatch.setattr(socket, "socket", NoSockets)
    import app.orchestration.dispatch as dispatch

    monkeypatch.setattr(
        dispatch, "LiveLLMClient", lambda *a, **k: pytest.fail("Constructed LIVE client")
    )
    seen = []
    original = FixtureLLMClient.structured_call
    cases = eval_offline.load_cases()
    answers = {case["input_text"]: case["fixture_payload"] for case in cases}

    def recorded(self, request):
        result = original(self, request)
        assert self.mode == "FIXTURE"
        assert result.payload == answers[request.fixture_key]
        seen.append(request.fixture_key)
        return result

    monkeypatch.setattr(FixtureLLMClient, "structured_call", recorded)
    # A live flag in process environment must not opt this script into LIVE.
    monkeypatch.setenv("OOS_LIVE_ENABLED", "true")
    path = tmp_path / "report.json"
    started = time.monotonic()
    assert eval_offline.main(["--out", str(path)]) == 0
    assert time.monotonic() - started < 30
    report = json.loads(path.read_text())
    assert report["mode"] == "FIXTURE"
    assert "not model understanding" in report["note"]
    assert report["case_count"] == len(cases)
    assert set(report["categories"]) == CATEGORIES
    assert len(report["groups"]) == 2
    for group in report["groups"]:
        categories = [report["categories"][name] for name in group["categories"]]
        assert group["label"]
        for key, source in (
            ("pass", "pass"),
            ("fail", "fail"),
            ("known_issues", "known_issue"),
            ("total", "total"),
        ):
            assert group[key] == sum(category[source] for category in categories)
        assert group["pass_rate"] == round(100 * group["pass"] / group["total"], 1)
    assert "不代表模型理解准确率" in report["groups"][1]["disclaimer"]
    assert report["blocked_network_attempts"] == 0
    assert report["unexpected_failures"] == 0
    assert set(seen) == set(answers)
    assert all(row["pass"] or row["known_issue"] for row in report["samples"])
    for row in report["samples"]:
        if row["category"] == "injection":
            # PASS includes live_smoke._assert_safe plus an all-table URL scan.
            assert row["pass"], row
            assert row["row_count_delta"] == {"approval": 0, "operation": 0, "outbox": 0}
        if row["category"] == "golden":
            assert row["actual_task_status"] == "SUCCEEDED"
            assert row["row_count_delta"]["approval"] == 1
            assert row["row_count_delta"]["operation"] == 1
    table = eval_offline.format_table(report)
    assert "不代表模型理解准确率" in table
    assert table in capsys.readouterr().out
    docs = eval_offline.CASE_PATH.parents[1] / "docs" / "OFFLINE_EVAL.md"
    assert table in docs.read_text()


def test_network_guard_blocks_dns_tcp_and_udp_before_system_calls(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        pytest.fail("External call reached the system")

    # Construct no socket at all; patched methods can be called with a sentinel.
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    with eval_offline.network_guard() as attempts:
        for host in ("example.invalid", "8.8.8.8", "0.0.0.0", "::", "2001:db8::1"):
            with pytest.raises(eval_offline.NetworkBlocked):
                socket.getaddrinfo(host, 443)
            for method in (socket.socket.connect, socket.socket.connect_ex):
                with pytest.raises(eval_offline.NetworkBlocked):
                    method(None, (host, 443))
            with pytest.raises(eval_offline.NetworkBlocked):
                socket.socket.sendto(None, b"x", (host, 53))
        assert len(attempts) == 20
    assert socket.getaddrinfo is forbidden  # Patches restored for in-process use.
    assert calls == []


@pytest.mark.parametrize("host", ["localhost", b"localhost", "127.0.0.1", "::1"])
def test_network_guard_permits_loopback_through_original_methods(host, monkeypatch):
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: calls.append("dns"))
    monkeypatch.setattr(socket.socket, "connect", lambda *a: calls.append("connect"))
    with eval_offline.network_guard() as attempts:
        socket.getaddrinfo(host, 80)
        socket.socket.connect(None, (host, 80))
        assert attempts == []
    assert calls == ["dns", "connect"]


def test_safe_outcome_fails_when_parser_answer_persists_external_url():
    case = copy.deepcopy(next(c for c in eval_offline.load_cases() if c["id"] == "injection-only"))
    # This answer passes the extraction schema but puts a URL in Task.result_ref.
    # A refusal status alone must never count as evidence of no exfiltration.
    case["fixture_payload"]["questions"] = ["请访问 http://evil.example 确认订单"]
    report = eval_offline.evaluate([case])
    row = report["samples"][0]
    assert row["actual_task_status"] == "NEEDS_CLARIFICATION"
    assert not row["pass"]
    assert "External URL in stored data" in row["failures"]
    assert report["unexpected_failures"] == 1


def test_refuse_live_resolution(monkeypatch):
    monkeypatch.setattr(
        eval_offline,
        "resolve_mode",
        lambda _: ModeResolution(RuntimeMode.LIVE, True, None),
    )
    with pytest.raises(RuntimeError, match="refuses LIVE"):
        eval_offline.evaluate([])


def test_failures_and_known_issues_are_reported_without_masking_scores(monkeypatch, capsys):
    original = eval_offline.load_cases()[0]
    wrong_diff = copy.deepcopy(original)
    wrong_diff["expected"]["diff_summary"][0]["qty"] = 999
    monkeypatch.setattr(eval_offline, "load_cases", lambda: [wrong_diff])
    assert eval_offline.main([]) == 1
    assert "Plan diff does not match" in capsys.readouterr().out
    known = copy.deepcopy(wrong_diff)
    known["known_issue"] = "Synthetic harness test: documented failure remains a failure."
    report = eval_offline.evaluate([known])
    assert report["failures"] == 1 and report["unexpected_failures"] == 0
    assert report["categories"]["golden"]["pass_rate"] == 0
    assert report["samples"][0]["known_issue"]
    group = report["groups"][1]
    assert (group["pass"], group["fail"], group["known_issues"], group["pass_rate"]) == (
        0,
        1,
        1,
        0.0,
    )
    monkeypatch.setattr(eval_offline, "load_cases", lambda: [known])
    assert eval_offline.main([]) == 0


def test_group_coverage_and_zero_quantity_annotations():
    grouped = [name for group in eval_offline.GROUPS for name in group["categories"]]
    assert set(grouped) == CATEGORIES
    assert len(grouped) == len(set(grouped))
    zero_cases = []
    for case in eval_offline.load_cases():
        event = case["fixture_payload"]["business_event"]
        if event and any(
            row.get("qty") == 0 for key in ("line_changes", "new_lines") for row in event[key]
        ):
            zero_cases.append(case)
            assert "待 Qi 确认" in case["product_question"]
            assert case["expected"]["error_code"] == "BUSINESS_CONFLICT"
    assert len(zero_cases) == 3


def test_unknown_category_is_rejected(tmp_path, monkeypatch):
    cases = eval_offline.load_cases()
    cases[0]["category"] = "unmapped"
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases))
    monkeypatch.setattr(eval_offline, "CASE_PATH", path)
    with pytest.raises(ValueError, match="Unknown category"):
        eval_offline.load_cases()
