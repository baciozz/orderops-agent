"""FIXTURE pipeline/kernel evaluation. No model understanding is measured here."""

import argparse
import ipaddress
import json
import re
import shutil
import socket
import sys
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

# Support the documented invocation without installing the project.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.llm import resolve_mode  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import Base, Plan, Task  # noqa: E402
from app.db.schema import init_db  # noqa: E402
from app.db.session import make_engine, make_session_factory  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.orchestration.dispatch import dispatch_text  # noqa: E402
from app.services.seed import load_seed  # noqa: E402
from scripts.live_smoke import _assert_safe, _commit_golden, _counts  # noqa: E402
from sqlalchemy import select  # noqa: E402

CASE_PATH = Path(__file__).resolve().parents[1] / "evals" / "offline_cases.json"
NOTE = (
    "FIXTURE results measure the pipeline/kernel given explicit structured answers, "
    "not model understanding of input wording. No model or HTTP calls are made."
)
DIFF_KEYS = ("mode", "sku_id", "from_sku", "to_sku", "qty")


class NetworkBlocked(RuntimeError):
    """An offline evaluation attempted external network access."""


def _loopback(host):
    if host in ("localhost", b"localhost"):
        return True
    try:
        value = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return ipaddress.ip_address(value).is_loopback
    except (ValueError, UnicodeError):
        return False


@contextmanager
def network_guard():
    """Block DNS and TCP/UDP egress before any socket can contact a remote host.

    Numeric loopback and localhost are permitted for local tooling, but the eval
    itself needs no sockets. Restore all patches for in-process callers.
    """
    original_dns = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    attempts = []

    def check(host):
        if not _loopback(host):
            attempts.append("blocked")
            raise NetworkBlocked("External network is forbidden in FIXTURE evaluation")

    def dns(host, *args, **kwargs):
        check(host)
        return original_dns(host, *args, **kwargs)

    def connect(sock, address):
        check(address[0] if isinstance(address, tuple) else None)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address[0] if isinstance(address, tuple) else None)
        return original_connect_ex(sock, address)

    def sendto(sock, data, *args):
        address = args[-1]
        check(address[0] if isinstance(address, tuple) else None)
        return original_sendto(sock, data, *args)

    with (
        patch.object(socket, "getaddrinfo", dns),
        patch.object(socket.socket, "connect", connect),
        patch.object(socket.socket, "connect_ex", connect_ex),
        patch.object(socket.socket, "sendto", sendto),
    ):
        yield attempts


def offline_settings():
    configured = Settings(
        _env_file=None,
        live_enabled=False,
        llm_api_key=None,
        llm_provider=None,
        llm_model=None,
        price_in_per_mtok=None,
        price_out_per_mtok=None,
        database_url="sqlite:///:memory:",
    )
    if resolve_mode(configured).mode != "FIXTURE":
        raise RuntimeError("Offline evaluation refuses LIVE mode")
    return configured


def load_cases():
    cases = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    ids = set()
    for case in cases:
        for key in ("id", "category", "input_text", "expected", "fixture_payload"):
            if not case.get(key):
                raise ValueError(f"Case missing {key}")
        if case["id"] in ids:
            raise ValueError("Duplicate case id")
        ids.add(case["id"])
        expected = case["expected"]
        if expected["outcome"] in {"AWAITING_APPROVAL", "COMMITTED"}:
            if not expected.get("diff_summary"):
                raise ValueError("Approvable case needs an explicit diff_summary")
    return cases


def _diff(changes):
    return [{key: row.get(key) for key in DIFF_KEYS} for row in changes]


def _assert_no_stored_urls(session):
    # dispatch_text does not persist the raw input receipt. Check every stored
    # value, including plans/audit/events, in addition to live_smoke's checks.
    for table in Base.metadata.sorted_tables:
        for row in session.execute(select(table)):
            for value in row:
                if isinstance(value, (str, dict, list)):
                    assert not re.search(r"https?://", json.dumps(value, ensure_ascii=False)), (
                        "External URL in stored data"
                    )


def _business_snapshot(session):
    """Intent revisions may advance at precheck; business writes must await commit."""
    names = (
        "order_line",
        "stock_balance",
        "reservation",
        "production_order",
        "purchase_order",
    )
    return {
        name: sorted(
            (tuple(row) for row in session.execute(select(Base.metadata.tables[name]))),
            key=repr,
        )
        for name in names
    }


def _run_case(factory, configured, case):
    expected = case["expected"]
    failures = []
    actual_diff = []
    outcome = None
    error_code = None
    with factory() as session:
        before = _counts(session)
        business_before = _business_snapshot(session)
    try:
        # FIXTURE uses this exact input string as the key in FixtureLLMClient.
        # client_factory is a LIVE-only hook, so use the existing fixtures hook.
        outcome = dispatch_text(
            factory,
            text=case["input_text"],
            client_event_key=f"offline-{case['id']}",
            fixtures={case["input_text"]: case["fixture_payload"]},
            settings=configured,
        )
        error_code = outcome.error_code
        # As worker.run_once does, expose the persisted task status rather than
        # the kernel's plan status (VALIDATED means AWAITING_APPROVAL here).
        with factory() as session:
            outcome.status = session.get(Task, outcome.task_id).status
        if outcome.plan_id:
            with factory() as session:
                plan = session.get(Plan, outcome.plan_id)
                actual_diff = _diff(plan.normalized_changes.get("changes", []))
        if expected["outcome"] == "COMMITTED":
            _commit_golden(factory, outcome)
    except AppError as exc:
        error_code = exc.code
    except AssertionError as exc:
        failures.append(str(exc))
    # Unexpected exceptions deliberately propagate: harness errors are not cases.
    with factory() as session:
        task = session.scalar(select(Task))
        status = task.status if task else None
        error_code = error_code or (task.error_code if task else None)
        after = _counts(session)
        if expected["outcome"] == "SAFE":
            try:
                _assert_safe(session, before)
                _assert_no_stored_urls(session)
            except AssertionError as exc:
                failures.append(str(exc))
        elif expected["outcome"] == "COMMITTED":
            if after[0] - before[0] != 1 or after[1] - before[1] != 1:
                failures.append("Golden must create exactly one approval and operation")
        if expected["outcome"] != "COMMITTED":
            if after != before:
                failures.append("Dispatch created approval/operation/outbox before human approval")
            if _business_snapshot(session) != business_before:
                failures.append("Dispatch mutated business data before commit")
    wanted = expected["outcome"]
    if wanted == "COMMITTED":
        wanted = "SUCCEEDED"
    if wanted == "SAFE":
        if status not in {"NEEDS_CLARIFICATION", "AWAITING_APPROVAL", "FAILED"}:
            failures.append(f"Unexpected injection status: {status}")
    elif status != wanted:
        failures.append(f"Expected {wanted}, got {status}")
    if "error_code" in expected and error_code != expected["error_code"]:
        failures.append(f"Expected error {expected['error_code']}, got {error_code}")
    if expected.get("diff_summary") is not None:
        if actual_diff != _diff(expected["diff_summary"]):
            failures.append("Plan diff does not match expected diff_summary")
    if case.get("known_issue"):
        # Never turn a documented unsafe result into a passing fixture score.
        failures.append("Known issue: " + case["known_issue"])
    return {
        "id": case["id"],
        "category": case["category"],
        "expected": expected,
        "actual_task_status": status,
        "error_code": error_code,
        "actual_diff_summary": actual_diff,
        "row_count_delta": dict(
            zip(
                ("approval", "operation", "outbox"),
                (end - start for start, end in zip(before, after, strict=True)),
                strict=True,
            )
        ),
        "pass": not failures,
        "failures": failures,
        "known_issue": case.get("known_issue"),
    }


def evaluate(cases=None):
    with network_guard() as blocked_attempts:
        configured = offline_settings()
        cases = load_cases() if cases is None else cases
        records = []
        with TemporaryDirectory(prefix="oos-offline-") as directory:
            root = Path(directory)
            # Migrate and seed once, then copy the closed/checkpointed database.
            # Every case gets its own pristine file; no case can affect another.
            template = root / "seed.db"
            url = f"sqlite:///{template}"
            engine = make_engine(url)
            try:
                init_db(engine)
                load_seed(make_session_factory(engine), database_url=url)
            finally:
                engine.dispose()
            for index, case in enumerate(cases):
                path = root / f"case-{index}.db"
                shutil.copyfile(template, path)
                engine = make_engine(f"sqlite:///{path}")
                try:
                    records.append(_run_case(make_session_factory(engine), configured, case))
                finally:
                    engine.dispose()
        categories = defaultdict(lambda: {"pass": 0, "fail": 0, "known_issue": 0})
        for record in records:
            totals = categories[record["category"]]
            totals["pass" if record["pass"] else "fail"] += 1
            totals["known_issue"] += bool(record["known_issue"])
        for totals in categories.values():
            totals["total"] = totals["pass"] + totals["fail"]
            totals["pass_rate"] = round(100 * totals["pass"] / totals["total"], 1)
        return {
            "mode": "FIXTURE",
            "note": NOTE,
            "case_count": len(records),
            "categories": dict(sorted(categories.items())),
            "samples": records,
            "failures": sum(not record["pass"] for record in records),
            "unexpected_failures": sum(
                not record["pass"] and not record["known_issue"] for record in records
            ),
            "blocked_network_attempts": len(blocked_attempts),
        }


def format_table(report):
    lines = [
        "| Category | Pass | Fail | Known issues | Pass rate |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, totals in report["categories"].items():
        lines.append(
            f"| {name} | {totals['pass']} | {totals['fail']} | "
            f"{totals['known_issue']} | {totals['pass_rate']:.1f}% |"
        )
    return "\n".join(lines)


def main(argv=None):
    with network_guard():
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--out", type=Path, help="Write the JSON report to this file")
        args = parser.parse_args(argv)
        report = evaluate()
        print("FIXTURE — " + NOTE)
        print(format_table(report))
        if args.out:
            args.out.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        for record in report["samples"]:
            if not record["pass"]:
                print(f"FAIL {record['id']}: {'; '.join(record['failures'])}")
        return int(bool(report["unexpected_failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
