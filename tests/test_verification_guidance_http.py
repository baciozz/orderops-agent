"""A completed HTTP change explains bounded verification without granting a whole-order pass."""

from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from tests import test_demo_clarification_journeys as journey
from tests import test_link_audit as support
from tests.test_http_storage_failure import real_http

seeded = support.seeded


def test_completed_change_has_readable_scope_and_safe_next_steps(seeded, database_url, tmp_path):
    with real_http(database_url, tmp_path) as (client, _):
        response = client.post(
            "/dispatch", json={"text": TEXT_BLACK_TO_WHITE, "client_event_key": "scope-experience"}
        )
        assert response.status_code == 200
        task = response.json()["task_id"]
        result = journey.approve_commit_replay(client, task, database_url, seeded)
        before = support.dump(database_url)
        view = client.get(f"/tasks/{task}/view").json()
        html, page = journey.page(client, task)
        assert view["result"]["operation_id"] == result["operation_id"]
        assert view["result"]["verification"] == "partial"
        assert view["result"]["verification_details"]["global_completeness"] == "not_assessed"
        assert page.element("verification-scope-notice")
        assert page.element("verification-next-steps")
        for text in [
            "已提交不等于整单核验通过",
            "请勿重复提交",
            "本次订单修订的 SKU、数量与履约方式",
            "本次提交保留的单据关联字段",
            "本次提交的库存预占/释放流水",
            "当前整单的完整来源尚未证明",
            "尚未确认所有共用该库存的订单和依赖",
            "维护人员",
        ]:
            assert text in html
        for field in view["result"]["verification_details"]["execution"]["fields"]:
            item = page.element("verification-field-" + field["field"])
            assert item["data-status"] == field["status"]
            assert item["data-expected-count"] == str(field["expected_count"])
            assert item["data-observed-count"] == str(field["observed_count"])
        uncovered = view["result"]["verification_details"]["execution"]["uncovered"]
        items = [a for _, a in page.tags if a.get("data-uncovered-object")]
        assert [(i["data-uncovered-object"], i["data-reason"]) for i in items] == [
            (i["object_id"], i["reason_code"]) for i in uncovered
        ]
        for path in ["/ui/demo/orders/SO-1001", f"/tasks/{task}/audit"]:
            assert any(a.get("href") == path for _, a in page.tags)
            assert client.get(path).status_code == 200
        assert support.dump(database_url) == before
