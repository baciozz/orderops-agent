"""The operator disclosure describes retention limits, never current file state."""

from html import escape
from io import BytesIO

import openpyxl
import pytest
from app.config import Settings
from app.db.models import InputSource, Task
from app.db.session import begin_immediate
from app.main import create_app
from app.orchestration.inbox import accept_input
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

NOTICE = (
    "上传成功仅表示当时已解析并保存提取内容，不代表原始文件仍被保留。"
    "原始文件使用临时存储，已保存的提取内容不证明原件当前可用。"
    "即使原始文件丢失，已保存的提取内容仍可读取；"
    "使用同一上传键重试不会恢复原始文件，原件不可用时会拒绝回放。审计摘要不包含原始文件。"
)
HEADERS = {"x-request-id": "retention-read", "x-correlation-id": "retention-read"}


def database_snapshot(engine):
    with engine.connect() as connection:
        return {
            name: connection.execute(text(f'SELECT * FROM "{name}"')).fetchall()
            for name in inspect(connection).get_table_names()
        }


@pytest.mark.parametrize("status", ["NEEDS_CLARIFICATION", "RUNNING", "SUCCEEDED"])
def test_retention_disclosure_is_conditional_and_read_only(
    database_url, session_factory, engine, tmp_path, status
):
    _, task_id, _ = accept_input(
        session_factory, thread_id="retention", client_event_key="one", text="Synthetic evidence"
    )
    _, other_id, _ = accept_input(
        session_factory, thread_id="other", client_event_key="one", text="No uploads"
    )
    settings = Settings(_env_file=None, database_url=database_url, live_enabled=False)
    app = create_app(settings)
    root = tmp_path / "private-path-canary"
    root.mkdir()
    app.state.source_storage_dir = root
    workbook = openpyxl.Workbook()
    workbook.active["A1"] = "Synthetic saved excerpt"
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    uploads = [
        ("<img src=x onerror=alert(1)>.txt", b"Synthetic text"),
        ("synthetic.xlsx", stream.getvalue()),
    ]

    def send(client, index):
        name, content = uploads[index]
        return client.post(
            "/sources",
            headers=HEADERS,
            data={"task_id": task_id, "client_upload_key": f"retention-{index}"},
            files={"file": (name, content)},
        )

    def evidence(client):
        urls = [f"/tasks/{task_id}/view", f"/tasks/{task_id}/audit"] + [
            f"/sources/{source_id}/spans?task_id={task_id}" for source_id in source_ids
        ]
        responses = [client.get(url, headers=HEADERS) for url in urls]
        assert all(response.status_code == 200 for response in responses)
        return [response.json() for response in responses]

    def page(client):
        response = client.get(f"/ui/tasks/{task_id}", headers=HEADERS)
        assert response.status_code == 200
        assert response.text.count('id="evidence-retention-notice"') == 1
        assert response.text.count(NOTICE) == 1
        assert escape(uploads[0][0]) in response.text
        assert uploads[0][0] not in response.text
        assert str(root) not in response.text
        assert "private-path-canary" not in response.text
        return response.text

    with TestClient(app, base_url="http://127.0.0.1") as client:
        for empty_task in (task_id, other_id, "T-STUB"):
            response = client.get(f"/ui/tasks/{empty_task}")
            assert response.status_code == 200
            assert "evidence-retention-notice" not in response.text
        receipts = [send(client, index) for index in range(len(uploads))]
        assert all(receipt.status_code == 200 for receipt in receipts)
        source_ids = [receipt.json()["source_id"] for receipt in receipts]
        with session_factory() as session, begin_immediate(session):
            session.get(Task, task_id).status = status
            paths = [root / source_id for source_id in source_ids]
            assert all(
                session.get(InputSource, sid).storage_ref == str(root / sid) for sid in source_ids
            )
        before_db = database_snapshot(engine)
        before_files = {path: path.read_bytes() for path in paths}
        before_evidence = evidence(client)
        before_page = page(client)
        assert database_snapshot(engine) == before_db
        assert {path: path.read_bytes() for path in paths} == before_files
        assert evidence(client) == before_evidence

    restarted = create_app(settings)
    with TestClient(restarted, base_url="http://127.0.0.1") as client:
        assert page(client) == before_page
        assert evidence(client) == before_evidence
        assert {path: path.read_bytes() for path in paths} == before_files
        assert database_snapshot(engine) == before_db
        # Remove only this fixture's exact generated TXT original, never a DB-supplied path.
        paths[0].unlink()
        assert page(client) == before_page
        assert evidence(client) == before_evidence
        replay = send(client, 0)
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "SOURCE_UNAVAILABLE"
        assert str(root) not in replay.text
        assert send(client, 1).json() == receipts[1].json()
        assert (
            client.get(f"/sources/{source_ids[0]}/spans", params={"task_id": other_id}).status_code
            == 404
        )
        assert database_snapshot(engine) == before_db
        assert not paths[0].exists()
        assert paths[1].read_bytes() == before_files[paths[1]]
        assert restarted.state.source_storage_dir is None
