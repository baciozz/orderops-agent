"""ASGI synthetic browser-header tests, not an actual browser/CORS exercise."""

import pytest
from app.api.browser_security import browser_request_error
from app.db.models import InboxDelivery, Task
from fastapi import Request
from sqlalchemy import func, select

import test_demo_ui

web = test_demo_ui.web


def check(headers=(), method="POST", scheme="http"):
    request = Request(
        {
            "type": "http",
            "method": method,
            "scheme": scheme,
            "path": "/",
            "headers": [(k.encode(), v.encode()) for k, v in headers],
        }
    )
    return browser_request_error(request)


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:80",
        "LOCALHOST:8000",
        "127.0.0.1:8000",
        "[::1]:8000",
    ],
)
def test_loopback_cli_allowed(host):
    assert check([("host", host)]) is None


@pytest.mark.parametrize(
    "host",
    [
        "attacker.invalid",
        "attacker.localhost",
        "localhost.attacker.invalid",
        "127.0.0.2",
        "0.0.0.0",
        "localhost.",
        "localhost:0",
        "localhost:65536",
        "localhost:bad",
        "localhost:8000,attacker.invalid",
        "user@localhost",
        "::1",
        "[::2]",
        "",
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_rebinding_or_malformed_host_denied(host, method):
    assert check([("host", host)], method).code == "UNTRUSTED_HOST"


def test_missing_duplicate_host_and_bad_scheme():
    assert check().code == "UNTRUSTED_HOST"
    assert check([("host", "localhost"), ("host", "localhost")]).code == "UNTRUSTED_HOST"
    assert check([("host", "localhost")], scheme="ftp").code == "UNTRUSTED_HOST"


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "https://attacker.invalid",
        "http://localhost:81",
        "https://localhost",
        "http://user@localhost",
        "http://localhost/",
        "http://localhost?x=y",
        "http://localhost#fragment",
        "http://localhost?",
        "http://localhost#",
        "http://localhost,",
        "http://localhost http://localhost",
        "http://localhost:65536",
        "http://[::1",
        " http://localhost",
        "http://local\nhost",
        "http://127.0.0.1",
        "http://localhost:bad",
        "",
    ],
)
def test_bad_origin_denied(origin):
    assert check([("host", "localhost"), ("origin", origin)]).http_status == 403


@pytest.mark.parametrize(
    "host,origin,scheme",
    [
        ("localhost", "http://localhost:80", "http"),
        ("localhost:80", "http://localhost", "http"),
        ("LOCALHOST:8000", "http://localhost:8000", "http"),
        ("[::1]:8000", "http://[::1]:8000", "http"),
        ("localhost:443", "https://localhost", "https"),
    ],
)
def test_origin_normalization(host, origin, scheme):
    assert check([("host", host), ("origin", origin)], scheme=scheme) is None


@pytest.mark.parametrize(
    "headers",
    [
        [("sec-fetch-site", "cross-site")],
        [("sec-fetch-site", "same-site")],
        [("sec-fetch-site", "none")],
        [("sec-fetch-site", "unknown")],
        [("sec-fetch-mode", "no-cors")],
        [("sec-fetch-dest", "document")],
        [("sec-fetch-user", "?1")],
        [("sec-fetch-site", "cross-site"), ("origin", "http://localhost")],
        [("origin", "http://localhost"), ("origin", "http://localhost")],
        [("referer", "http://localhost/"), ("referer", "http://localhost/")],
        [("sec-fetch-site", "same-origin"), ("sec-fetch-site", "same-origin")],
        [("referer", "https://attacker.invalid/form")],
        [("origin", "http://localhost"), ("referer", "https://attacker.invalid/form")],
        [("origin", "null"), ("referer", "http://localhost/")],
        [("x-forwarded-host", "attacker.invalid"), ("origin", "http://attacker.invalid")],
        [("x-forwarded-proto", "https"), ("origin", "https://localhost")],
    ],
)
def test_conflicting_missing_or_duplicate_browser_evidence_denied(headers):
    assert check([("host", "localhost"), *headers]).code == "CROSS_ORIGIN_REQUEST"


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("origin", "http://localhost")],
        [("sec-fetch-site", "same-origin")],
        [("referer", "http://localhost/path?query=yes")],
        [("sec-fetch-site", "none"), ("origin", "http://localhost")],
    ],
)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "CUSTOM"])
def test_same_origin_and_cli_mutations_allowed(headers, method):
    assert check([("host", "localhost"), *headers], method) is None


@pytest.mark.parametrize(
    "path",
    [
        "/ui/demo/submit",
        "/ui/demo/worker-once",
        "/plans/synthetic/approve",
        "/plans/synthetic/commit",
        "/inputs",
        "/dispatch",
        "/sources",
    ],
)
def test_denial_before_route_validation_and_side_effects(web, path):
    client, factory, _ = web
    response = client.post(
        path,
        content=b"invalid body",
        headers={
            "Origin": "https://attacker.invalid",
            "Sec-Fetch-Site": "cross-site",
            "X-Request-ID": "synthetic-request",
            "X-Correlation-ID": "synthetic-correlation",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CROSS_ORIGIN_REQUEST"
    assert response.json()["request_id"] == response.headers["x-request-id"] == "synthetic-request"
    assert response.json()["correlation_id"] == "synthetic-correlation"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0
        assert session.scalar(select(func.count()).select_from(InboxDelivery)) == 0


def test_same_origin_full_native_form_worker_approval_commit_workflow(web):
    client, _, _ = web
    client.headers.update({"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"})
    test_demo_ui.test_worker_approve_commit_and_read_result(web)


def test_host_denial_applies_to_reads_and_ignores_forwarded_host(web):
    client, _, _ = web
    response = client.get(
        "/ui/demo",
        headers={
            "Host": "attacker.invalid",
            "X-Forwarded-Host": "127.0.0.1",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNTRUSTED_HOST"


@pytest.mark.parametrize(
    "path,headers,status",
    [
        ("/ui/demo", {}, 200),
        ("/ui/demo", {"Host": "attacker.invalid"}, 400),
    ],
)
def test_framing_headers_on_success_and_failure(web, path, headers, status):
    response = web[0].get(path, headers=headers)
    assert response.status_code == status
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"


def test_real_native_form_cross_site_attempt_does_not_queue_task(web):
    client, factory, _ = web
    response = client.post(
        "/ui/demo/submit",
        data={
            "selected_order_id": "SO-1001",
            "text": test_demo_ui.TEXT_BLACK_TO_WHITE,
            "client_event_key": "synthetic-csrf-repro",
            "thread_id": "synthetic",
        },
        headers={"Origin": "https://attacker.invalid", "Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert response.headers["x-frame-options"] == "DENY"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_valid_json_mutations_rejected_without_changing_queued_or_approved_state(web):
    client, factory, _ = web
    task_id, _, _ = test_demo_ui.submit(client)
    hostile = {"Origin": "null", "Sec-Fetch-Site": "cross-site"}
    assert client.post("/ui/demo/worker-once", headers=hostile).status_code == 403
    with factory() as session:
        assert session.get(Task, task_id).status == "QUEUED"
    result = client.post("/ui/demo/worker-once").json()["result"]
    plan_id = result["plan_id"]
    approval_body = {
        "plan_hash": result["plan_hash"],
        "expected_intent_revision": result["intent_revision"],
    }
    assert (
        client.post(f"/plans/{plan_id}/approve", json=approval_body, headers=hostile).status_code
        == 403
    )
    from app.db.models import Approval

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
    approval_id = client.post(f"/plans/{plan_id}/approve", json=approval_body).json()["approval_id"]
    commit_body = {
        "approval_id": approval_id,
        "plan_hash": result["plan_hash"],
        "idempotency_key": "synthetic-browser-boundary",
    }
    assert (
        client.post(f"/plans/{plan_id}/commit", json=commit_body, headers=hostile).status_code
        == 403
    )
    assert client.get("/orders/SO-1001").json()["current_revision"] == 1
    assert client.post(f"/plans/{plan_id}/commit", json=commit_body).status_code == 200
