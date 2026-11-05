"""Two synthetic FIXTURE journeys; no server, live configuration, or existing database."""

import argparse
import csv
import hashlib
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
STOCK_TEXT = "SO-1001 把未发的 40 件黑色 M 改成白色 M"
NAVY_TEXT = "C 款黑色 L 全部改藏青"
ANSWER_TEXT = "只改未开裁"


class AcceptanceError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise AcceptanceError(message)


def clean_env(home):
    # No inherited OOS, credentials, proxies, PYTHONPATH, or user HOME.
    return {"HOME": str(home), "TMPDIR": str(home), "PATH": os.defpath}


def configure(database):
    """Run before importing app.main or app.cli, including in every worker child."""
    os.environ.clear()
    os.environ.update(clean_env(database.parent))
    tempfile.tempdir = str(database.parent)
    os.environ["OOS_DATABASE_URL"] = "sqlite:///" + str(database)
    os.environ["OOS_LIVE_ENABLED"] = "false"
    sys.path.insert(0, str(ROOT))

    def deny_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.bind"}:
            raise AcceptanceError("Network/server operation forbidden in fixture acceptance")

    sys.addaudithook(deny_network)
    from app.config import Settings, get_settings

    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    settings = get_settings()
    require(not settings.live_enabled, "FIXTURE required")
    require(settings.llm_api_key is None, "Credentials must not be loaded")
    require(settings.database_url == "sqlite:///" + str(database), "Database isolation failed")
    return settings


def child_code(expression):
    return (
        "import runpy,sys; "
        f"m=runpy.run_path({str(SCRIPT)!r},run_name='fixture_support'); " + expression
    )


def cli(database, command, log):
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            child_code(
                "m['configure'](m['Path'](sys.argv[1])); "
                "from app.cli import main; raise SystemExit(main([sys.argv[2]]))"
            ),
            str(database),
            command,
        ],
        env=clean_env(database.parent),
        cwd=database.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    log.append({"cli": command, "exit_code": completed.returncode})
    require(completed.returncode == 0, f"Isolated CLI {command} failed")


def write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def order_business(order):
    return {key: order[key] for key in ("current_revision", "status", "lines")}


def business_rows(database):
    """Read-only evidence for stored quantities/docs not all exposed by public APIs."""
    tables = (
        "order_revision",
        "order_line",
        "shipment_record",
        "stock_balance",
        "reservation",
        "stock_movement",
        "production_order",
        "purchase_order",
        "doc_revision",
        "downstream_link",
    )
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return {
            table: [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
            for table in tables
        }


def only_plan(state, excluded=()):
    plans = [p for p in state["plans"] if p["plan_id"] not in excluded]
    require(len(plans) == 1, "Expected exactly one new plan; refusing to guess")
    return plans[0]["plan_id"]


def journey(output, name, database):
    settings = configure(database)
    from app.main import create_app
    from fastapi.testclient import TestClient
    from openpyxl import load_workbook

    log = []
    cli(database, "init-db", log)
    cli(database, "seed", log)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:

        def request(method, path, body=None, expected=200):
            response = client.request(method, path, json=body)
            log.append({"method": method, "path": path, "status": response.status_code})
            require(response.status_code == expected, f"{method} {path}: unexpected status")
            return response

        def get(path):
            return request("GET", path).json()

        before = get("/orders/SO-1001")
        stored_before = business_rows(database)
        receipt = request(
            "POST",
            "/inputs",
            {
                "thread_id": name,
                "client_event_key": name + "-input",
                "selected_order_id": "SO-1001",
                "text": STOCK_TEXT if name == "stock" else NAVY_TEXT,
            },
            202,
        ).json()
        task = receipt["task_id"]
        cli(database, "worker-once", log)
        state = get(f"/tasks/{task}")
        clarification = None
        answer = None
        excluded = []
        if name == "uncut":
            require(state["status"] == "NEEDS_CLARIFICATION", "Unsafe request must clarify")
            clarification = get(f"/tasks/{task}/view")
            require(bool(state["questions"]), "Clarification question missing")
            clarification["task_questions"] = state["questions"]
            excluded = [only_plan(state)]
            require(
                order_business(get("/orders/SO-1001")) == order_business(before),
                "Business changed during clarification",
            )
            answer = request(
                "POST",
                f"/tasks/{task}/answers",
                {
                    "thread_id": name,
                    "client_event_key": name + "-answer",
                    "text": ANSWER_TEXT,
                    "source_ids": [],
                    "expected_intent_revision": clarification["card"]["intent_revision"],
                    "expected_input_event_id": receipt["event_id"],
                },
                202,
            ).json()
            cli(database, "worker-once", log)
            state = get(f"/tasks/{task}")
        require(state["status"] == "AWAITING_APPROVAL", "Plan must await approval")
        plan = only_plan(state, excluded)
        view = get(f"/tasks/{task}/view")
        preapproval = get("/orders/SO-1001")
        require(view["card"]["mode"] == "FIXTURE", "Unexpected runtime mode")
        require(
            order_business(preapproval) == order_business(before),
            "Business changed before simulated approval",
        )
        audit_before = get(f"/tasks/{task}/audit")
        require(not audit_before["committed_operations"], "Operation exists before approval")
        require(business_rows(database) == stored_before, "Stored business changed before approval")
        print(f"{name}: no business change before simulated demo approval", flush=True)
        # Deliberately separate demo approval and commit. This is not a human signature.
        approval_body = {
            "plan_hash": view["approval"]["plan_hash"],
            "expected_intent_revision": state["intent_revision"],
        }
        approval = request("POST", f"/plans/{plan}/approve", approval_body).json()
        require(
            order_business(get("/orders/SO-1001")) == order_business(before),
            "Approval alone changed business",
        )
        require(business_rows(database) == stored_before, "Approval changed stored business")
        commit_body = {
            "approval_id": approval["approval_id"],
            "plan_hash": view["approval"]["plan_hash"],
            "idempotency_key": name + "-synthetic-commit",
        }
        commit = request("POST", f"/plans/{plan}/commit", commit_body).json()
        replay = request("POST", f"/plans/{plan}/commit", commit_body).json()
        require(commit["operation_id"] == replay["operation_id"], "Replay created new operation")
        final = get(f"/tasks/{task}/view")
        after = get("/orders/SO-1001")
        audit = get(f"/tasks/{task}/audit")
        require(final["card"]["status"] == "SUCCEEDED", "Task did not succeed")
        require(final["result"]["verification"] == "passed", "Verification did not pass")
        require(after["current_revision"] == replay["revision"] == 2, "Wrong final revision")
        require(len(audit["committed_operations"]) == 1, "Expected exactly one operation")
        lines = {
            r["sku_id"]: (r["ordered_qty"], r["shipped_qty"], r["cut_qty"]) for r in after["lines"]
        }
        expected = (
            {"A-BLACK-M": (60, 60, 0), "A-WHITE-M": (40, 0, 0), "C-BLACK-L": (300, 0, 120)}
            if name == "stock"
            else {"A-BLACK-M": (100, 60, 0), "C-BLACK-L": (120, 0, 120), "C-NAVY-L": (180, 0, 0)}
        )
        require(lines == expected, "Unexpected business quantities or protected work changed")
        downstream = {r["doc_id"]: r for r in final["downstream"]}
        require(len(downstream) == 4, "Expected four downstream records")
        unchanged = (
            downstream.values()
            if name == "stock"
            else (downstream["MO-2001"], downstream["PUR-3001"])
        )
        require(all(r["before"] == r["after"] for r in unchanged), "Protected downstream changed")
        if name == "uncut":
            require(
                downstream["MO-2002"]["after"]
                == {
                    "sku_id": "C-NAVY-L",
                    "planned_qty": 180,
                    "cut_qty": 0,
                    "status": "NOT_STARTED",
                },
                "Wrong uncut production draft",
            )
            require(
                downstream["PUR-3002"]["after"]
                == {
                    "material_id": "FAB-NAVY",
                    "ordered_qty": 360,
                    "received_qty": 0,
                    "status": "PLACED",
                    "change_state": "PENDING_SUPPLIER",
                },
                "Supplier draft must remain pending",
            )
        stored_after = business_rows(database)
        for table in ("production_order", "purchase_order"):
            for row in stored_after[table]:
                expected_doc = downstream[row["id"]]["after"]
                require(
                    all(row[k] == v for k, v in expected_doc.items()),
                    f"Stored {table} differs from planned consequence",
                )
        csv_bytes = request("GET", f"/tasks/{task}/download?fmt=csv").content
        rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
        require(len(rows) == 3 and all(r["mode"] == "FIXTURE" for r in rows), "Bad CSV")
        expected_rows = [{"mode": "FIXTURE", **row} for row in final["line_diffs"]]
        require(
            rows
            == [
                {key: "" if value is None else str(value) for key, value in row.items()}
                for row in expected_rows
            ],
            "CSV differs from final business view",
        )
        xlsx = request("GET", f"/tasks/{task}/download?fmt=xlsx").content
        workbook = load_workbook(io.BytesIO(xlsx), read_only=True, data_only=True)
        try:
            sheet_rows = list(workbook.active.values)
            require(len(sheet_rows) == 4 and sheet_rows[0][0] == "mode", "Bad XLSX")
            require(
                [dict(zip(sheet_rows[0], row, strict=True)) for row in sheet_rows[1:]]
                == expected_rows,
                "XLSX differs from final business view",
            )
        finally:
            workbook.close()
        for suffix, content in (("csv", csv_bytes), ("xlsx", xlsx)):
            with (output / f"{name}-changes.{suffix}").open("xb") as stream:
                stream.write(content)
        write_json(output / f"{name}-audit.json", audit)
        result = {
            "scenario": name,
            "evidence": "SYNTHETIC FIXTURE; simulated demo approval",
            "input_text": STOCK_TEXT if name == "stock" else NAVY_TEXT,
            "answer_text": ANSWER_TEXT if name == "uncut" else None,
            "input": receipt,
            "stored_before": stored_before,
            "stored_after": stored_after,
            "clarification": clarification,
            "answer": answer,
            "plan_id": plan,
            "before": before,
            "preapproval": preapproval,
            "no_business_change_before_approval": True,
            "preapproval_view": view,
            "simulated_approval_request": approval_body,
            "simulated_approval": approval,
            "simulated_commit_request": commit_body,
            "commit": commit,
            "replay": replay,
            "after": after,
            "final_view": final,
            "steps": log,
            "supplier_contacted": False,
        }
        write_json(output / f"{name}-result.json", result)
        return result


RUNTIME_DIRS = ("app", "schemas", "seeds", "alembic")
SOURCE_FILES = (
    "pyproject.toml",
    "alembic.ini",
    "Makefile",
    "requirements.lock",
    "requirements-dev.lock",
    "scripts/accept_fixture.py",
)


def regular_source(path):
    """Reject symlink leaves and ancestors before reading source content."""
    current = ROOT
    for part in path.relative_to(ROOT).parts:
        current = current / part
        require(not current.is_symlink(), "Symlink in source fingerprint scope")
    require(path.is_file(), "Required source file missing")
    return path


def source_identity():
    """Fingerprint relevant runtime/runner files, not arbitrary repository content."""
    files = [regular_source(ROOT / name) for name in SOURCE_FILES]
    for directory in RUNTIME_DIRS:
        base = ROOT / directory
        require(not base.is_symlink() and base.is_dir(), "Unsafe runtime source directory")
        for current, directories, names in os.walk(base, followlinks=False):
            directories[:] = [name for name in directories if name != "__pycache__"]
            for name in directories:
                require(not (Path(current) / name).is_symlink(), "Symlink in runtime source")
            for name in names:
                path = Path(current) / name
                if path.suffix == ".py" or (
                    directory == "app" and path.suffix in {".html", ".css", ".js"}
                ):
                    files.append(regular_source(path))
    return {
        "evidence": "local runtime/runner inputs only; not whole release or remote identity",
        "limits": "does not hash installed dependencies, docs, tests, evals or user outputs",
        "source_root": str(ROOT),
        "python": sys.version,
        "files": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(files)
        },
    }


def controller(output):
    write_json(output / "SOURCE.json", source_identity())
    for name in ("stock", "uncut"):
        with tempfile.TemporaryDirectory(prefix=f"{name}-ephemeral-", dir=output) as temporary:
            journey(output, name, Path(temporary) / "synthetic.db")


def publish_summary(output):
    with (output / "SUMMARY.md").open("x", encoding="utf-8") as stream:
        stream.write(
            "# 合成 FIXTURE 验收通过\n\n"
            "- 库存改单：黑色 100 → 60，已发 60 保留；白色 0 → 40。\n"
            "- 已裁保护：全改藏青请求先进入澄清；明确回答「只改未开裁」后，"
            "只改未裁 180，黑色已裁 120 保留。\n"
            "- PUR-3002：藏青面料 360 为 PENDING_SUPPLIER 草案，仍需供应商确认。"
            "没有联系供应商，也没有取得对方同意。\n\n"
            "两条旅程均经过持久输入和单独 CLI worker；批准前订单及业务表未变。"
            "脚本模拟 demo-user 批准，再单独提交；最终 revision 2、SUCCEEDED、"
            "verification passed。同 key 重放返回原 Operation ID，审计各只有一次操作。\n\n"
            "这是模拟批准，不是真实人工验收。仅证明 FIXTURE 管道，不证明浏览器、"
            "模型理解、ERP 或部署就绪。CSV/XLSX 是当前视图，不是批准时库存快照；"
            "audit 文件保留自身的历史与来源语义限制。IDs/时间戳每次不同。\n\n"
            "SOURCE.json 记录本地运行相关源码与配置的哈希和 Python 版本，不覆盖全部"
            "仓库或已装依赖，也不证明远端提交身份。临时合成库已清理，结果文件保留。\n"
        )


def run_controller(command, output, log, timeout=180):
    """POSIX process group bounds controller and its CLI descendants together."""

    def interrupted(signum, frame):
        raise InterruptedError("Fixture acceptance interrupted")

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    process = None
    try:
        process = subprocess.Popen(
            command,
            env=clean_env(output),
            cwd=output,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        return process.wait(timeout=timeout)
    finally:
        if process is not None:
            # Kill any remaining descendants too, even if the controller exited first.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="New directory; parent must exist"
    )
    args = parser.parse_args(argv)
    if os.name != "posix":
        parser.error("This acceptance runner requires POSIX process-group cleanup")
    output = args.output.absolute()
    if any(
        output.resolve().is_relative_to((ROOT / directory).resolve()) for directory in RUNTIME_DIRS
    ):
        parser.error("Output must be outside runtime source directories")
    try:
        output.mkdir(mode=0o700)  # Never reuse, overwrite or reset an existing output.
    except OSError:
        parser.error("Output must not exist and its parent must be an existing writable directory")
    try:
        with (output / "run.log").open("x", encoding="utf-8") as log:
            returncode = run_controller(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    child_code("m['controller'](m['Path'](sys.argv[1]))"),
                    str(output),
                ],
                output,
                log,
            )
        require(returncode == 0, "Acceptance failed; inspect run.log")
        publish_summary(output)
    except (OSError, subprocess.TimeoutExpired, AcceptanceError) as exc:
        print(
            f"{type(exc).__name__}: acceptance incomplete; retained output at {output}",
            file=sys.stderr,
        )
        return 1
    print(f"SYNTHETIC FIXTURE passed: {output / 'SUMMARY.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
