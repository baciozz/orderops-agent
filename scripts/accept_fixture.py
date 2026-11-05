"""Three synthetic FIXTURE journeys; no server, live configuration, or existing database."""

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


def new_order_journey(output, database):
    """Explicit synthetic service inputs, not upload/extraction or browser coverage."""
    configure(database)
    from dataclasses import asdict
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.api.orders import get_order
    from app.db.models import Base, Operation, SalesOrder
    from app.db.session import make_engine, make_session_factory
    from app.errors import AppError
    from app.orchestration.recovery import durable_commit
    from app.services.export import export_change_list
    from app.services.kernel import (
        approve_plan,
        consistency_problems,
        downstream_problems,
        plan_change,
        plan_new_order,
    )
    from app.services.task_audit import export_task_audit
    from app.services.task_view import build_task_view
    from openpyxl import load_workbook
    from sqlalchemy import select

    log = []
    cli(database, "init-db", log)
    cli(database, "seed", log)
    engine = make_engine("sqlite:///" + str(database))
    factory = make_session_factory(engine)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_factory=factory)),
        state=SimpleNamespace(
            request_id="synthetic-new-stock", correlation_id="synthetic-new-stock"
        ),
    )

    def snapshot():
        rows = business_rows(database)
        with factory() as session:
            rows["operations"] = [
                dict(row) for row in session.execute(Operation.__table__.select()).mappings()
            ]
            rows["orders"] = [
                {"id": order.id, "revision": order.current_revision, "status": order.status}
                for order in session.scalars(select(SalesOrder).order_by(SalesOrder.id))
            ]
        return rows

    def full_snapshot():
        with engine.connect() as connection:
            return {
                table.name: [tuple(row) for row in connection.execute(table.select())]
                for table in Base.metadata.sorted_tables
            }

    def consistent():
        with factory() as session:
            require(not consistency_problems(session), "New-order inventory inconsistency")
            require(not downstream_problems(session), "New-order downstream inconsistency")

    def view(plan):
        with factory() as session:
            return build_task_view(
                session,
                plan.task_id,
                request_id="synthetic",
                correlation_id="synthetic",
                now=datetime.now(UTC),
            ).model_dump(mode="json")

    def finish(plan, key, before):
        require(plan.status == "VALIDATED", "New-order plan must be validated")
        require(snapshot() == before, "Planning changed stored business")
        preview = view(plan)
        require(preview["card"]["mode"] == "FIXTURE", "New-order mode must be FIXTURE")
        approval_request = {
            "plan_id": plan.plan_id,
            "plan_hash": plan.plan_hash,
            "expected_intent_revision": plan.intent_revision,
        }
        approval = approve_plan(factory, **approval_request)
        require(snapshot() == before, "Simulated approval changed stored business")
        commit_request = {
            "plan_id": plan.plan_id,
            "approval_id": approval.approval_id,
            "plan_hash": plan.plan_hash,
            "idempotency_key": key,
        }
        commit = durable_commit(factory, **commit_request)
        state = full_snapshot()
        replay = durable_commit(factory, **commit_request)
        require(
            replay.replayed and replay.operation_id == commit.operation_id,
            "New operation on replay",
        )
        require(full_snapshot() == state, "Replay changed stored rows")
        consistent()
        final = view(plan)
        require(final["card"]["status"] == "SUCCEEDED", "New-order task did not succeed")
        require(final["result"]["verification"] == "passed", "New-order verification failed")
        return {
            "plan_id": plan.plan_id,
            "task_id": plan.task_id,
            "preapproval_view": preview,
            "no_business_change_before_approval": True,
            "no_business_change_on_approval": True,
            "simulated_approval_request": approval_request,
            "simulated_approval": asdict(approval),
            "simulated_commit_request": commit_request,
            "commit": asdict(commit),
            "replay": asdict(replay),
            "replay_all_tables_unchanged": True,
            "final_view": final,
        }

    def check_order(order_id, quantities, revision):
        order = get_order(order_id, request).model_dump(mode="json")
        require(
            order["current_revision"] == revision and order["status"] == "OPEN",
            "Wrong new-order revision",
        )
        require(
            {row["sku_id"]: row["ordered_qty"] for row in order["lines"]} == quantities,
            "Wrong new-order quantities",
        )
        require(
            all(row["shipped_qty"] == row["cut_qty"] == 0 for row in order["lines"]),
            "Unexpected new-order execution",
        )
        reservations = {
            row["sku_id"]: row["qty"]
            for row in business_rows(database)["reservation"]
            if row["order_id"] == order_id and row["qty"]
        }
        require(reservations == quantities, "New-order reservations differ from demand")
        return {"order": order, "reservations": reservations}

    def export(plan, label, quantities):
        current = view(plan)
        rows = [{"mode": current["card"]["mode"], **row} for row in current["line_diffs"]]
        require(
            {row["sku_id"]: row["new_qty"] for row in rows} == quantities,
            "Export quantities changed",
        )
        available = {
            row["sku_id"]: row["on_hand"] - row["reserved"]
            for row in business_rows(database)["stock_balance"]
        }
        require(
            all(row["available_qty"] == available[row["sku_id"]] for row in rows),
            "Export availability must reflect current stock, not historical inventory",
        )
        for fmt in ("csv", "xlsx"):
            content = export_change_list(rows, fmt=fmt)
            if fmt == "csv":
                actual = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
                expected = [
                    {key: "" if value is None else str(value) for key, value in row.items()}
                    for row in rows
                ]
            else:
                workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
                try:
                    sheet = list(workbook.active.values)
                    actual = [dict(zip(sheet[0], row, strict=True)) for row in sheet[1:]]
                finally:
                    workbook.close()
                expected = rows
            require(actual == expected, f"New-order {fmt} differs from task view")
            with (output / f"{label}-changes.{fmt}").open("xb") as stream:
                stream.write(content)
        audit = json.loads(export_task_audit(engine, plan.task_id))
        require(len(audit["committed_operations"]) == 1, "New-order task must have one operation")
        write_json(output / f"{label}-audit.json", audit)
        return current

    try:
        consistent()
        initial = snapshot()
        mixed = [{"sku_id": "A-WHITE-M", "qty": 8}, {"sku_id": "C-NAVY-L", "qty": 6}]
        try:
            plan_new_order(
                factory, customer_code="HD", lines=mixed, client_event_key="mixed-unsupported"
            )
        except AppError as exc:
            require(exc.code == "MTO_CREATE_UNSUPPORTED", "Unexpected mixed-order error")
            boundary = {
                "input": mixed,
                "error_code": exc.code,
                "business_unchanged": snapshot() == initial,
            }
            require(boundary["business_unchanged"], "Unsupported creation changed business")
        else:
            raise AcceptanceError("Mixed STOCK/MTO creation must remain unsupported")
        ref = {
            "source_id": "synthetic-new-order.csv",
            "sheet": "Orders",
            "cell_range": "B2:C4",
            "text_excerpt": "WHITE 8; BLUE 6; WHITE 2",
        }
        inputs = {
            "customer_code": "HD",
            "lines": [
                {"sku_id": "A-WHITE-M", "qty": 8, "source_ref": ref},
                {"sku_id": "B-BLUE-L", "qty": 6},
                {"sku_id": "A-WHITE-M", "qty": 2},
            ],
            "client_event_key": "new-stock-create",
        }
        creation = plan_new_order(factory, **inputs)
        state = full_snapshot()
        repeated_plan = plan_new_order(factory, **inputs)
        require(
            repeated_plan.plan_id == creation.plan_id and full_snapshot() == state,
            "Plan replay changed rows",
        )
        created = finish(creation, "new-stock-create-commit", initial)
        order_id = created["commit"]["order_id"]
        create_quantities = {"A-WHITE-M": 10, "B-BLUE-L": 6}
        created.update(check_order(order_id, create_quantities, 1))
        original = export(creation, "new-stock-create", create_quantities)
        require(
            any(
                source["source_id"] == ref["source_id"]
                and source["cell_range"] == ref["cell_range"]
                for source in original["sources"]
            ),
            "Synthetic provenance missing",
        )
        before_amend = snapshot()
        amendment_input = {
            "order_id": order_id,
            "base_revision": 1,
            "intent_revision": 1,
            "changes": [
                {
                    "mode": "replace_remaining",
                    "from_sku": "A-WHITE-M",
                    "to_sku": "A-BLACK-M",
                    "qty": 4,
                },
                {"mode": "increment", "sku_id": "B-BLUE-L", "qty": 3},
            ],
            "client_event_key": "new-stock-amend",
            "source_refs": [{"source_id": "synthetic-amend.txt", "line_start": 1, "line_end": 1}],
        }
        amendment = plan_change(factory, **amendment_input)
        amended = finish(amendment, "new-stock-amend-commit", before_amend)
        amend_quantities = {"A-WHITE-M": 6, "A-BLACK-M": 4, "B-BLUE-L": 9}
        amended.update(check_order(order_id, amend_quantities, 2))
        amended_view = export(amendment, "new-stock-amend", amend_quantities)
        require(
            any(
                source["source_id"] == "synthetic-amend.txt" and source["line_start"] == 1
                for source in amended_view["sources"]
            ),
            "Amendment synthetic provenance missing",
        )
        historical = export(creation, "new-stock-create-after-amend", create_quantities)

        def projection(v):
            return [(r["sku_id"], r["old_qty"], r["new_qty"], r["delta"]) for r in v["line_diffs"]]

        require(projection(original) == projection(historical), "Historical quantity diffs changed")
        require(
            {r["sku_id"]: r["available_qty"] for r in original["line_diffs"]}
            == {"A-WHITE-M": 110, "B-BLUE-L": 14},
            "Unexpected creation availability",
        )
        require(
            {r["sku_id"]: r["available_qty"] for r in historical["line_diffs"]}
            == {"A-WHITE-M": 114, "B-BLUE-L": 11},
            "Old task must show current availability after amendment",
        )
        state = full_snapshot()
        late_replay = durable_commit(factory, **created["simulated_commit_request"])
        require(
            late_replay.replayed
            and late_replay.revision == 1
            and late_replay.operation_id == created["commit"]["operation_id"],
            "Historical commit receipt changed",
        )
        require(full_snapshot() == state, "Original replay reverted amended order")
        check_order(order_id, amend_quantities, 2)
        consistent()
        final = snapshot()
        for table in ("production_order", "purchase_order", "shipment_record"):
            require(
                final[table] == initial[table], "New STOCK lifecycle changed unrelated execution"
            )
        # Approval expiration is a datetime in the service outcome; serialize explicitly.
        for result in (created, amended):
            result["simulated_approval"]["expires_at"] = result["simulated_approval"][
                "expires_at"
            ].isoformat()
        write_json(
            output / "new-stock-result.json",
            {
                "scenario": "new-stock",
                "evidence": "SYNTHETIC FIXTURE; simulated demo approval; direct services",
                "input": inputs,
                "amendment_input": amendment_input,
                "mixed_boundary": boundary,
                "stored_before": initial,
                "stored_after_create": before_amend,
                "stored_after": final,
                "creation": created,
                "amendment": amended,
                "plan_replay_all_tables_unchanged": True,
                "create_view_after_amend": historical,
                "historical_quantity_diffs_preserved": True,
                "exports_use_current_availability": True,
                "original_commit_replay_after_amend": asdict(late_replay),
                "historical_replay_all_tables_unchanged": True,
                "source_ref_preserved": True,
                "supplier_contacted": False,
                "steps": log,
            },
        )
        print(
            "new-stock: 创建白10/蓝6 → 改单白6/黑4/蓝9；模拟批准前业务未变；原提交重放不回滚",
            flush=True,
        )
    finally:
        engine.dispose()


RUNTIME_DIRS = ("app", "schemas", "seeds", "order_ops_migrations")
SOURCE_FILES = (
    "pyproject.toml",
    "alembic.ini",
    "order_ops_migrations/script.py.mako",
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
    with tempfile.TemporaryDirectory(prefix="new-stock-ephemeral-", dir=output) as temporary:
        new_order_journey(output, Path(temporary) / "synthetic.db")


def publish_summary(output):
    with (output / "SUMMARY.md").open("x", encoding="utf-8") as stream:
        stream.write(
            "# 合成 FIXTURE 验收通过\n\n"
            "- 库存改单：黑色 100 → 60，已发 60 保留；白色 0 → 40。\n"
            "- 已裁保护：全改藏青请求先进入澄清；明确回答「只改未开裁」后，"
            "只改未裁 180，黑色已裁 120 保留。\n"
            "- PUR-3002：藏青面料 360 为 PENDING_SUPPLIER 草案，仍需供应商确认。"
            "没有联系供应商，也没有取得对方同意。\n\n"
            "前两条旅程均经过持久输入和单独 CLI worker；批准前订单及业务表未变。"
            "脚本模拟 demo-user 批准，再单独提交；最终 revision 2、SUCCEEDED、"
            "verification passed。同 key 重放返回原 Operation ID，审计各只有一次操作。\n\n"
            "- 新建库存单：白色 8+2 合并为 10，蓝色 6，revision 1；独立模拟批准后提交。\n"
            "- 新单改单：白色 6、黑色 4、蓝色 9，revision 2；预留与订单数量一致。\n"
            "- 原建单提交重放仍返回 revision 1 同一 Operation ID，不回滚当前 revision 2。\n"
            "- 混合 STOCK/MTO 新单明确拒绝 MTO_CREATE_UNSUPPORTED，业务未变。\n\n"
            "新单通过直接服务规划和同步读回，不经过文本提取、上传或 worker。"
            "new-stock-create-after-amend 文件保留建单历史差量（白10/蓝6）；库存等字段"
            "仍来自当前事实，不是完整历史快照。来源是显式合成引用，不证明上传归属。\n\n"
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
