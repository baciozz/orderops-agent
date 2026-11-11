"""eval-report-check validates committed summaries without live calls."""

import json
from pathlib import Path

import pytest
from scripts.eval_report_check import check_summary, main

LIVE_SUMMARY = (
    Path(__file__).resolve().parents[1] / "evals" / "live_v1" / "results" / "summary.json"
)


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "summary.json"
    target.write_text(LIVE_SUMMARY.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def test_committed_live_v1_summary_passes():
    assert check_summary(LIVE_SUMMARY) == []
    assert main([]) == 0


def test_missing_required_path_fails(tmp_path):
    missing = tmp_path / "missing.json"
    assert main([str(missing)]) == 1


def _break_numerator(payload):
    payload["accuracy"]["numerator"] = payload["accuracy"]["denominator"] + 1


def _break_attempts(payload):
    payload["attempted"] = 1


def _break_sha(payload):
    payload["code_sha"] = "not-a-sha"


def _break_notice(payload):
    payload["accuracy"]["notice"] = "placeholder without the required wording"
    payload["kind"] = "live report"
    payload.pop("human_correction_notice", None)
    payload["formal_qualified"] = False


def _break_formal(payload):
    payload["formal_qualified"] = True


@pytest.mark.parametrize(
    "mutate",
    [_break_numerator, _break_attempts, _break_sha, _break_notice, _break_formal],
    ids=["numerator", "attempts", "sha", "notice", "formal"],
)
def test_deliberately_broken_report_fails(tmp_path, mutate):
    path = _copy(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    errors = check_summary(path)
    assert errors
    assert main([str(path)]) == 1
