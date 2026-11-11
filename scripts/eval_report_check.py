"""Offline consistency checks for committed evaluation summaries.

This command never enables live model calls and does not read credentials.
It validates files that already exist in the repository. The agent-comparison
summary is checked when present and skipped until that result is merged.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (ROOT / "evals" / "live_v1" / "results" / "summary.json",)
OPTIONAL = (ROOT / "reports" / "agent_comparison_v1" / "summary.json",)
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
INFORMAL = re.compile(r"非正式|不是正式|not formal|not official", re.I)


class ReportError(Exception):
    pass


def _walk(value: Any, path: str = "") -> list[tuple[str, Any]]:
    found = [(path, value)]
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_walk(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_walk(item, f"{path}[{index}]"))
    return found


def _check_rates(payload: Any, errors: list[str]) -> None:
    for path, value in _walk(payload):
        if not isinstance(value, dict):
            continue
        if "numerator" not in value or "denominator" not in value:
            continue
        numerator = value["numerator"]
        denominator = value["denominator"]
        if not isinstance(numerator, int) or not isinstance(denominator, int):
            errors.append(f"{path}: numerator/denominator 必须是整数")
            continue
        if numerator < 0 or denominator < 0:
            errors.append(f"{path}: 分子或分母为负")
        if numerator > denominator:
            errors.append(f"{path}: 分子 {numerator} 大于分母 {denominator}")


def _check_sha_fields(payload: Any, errors: list[str]) -> None:
    for path, value in _walk(payload):
        name = path.rsplit(".", 1)[-1]
        if not isinstance(value, str):
            continue
        if name.endswith("_sha256") or name == "sha256":
            if not SHA256.fullmatch(value):
                errors.append(f"{path}: SHA256 格式应为 64 位小写十六进制")
        elif name in {"code_sha", "rescored_with_code_sha"} or name.endswith("_git_sha"):
            if not GIT_SHA.fullmatch(value):
                errors.append(f"{path}: git SHA 格式应为 40 位小写十六进制")


def _check_attempts(payload: dict[str, Any], errors: list[str]) -> None:
    rows = payload.get("rows")
    attempted = payload.get("attempted")
    if isinstance(rows, list) and isinstance(attempted, int) and len(rows) != attempted:
        errors.append(f"attempted={attempted} 与 rows={len(rows)} 不一致")
    first_pass = payload.get("first_pass_attempts")
    succeeded = payload.get("succeeded")
    failed = payload.get("failed")
    if isinstance(first_pass, int) and isinstance(succeeded, int) and isinstance(failed, int):
        if succeeded + failed != first_pass:
            errors.append(
                f"succeeded+failed={succeeded + failed} 与 first_pass_attempts={first_pass} 不一致"
            )


def _check_accuracy_informal(payload: dict[str, Any], errors: list[str]) -> None:
    if payload.get("formal_qualified") is True:
        errors.append("formal_qualified 为 true，但本检查要求准确率标注为非正式")
    accuracy = payload.get("accuracy")
    notices = []
    if isinstance(payload.get("kind"), str):
        notices.append(payload["kind"])
    if isinstance(accuracy, dict) and isinstance(accuracy.get("notice"), str):
        notices.append(accuracy["notice"])
    if isinstance(payload.get("human_correction_notice"), str):
        notices.append(payload["human_correction_notice"])
    if not any(INFORMAL.search(text) for text in notices):
        errors.append("accuracy 未标注为非正式（缺少 notice 或 kind 中的非正式说明）")


def check_summary(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{path}: 无法读取 JSON（{type(exc).__name__}）"]
    if not isinstance(payload, dict):
        return [f"{path}: 根对象必须是 JSON object"]
    errors: list[str] = []
    _check_rates(payload, errors)
    _check_sha_fields(payload, errors)
    _check_attempts(payload, errors)
    _check_accuracy_informal(payload, errors)
    return [f"{path}: {item}" for item in errors]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线检查已提交评测报告的内部自洽性")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="可选；默认检查仓库中已存在的 live_v1 摘要，以及若存在的对照摘要",
    )
    args = parser.parse_args(argv)
    targets = list(args.paths) if args.paths else list(REQUIRED)
    if not args.paths:
        for path in OPTIONAL:
            if path.exists():
                targets.append(path)
            else:
                print(f"跳过尚不存在的报告：{path.relative_to(ROOT)}")
    errors: list[str] = []
    for path in targets:
        resolved = path if path.is_absolute() else ROOT / path
        if not resolved.exists():
            errors.append(f"{resolved}: 文件不存在")
            continue
        errors.extend(check_summary(resolved))
    if errors:
        for item in errors:
            print(item)
        return 1
    print(f"eval-report-check 通过：{len(targets)} 份报告内部自洽")
    return 0


if __name__ == "__main__":
    sys.exit(main())
