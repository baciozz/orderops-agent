"""Synthetic audit-export contract, privacy, consistency, and zero-write evidence."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest
from app.config import Settings
from app.db.models import (
    Approval,
    AuditRecord,
    Event,
    InputPayload,
    InputSource,
    Operation,
    Plan,
    Task,
    TaskSource,
)
from app.main import create_app
from app.services import task_audit
from app.services.seed import load_seed
from fastapi.testclient import TestClient
from schemas.task_audit import TaskAudit
from sqlalchemy import delete, event, select

NOW = datetime(2026, 11, 3, tzinfo=UTC)
SECRET = "PRIVATE-CANARY-DO-NOT-EXPORT"
PLAN_HASH = "sha256:" + "a" * 64


def dump(database_url):
    path = database_url.removeprefix("sqlite:///")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return "\n".join(db.iterdump())


def graph(factory, name="one", *, status="SUCCEEDED", committed=True, pending=False):
    """Only fabricated local rows. No approval, commit, or verification service calls."""
    task_id, plan_id, operation_id = f"T-{name}", f"PL-{name}", f"OP-{name}"
    with factory.begin() as session:
        session.add(
            Task(
                id=task_id,
                chain_id="same-private-chain",
                order_id="SO-1001",
                status=status,
                intent_revision=1,
                cancel_requested=status == "CANCELLED",
            )
        )
        session.flush()
        for n in (2, 1):
            session.add(
                Event(
                    id=f"EV-{name}-{n}",
                    source_event_key=f"{SECRET}-{name}-{n}",
                    thread_id=SECRET,
                    chain_id="same-private-chain",
                    order_id="SO-1001",
                    kind="clarify_answer" if n == 2 else "input.submit",
                    received_at=NOW - timedelta(seconds=n),
                )
            )
            session.flush()
            session.add(
                InputPayload(
                    event_id=f"EV-{name}-{n}",
                    task_id=task_id,
                    body={
                        "input_sequence": n,
                        "text": SECRET,
                        "questions": [SECRET],
                        "extraction": {"secret": SECRET},
                    },
                )
            )
        source_id = f"SRC-{name}"
        session.add(
            InputSource(
                id=source_id,
                sha256="b" * 64,
                media_type="text/plain",
                original_name=SECRET,
                storage_ref="/private/" + SECRET,
                size=42,
                parser_version="static-v1",
            )
        )
        session.flush()
        session.add(TaskSource(task_id=task_id, source_id=source_id))
        refs = [
            {"source_id": source_id, "text_excerpt": SECRET, "sheet": SECRET},
            {"source_id": "SRC-other", "text_excerpt": SECRET},
        ]
        session.add(
            Plan(
                id=plan_id,
                task_id=task_id,
                plan_kind="change",
                base_order_revision=1,
                intent_revision=1,
                chain_intent_revision=None,
                expected_stock_revisions={"secret": SECRET},
                expected_doc_revisions={"secret": SECRET},
                normalized_changes={"changes": [], "customer_id": SECRET},
                source_refs=refs,
                plan_hash=PLAN_HASH,
                status="COMMITTED" if committed else "VALIDATED",
            )
        )
        session.flush()
        session.add(
            Approval(
                id=f"AP-{name}",
                plan_id=plan_id,
                plan_hash=PLAN_HASH,
                actor_id=SECRET,
                intent_revision=1,
                dependencies_hash=SECRET,
                expires_at=NOW + timedelta(minutes=10),
                status="CONSUMED" if committed else "ACTIVE",
                consumed_operation_id=operation_id if committed else None,
            )
        )
        if committed:
            session.add(
                Operation(
                    id=operation_id,
                    idempotency_key=f"{SECRET}-{name}",
                    request_hash=SECRET,
                    plan_id=plan_id,
                    status="COMMITTED",
                    result_json={
                        "order_id": "SO-1001",
                        "revision": 2,
                        "task_id": task_id,
                        "plan_id": plan_id,
                        "chain_id": SECRET,
                        "audit_id": f"AUD-{name}",
                        "private_extra": {"password": SECRET},
                    },
                    committed_verification_pending=pending,
                    verification_retry_attempts=2 if pending else 0,
                    verification_manual_required=pending,
                    verification_lease_token=SECRET,
                    verification_last_error_code=SECRET,
                )
            )
            session.add(
                AuditRecord(
                    id=f"AUD-{name}",
                    actor_type="human",
                    actor_id=SECRET,
                    action="commit",
                    object_id="SO-1001",
                    operation_id=operation_id,
                    before_hash=PLAN_HASH,
                    after_hash=PLAN_HASH,
                    source_refs=refs,
                    at=NOW,
                )
            )
            session.get(Task, task_id).result_ref = operation_id
        session.add(
            AuditRecord(
                id=f"AUD-denied-{name}",
                actor_type="subagent",
                actor_id=SECRET,
                action="tool.denied",
                object_id=task_id,
                source_refs=[{"tool": SECRET}],
                at=NOW,
            )
        )
    return task_id


@pytest.fixture()
def audit_web(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    app = create_app(Settings(_env_file=None, database_url=database_url))
    # No lifespan: its pre-existing budget reconciliation is not part of a GET.
    client = TestClient(app, base_url="http://127.0.0.1")
    yield client, session_factory, app
    client.close()
    app.state.engine.dispose()


def trace_reads(app):
    statements = []

    @event.listens_for(app.state.engine, "connect")
    def trace(dbapi, _):
        dbapi.set_trace_callback(statements.append)

    return statements


def assert_reads_only(statements):
    assert statements
    assert "BEGIN" in statements
    assert "ROLLBACK" in statements
    assert all(
        sql.split()[0].upper() in {"SELECT", "PRAGMA", "BEGIN", "ROLLBACK"} for sql in statements
    ), statements
    assert not any("IMMEDIATE" in sql or "query_only" in sql for sql in statements)
    reads = "\n".join(statements).lower()
    assert "source_span" not in reads
    assert "original_name" not in reads
    assert "storage_ref" not in reads
    assert "source_event_key" not in reads
    assert "actor_id" not in reads
    assert "idempotency_key" not in reads
    assert "lease_token" not in reads
    assert "normalized_changes" not in reads
    for line in statements:
        if line.lstrip().upper().startswith("SELECT"):
            assert "SELECT input_payload.body" not in line
            assert "SELECT operation.result_json" not in line
            assert "SELECT plan.source_refs" not in line


@pytest.mark.parametrize(
    "status,committed,pending",
    [
        ("AWAITING_APPROVAL", False, False),
        ("COMMITTING", True, True),
        ("SUCCEEDED", True, False),
        ("CANCELLED", False, False),
    ],
)
def test_repeated_get_is_allowlisted_deterministic_and_entire_database_read_only(
    audit_web,
    database_url,
    status,
    committed,
    pending,
):
    client, factory, app = audit_web
    task_id = graph(factory, status=status, committed=committed, pending=pending)
    statements = trace_reads(app)
    before = dump(database_url)
    first = client.get(f"/tasks/{task_id}/audit")
    second = client.get(f"/tasks/{task_id}/audit")
    assert first.status_code == second.status_code == 200, first.text
    assert first.content == second.content
    assert dump(database_url) == before
    assert_reads_only(statements)
    assert SECRET not in first.text
    assert "PL-one" not in first.text and "AP-one" not in first.text
    assert "before_hash" not in first.text and "after_hash" not in first.text
    assert "SRC-other" not in first.text and "AUD-denied" not in first.text
    body = first.json()
    TaskAudit.model_validate(body)
    assert set(body) == {
        "export_version",
        "semantics",
        "limits",
        "task",
        "inputs",
        "plans",
        "committed_operations",
        "business_audits",
        "sources",
    }
    assert body["export_version"] == "1.0"
    assert body["limits"] == {"rows_per_collection": 1000, "output_bytes": 4 * 1024 * 1024}
    assert body["task"]["status"] == status
    assert [row["input_sequence"] for row in body["inputs"]] == [1, 2]
    assert body["semantics"]["causal_sequence_complete"] is True
    assert body["semantics"]["input_plan_link"] == "not_recorded_not_inferred"
    assert body["plans"][0]["source_ids"] == ["SRC-one"]
    assert len(body["committed_operations"]) == int(committed)
    if committed:
        operation = body["committed_operations"][0]
        assert operation["receipt"] == {"order_id": "SO-1001", "revision": 2, "audit_id": "AUD-one"}
        assert operation["current_verification"]["pending"] is pending
        assert "current_verification" not in operation["receipt"]
        assert body["business_audits"][0]["source_ids"] == ["SRC-one"]
    assert first.headers["content-type"] == "application/json"
    assert first.headers["content-disposition"] == 'attachment; filename="task-audit-v1.json"'
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["x-content-type-options"] == "nosniff"


def test_same_chain_order_tasks_do_not_share_export_data(audit_web, database_url):
    client, factory, app = audit_web
    graph(factory, "other")
    task_id = graph(factory)
    before = dump(database_url)
    body = client.get(f"/tasks/{task_id}/audit").json()
    assert "other" not in json.dumps(body)
    assert len(body["inputs"]) == 2 and len(body["plans"]) == 1
    assert len(body["committed_operations"]) == len(body["sources"]) == 1
    assert dump(database_url) == before


def test_empty_task_and_missing_task_are_not_stubbed(audit_web, database_url):
    client, factory, _ = audit_web
    with factory.begin() as session:
        session.add(
            Task(id="T-empty", chain_id="same-private-chain", status="QUEUED", intent_revision=0)
        )
    before = dump(database_url)
    body = client.get("/tasks/T-empty/audit").json()
    assert body["task"]["order_id"] is None
    assert body["semantics"]["causal_sequence_complete"] is False
    for key in ("inputs", "plans", "committed_operations", "business_audits", "sources"):
        assert body[key] == []
    for task in ("missing", "T-STUB"):
        response = client.get(f"/tasks/{task}/audit")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"
    assert dump(database_url) == before


@pytest.mark.parametrize("existing", [False, True])
def test_absent_and_empty_database_do_not_create_files_or_tables(tmp_path, existing):
    path = tmp_path / "empty.sqlite"
    if existing:
        path.touch()
    settings = Settings(_env_file=None, database_url=f"sqlite:///{path}")
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1")
    for _ in range(2):
        response = client.get("/tasks/no-task/audit")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"
    assert sorted(p.name for p in tmp_path.iterdir()) == (["empty.sqlite"] if existing else [])
    if existing:
        assert path.stat().st_size == 0
    client.close()
    app.state.engine.dispose()
    assert path.exists() is existing
    if existing:
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT name FROM sqlite_master").fetchall() == []
    else:
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("sequence", [None])
def test_legacy_input_sequence_is_explicit_noncausal(audit_web, sequence):
    client, factory, _ = audit_web
    task_id = graph(factory)
    with factory.begin() as session:
        session.get(InputPayload, "EV-one-1").body = {"text": SECRET, "input_sequence": sequence}
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["semantics"]["causal_sequence_complete"] is False
    assert [row["input_sequence"] for row in body["inputs"]] == [2, None]


def test_duplicate_sequence_is_not_claimed_causal(audit_web):
    client, factory, _ = audit_web
    task_id = graph(factory)
    with factory.begin() as session:
        session.get(InputPayload, "EV-one-1").body = {"input_sequence": 2}
    assert (
        client.get(f"/tasks/{task_id}/audit").json()["semantics"]["causal_sequence_complete"]
        is False
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "T-other"),
        ("plan_id", "PL-other"),
        ("order_id", "SO-1002"),
        ("revision", True),
        ("revision", "2"),
        ("revision", 0),
        ("revision", 2.5),
        ("revision", 10**40),
        ("audit_id", "AUD-other"),
    ],
)
def test_receipt_corruption_fails_closed_without_mutating(audit_web, database_url, field, value):
    client, factory, _ = audit_web
    task_id = graph(factory)
    with factory.begin() as session:
        operation = session.get(Operation, "OP-one")
        operation.result_json = {**operation.result_json, field: value}
    before = dump(database_url)
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "AUDIT_EXPORT_INVALID"
    assert SECRET not in response.text
    assert dump(database_url) == before


@pytest.mark.parametrize(
    "model,field,value",
    [
        (Approval, "consumed_operation_id", "OP-other"),
        (AuditRecord, "object_id", "SO-1002"),
        (Plan, "plan_hash", SECRET),
        (InputSource, "sha256", SECRET),
        (InputSource, "media_type", SECRET),
        (InputSource, "parser_version", SECRET),
    ],
)
def test_invalid_metadata_or_cross_task_references_fail_closed(audit_web, model, field, value):
    client, factory, _ = audit_web
    task_id = graph(factory)
    with factory.begin() as session:
        row = session.scalar(select(model))
        setattr(row, field, value)
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AUDIT_EXPORT_INVALID"
    assert SECRET not in response.text


def test_unknown_actor_and_event_kind_never_emit_arbitrary_text(audit_web):
    client, factory, _ = audit_web
    task_id = graph(factory)
    with factory.begin() as session:
        session.get(AuditRecord, "AUD-one").actor_type = SECRET
        session.get(Event, "EV-one-1").kind = SECRET
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 200 and SECRET not in response.text
    assert response.json()["business_audits"][0]["actor_type"] == "unknown"
    assert response.json()["inputs"][0]["kind"] == "unknown"


@pytest.mark.parametrize("value", [None, [], "bad", {"revision": float("nan")}])
def test_malformed_receipt_returns_generic_error(audit_web, value):
    client, factory, _ = audit_web
    task_id = graph(factory)
    with factory.begin() as session:
        session.get(Operation, "OP-one").result_json = value
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AUDIT_EXPORT_INVALID"
    assert SECRET not in response.text


def test_row_and_byte_limits_fail_without_partial_report(audit_web, monkeypatch, database_url):
    client, factory, _ = audit_web
    task_id = graph(factory)
    before = dump(database_url)
    with monkeypatch.context() as patch:
        patch.setattr(task_audit, "MAX_ROWS", 1)
        response = client.get(f"/tasks/{task_id}/audit")
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "AUDIT_EXPORT_LIMIT"
    content = client.get(f"/tasks/{task_id}/audit").content
    with monkeypatch.context() as patch:
        patch.setattr(task_audit, "MAX_OUTPUT_BYTES", len(content) - 10)
        response = client.get(f"/tasks/{task_id}/audit")
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "AUDIT_EXPORT_LIMIT"
    assert dump(database_url) == before


def test_snapshot_is_consistent_while_wal_writer_commits(audit_web):
    client, factory, app = audit_web
    task_id = graph(factory, status="COMMITTING", pending=True)
    wrote = False

    def write_new_state():
        with factory.begin() as session:
            session.get(Task, task_id).status = "SUCCEEDED"
            session.get(Operation, "OP-one").committed_verification_pending = False
            session.get(InputSource, "SRC-one").sha256 = "c" * 64

    @event.listens_for(app.state.engine, "after_cursor_execute")
    def change_after_first_read(connection, cursor, statement, parameters, context, executemany):
        nonlocal wrote
        if not wrote and statement.startswith("SELECT task.id"):
            wrote = True
            # The writer must commit before this read transaction continues.
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(write_new_state).result(timeout=3)

    first = client.get(f"/tasks/{task_id}/audit").json()
    assert wrote
    assert first["task"]["status"] == "COMMITTING"
    assert first["committed_operations"][0]["current_verification"]["pending"] is True
    assert first["sources"][0]["sha256"] == "b" * 64
    second = client.get(f"/tasks/{task_id}/audit").json()
    assert second["task"]["status"] == "SUCCEEDED"
    assert second["committed_operations"][0]["current_verification"]["pending"] is False
    assert second["sources"][0]["sha256"] == "c" * 64
    assert (
        first["committed_operations"][0]["receipt"] == second["committed_operations"][0]["receipt"]
    )


def test_ui_link_and_safe_attachment_name(audit_web):
    client, factory, _ = audit_web
    task_id = graph(factory, committed=False, status="AWAITING_APPROVAL")
    page = client.get(f"/ui/tasks/{task_id}")
    assert page.status_code == 200
    assert f'href="/tasks/{task_id}/audit"' in page.text
    assert "下载任务审计摘要（JSON）" in page.text
    assert "/audit" not in client.get("/ui/tasks/T-STUB").text
    simple_id = 'T-"<bad>'
    with factory.begin() as session:
        session.add(Task(id=simple_id, chain_id="test", status="QUEUED", intent_revision=0))
    page = client.get(f"/ui/tasks/{quote(simple_id, safe='')}")
    assert page.status_code == 200
    assert f'href="/tasks/{quote(simple_id, safe="")}/audit"' in page.text
    response = client.get(f"/tasks/{quote(simple_id, safe='')}/audit")
    assert response.status_code == 200
    assert response.json()["task"]["task_id"] == simple_id
    assert response.headers["content-disposition"] == 'attachment; filename="task-audit-v1.json"'
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "table,column",
    [
        ("event", "received_at"),
        ("approval", "expires_at"),
        ("operation", "verification_next_attempt_at"),
        ("audit_record", "at"),
    ],
)
def test_corrupt_stored_dates_are_generic_errors(audit_web, database_url, table, column):
    client, factory, _ = audit_web
    task_id = graph(factory)
    path = database_url.removeprefix("sqlite:///")
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE {table} SET {column} = ?", (SECRET,))
    before = dump(database_url)
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AUDIT_EXPORT_INVALID"
    assert SECRET not in response.text
    assert dump(database_url) == before


@pytest.mark.parametrize(
    "table,column",
    [
        ("input_payload", "body"),
        ("operation", "result_json"),
        ("plan", "source_refs"),
        ("audit_record", "source_refs"),
    ],
)
def test_raw_malformed_json_is_rejected_without_content_exposure(
    audit_web,
    database_url,
    table,
    column,
):
    client, factory, _ = audit_web
    task_id = graph(factory)
    path = database_url.removeprefix("sqlite:///")
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE {table} SET {column} = ?", ("not JSON " + SECRET,))
    before = dump(database_url)
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AUDIT_EXPORT_INVALID"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert SECRET not in response.text
    assert dump(database_url) == before


def test_legitimate_unicode_identifiers_round_trip(audit_web):
    client, factory, _ = audit_web
    task_id = "任务-演示"
    with factory.begin() as session:
        session.add(Task(id=task_id, chain_id="test", status="QUEUED", intent_revision=0))
    response = client.get(f"/tasks/{quote(task_id, safe='')}/audit")
    assert response.status_code == 200
    assert response.json()["task"]["task_id"] == task_id
    assert response.json()["semantics"]["runtime_mode"] == "not_attested_by_this_report"


def test_exact_thousand_input_limit_then_overflow(audit_web, database_url):
    client, factory, _ = audit_web
    task_id = "T-many"
    with factory.begin() as session:
        session.add(Task(id=task_id, chain_id="test", status="QUEUED", intent_revision=0))
        session.flush()
        for start in (0, 500):
            session.add_all(
                Event(
                    id=f"EV-many-{n:04}",
                    source_event_key=f"event-{n}",
                    thread_id="many",
                    chain_id="many",
                    kind="input.submit",
                    received_at=NOW,
                )
                for n in range(start, start + 500)
            )
            session.flush()
            session.add_all(
                InputPayload(
                    event_id=f"EV-many-{n:04}",
                    task_id=task_id,
                    body={"input_sequence": n + 1, "text": SECRET},
                )
                for n in range(start, start + 500)
            )
    before = dump(database_url)
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 200
    assert len(response.json()["inputs"]) == 1000
    assert response.json()["inputs"][-1]["input_sequence"] == 1000
    assert dump(database_url) == before
    with factory.begin() as session:
        session.add(
            Event(
                id="EV-overflow",
                source_event_key="overflow",
                thread_id="many",
                chain_id="many",
                kind="input.submit",
                received_at=NOW,
            )
        )
        session.flush()
        session.add(
            InputPayload(event_id="EV-overflow", task_id=task_id, body={"input_sequence": 1001})
        )
    before = dump(database_url)
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "AUDIT_EXPORT_LIMIT"
    assert dump(database_url) == before


def test_error_boundary_and_host_validation(audit_web):
    client, factory, _ = audit_web
    task_id = graph(factory)
    assert client.post(f"/tasks/{task_id}/audit").status_code == 405
    response = client.get(f"/tasks/{task_id}/audit", headers={"host": "attacker.example"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNTRUSTED_HOST"


@pytest.mark.parametrize("sequence", [0, -1, True, "1", {}, [1], 1.5])
def test_provided_invalid_input_sequence_fails_closed(audit_web, database_url, sequence):
    client, factory, _ = audit_web
    task_id = graph(factory)
    with factory.begin() as session:
        session.get(InputPayload, "EV-one-1").body = {"input_sequence": sequence}
    before = dump(database_url)
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AUDIT_EXPORT_INVALID"
    assert dump(database_url) == before


@pytest.mark.parametrize("body", [{"text": SECRET}, {"input_sequence": 4}])
def test_missing_or_gapped_sequence_does_not_claim_complete_causal_order(audit_web, body):
    client, factory, _ = audit_web
    task_id = graph(factory)
    with factory.begin() as session:
        session.get(InputPayload, "EV-one-1").body = body
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 200
    assert response.json()["semantics"]["causal_sequence_complete"] is False


@pytest.mark.parametrize("value", [123, True, {"x": 1}, [1]])
def test_receipt_id_type_never_coerces_into_matching_string_id(audit_web, value):
    client, factory, _ = audit_web
    task_id = graph(factory)
    sqlite_string = "1" if value is True else json.dumps(value, separators=(",", ":"))
    with factory.begin() as session:
        session.get(AuditRecord, "AUD-one").id = sqlite_string
        operation = session.get(Operation, "OP-one")
        operation.result_json = {**operation.result_json, "audit_id": value}
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AUDIT_EXPORT_INVALID"


@pytest.mark.parametrize("model", [Plan, AuditRecord])
@pytest.mark.parametrize("value", [123, True, {"x": 1}, [1]])
def test_source_ref_id_type_never_coerces_into_matching_owned_source(audit_web, model, value):
    client, factory, _ = audit_web
    task_id = graph(factory)
    sqlite_string = "1" if value is True else json.dumps(value, separators=(",", ":"))
    with factory.begin() as session:
        session.add(
            InputSource(
                id=sqlite_string,
                sha256="d" * 64,
                media_type="text/plain",
                original_name=SECRET,
                storage_ref=SECRET,
                size=1,
                parser_version="static-v1",
            )
        )
        session.flush()
        session.add(TaskSource(task_id=task_id, source_id=sqlite_string))
        row = session.get(model, "PL-one" if model is Plan else "AUD-one")
        row.source_refs = [{"source_id": value}]
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AUDIT_EXPORT_INVALID"


@pytest.mark.parametrize("model", [Plan, AuditRecord])
@pytest.mark.parametrize("refs", [{"extra": {"source_id": "SRC-one"}}, [SECRET], [{}]])
def test_invalid_source_reference_shape_is_not_inferred(audit_web, model, refs):
    client, factory, _ = audit_web
    task_id = graph(factory)
    with factory.begin() as session:
        session.get(model, "PL-one" if model is Plan else "AUD-one").source_refs = refs
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AUDIT_EXPORT_INVALID"
    assert SECRET not in response.text


def test_nullable_business_audit_sources_are_empty(audit_web):
    client, factory, _ = audit_web
    task_id = graph(factory)
    with factory.begin() as session:
        session.get(AuditRecord, "AUD-one").source_refs = None
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 200
    assert response.json()["business_audits"][0]["source_ids"] == []


@pytest.mark.parametrize("committed", [False, True])
def test_plan_without_durable_inputs_does_not_claim_causal_completeness(
    audit_web,
    database_url,
    committed,
):
    client, factory, app = audit_web
    task_id = graph(
        factory, committed=committed, status="SUCCEEDED" if committed else "AWAITING_APPROVAL"
    )
    # Legacy/direct planning need not have durable inbox receipts. Preserve
    # the plan/commit evidence while modeling that absence explicitly.
    with factory.begin() as session:
        session.execute(delete(InputPayload).where(InputPayload.task_id == task_id))
    before = dump(database_url)
    statements = trace_reads(app)
    response = client.get(f"/tasks/{task_id}/audit")
    assert response.status_code == 200
    body = response.json()
    assert body["inputs"] == []
    assert body["semantics"]["causal_sequence_complete"] is False
    assert len(body["plans"]) == 1
    assert len(body["committed_operations"]) == int(committed)
    assert dump(database_url) == before
    assert_reads_only(statements)
