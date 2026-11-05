"""Freeze approval snapshot data before relocating the two pure kernel helpers.

Expected payloads and canonical JSON are independently written literals. Their
hashes are SHA-256 of those literal UTF-8 strings, not output from either helper.
"""

import copy
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.domain.changes import ChangeEval, SnapLine
from app.domain.hashing import canonical_hash
from app.services import kernel

CREATE_PAYLOAD = {
    "schema_version": "1.0",
    "plan_kind": "CREATE",
    "order_id": None,
    "customer_id": "客户-一",
    "base_revision": 0,
    "intent_revision": 1,
    "lines": [{"sku_id": "蓝-M", "ordered_qty": 2, "fulfillment_mode": "STOCK"}],
    "reservation_delta": [{"sku_id": "蓝-M", "qty": 2}],
    "stock_revisions": {"蓝-M": 7},
    "downstream": [],
    "expected_doc_revisions": {},
}
CREATE_JSON = (
    '{"base_revision":0,"customer_id":"客户-一","downstream":[],"expected_doc_revisions":{},'
    '"intent_revision":1,"lines":[{"fulfillment_mode":"STOCK","ordered_qty":2,"sku_id":"蓝-M"}],'
    '"order_id":null,"plan_kind":"CREATE","reservation_delta":[{"qty":2,"sku_id":"蓝-M"}],'
    '"schema_version":"1.0","stock_revisions":{"蓝-M":7}}'
)
CREATE_HASH = "sha256:ba04590c0e1bd77da19a9ab745a6fd3edbb51ad25e1d5a5ccf1e18ae44fd12dc"

CHANGE_PAYLOAD = {
    "schema_version": "1.0",
    "plan_kind": "CHANGE",
    "order_id": "SO-一",
    "customer_id": "客户-一",
    "base_revision": 3,
    "intent_revision": 5,
    "lines": [
        {"sku_id": "A-S", "ordered_qty": 6, "fulfillment_mode": "STOCK"},
        {"sku_id": "蓝-M", "ordered_qty": 12, "fulfillment_mode": "MTO"},
    ],
    "reservation_delta": [{"sku_id": "A-S", "qty": -2}, {"sku_id": "蓝-M", "qty": 0}],
    "stock_revisions": {"A-S": 4, "蓝-M": 9},
    "downstream": [
        {"doc_id": "A", "before": None, "after": {"qty": 2}},
        {"doc_id": "Z", "before": {"qty": 1}, "after": None},
    ],
    "expected_doc_revisions": {"MO-1": {"doc_type": "production_order", "doc_revision": 2}},
}
CHANGE_JSON = (
    '{"base_revision":3,"customer_id":"客户-一","downstream":['
    '{"after":{"qty":2},"before":null,"doc_id":"A"},'
    '{"after":null,"before":{"qty":1},"doc_id":"Z"}],'
    '"expected_doc_revisions":{"MO-1":{"doc_revision":2,"doc_type":"production_order"}},'
    '"intent_revision":5,"lines":['
    '{"fulfillment_mode":"STOCK","ordered_qty":6,"sku_id":"A-S"},'
    '{"fulfillment_mode":"MTO","ordered_qty":12,"sku_id":"蓝-M"}],'
    '"order_id":"SO-一","plan_kind":"CHANGE","reservation_delta":['
    '{"qty":-2,"sku_id":"A-S"},{"qty":0,"sku_id":"蓝-M"}],'
    '"schema_version":"1.0","stock_revisions":{"A-S":4,"蓝-M":9}}'
)
CHANGE_HASH = "sha256:adaec280b0221d1d43378dc3a4cb96b8e199c295451db7270afb4da23dd48d4e"

DOCUMENT_FINGERPRINT = {
    "生产-1": {
        "doc_type": "production_order",
        "doc_revision": 2,
        "status": "PLANNED",
        "cut_qty": 3,
        "planned_qty": 20,
        "sku_id": "蓝-M",
    },
    "采购-1": {
        "doc_type": "purchase_order",
        "doc_revision": 4,
        "status": "RECEIVED",
        "received_qty": 20,
        "ordered_qty": 20,
        "change_state": "UNCHANGED",
        "material_id": "布-蓝",
        "supplier_id": "供方-一",
        "production_order_id": "生产-1",
    },
    "采购-2": {"doc_type": "purchase_order", "exists": False},
}
DOCUMENT_JSON = (
    '{"生产-1":{"cut_qty":3,"doc_revision":2,"doc_type":"production_order",'
    '"planned_qty":20,"sku_id":"蓝-M","status":"PLANNED"},'
    '"采购-1":{"change_state":"UNCHANGED","doc_revision":4,"doc_type":"purchase_order",'
    '"material_id":"布-蓝","ordered_qty":20,"production_order_id":"生产-1",'
    '"received_qty":20,"status":"RECEIVED","supplier_id":"供方-一"},'
    '"采购-2":{"doc_type":"purchase_order","exists":false}}'
)
DOCUMENT_HASH = "sha256:8b19cb8a23b35a05b6a8471137dad6dcd9bbbd9d6d4259ae6946b4858986170b"


def _create_arguments():
    return {
        "plan_kind": "CREATE",
        "order_id": None,
        "customer_id": "客户-一",
        "base_revision": 0,
        "intent_revision": 1,
        "evaluation": ChangeEval(
            kind="validated",
            lines={"ignored-map-key": SnapLine("蓝-M", 2, "STOCK")},
            reservation_delta={"蓝-M": 2},
        ),
        "stock_revisions": {"蓝-M": 7},
    }


def _change_arguments():
    return {
        "plan_kind": "CHANGE",
        "order_id": "SO-一",
        "customer_id": "客户-一",
        "base_revision": 3,
        "intent_revision": 5,
        "evaluation": ChangeEval(
            kind="validated",
            lines={
                "first-map-key": SnapLine("蓝-M", 12, "MTO", cut_qty=3),
                "last-map-key": SnapLine("A-S", 6, "STOCK", shipped_qty=2),
            },
            reservation_delta={"蓝-M": 0, "A-S": -2},
        ),
        "stock_revisions": {"蓝-M": 9, "A-S": 4},
        "verdicts": [
            {"doc_id": "Z", "before": {"qty": 1}, "after": None},
            {"doc_id": "A", "before": None, "after": {"qty": 2}},
        ],
        "expected_docs": {"MO-1": {"doc_type": "production_order", "doc_revision": 2}},
    }


def _documents():
    return {
        "production": [
            {
                "doc_id": "生产-1",
                "doc_revision": "2",
                "status": "PLANNED",
                "cut_qty": "3",
                "planned_qty": "20",
                "sku_id": "蓝-M",
                "order_line_sku": "ignored",
            }
        ],
        "purchase": [
            {
                "doc_id": "采购-1",
                "doc_revision": "4",
                "status": "RECEIVED",
                "received_qty": "20",
                "ordered_qty": 20,
                "change_state": "UNCHANGED",
                "material_id": "布-蓝",
                "supplier_id": "供方-一",
                "production_order_id": "生产-1",
                "order_line_sku": "ignored",
            }
        ],
    }


@pytest.mark.parametrize(
    "arguments,expected,encoded,digest",
    [
        (_create_arguments, CREATE_PAYLOAD, CREATE_JSON, CREATE_HASH),
        (_change_arguments, CHANGE_PAYLOAD, CHANGE_JSON, CHANGE_HASH),
    ],
    ids=["create", "change"],
)
def test_characterization_payload_literals(arguments, expected, encoded, digest):
    inputs = arguments()
    before = copy.deepcopy(inputs)
    actual = kernel._payload(**inputs)
    assert actual == expected
    assert list(actual) == list(expected)
    assert json.dumps(actual, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == encoded
    assert canonical_hash(actual) == digest
    assert inputs == before


def test_characterization_document_literals():
    documents = _documents()
    verdicts = [
        {"doc_id": "生产-1", "before": {}},
        {"doc_id": "采购-2", "before": None, "after": {"ordered_qty": 2}},
    ]
    before = copy.deepcopy((documents, verdicts))
    actual = kernel._doc_fingerprint(documents, verdicts)
    assert actual == DOCUMENT_FINGERPRINT
    assert list(actual) == ["生产-1", "采购-1", "采购-2"]
    for doc_id, expected in DOCUMENT_FINGERPRINT.items():
        assert list(actual[doc_id]) == list(expected)
    assert (
        json.dumps(actual, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        == DOCUMENT_JSON
    )
    assert canonical_hash(actual) == DOCUMENT_HASH
    assert (documents, verdicts) == before


def test_characterization_payload_sorting_and_shallow_sharing():
    inputs = _change_arguments()
    expected_docs = inputs["expected_docs"]
    verdicts = inputs["verdicts"]
    actual = kernel._payload(**inputs)
    assert actual["expected_doc_revisions"] is expected_docs
    assert actual["downstream"] is not verdicts
    assert actual["downstream"][0] is verdicts[1]
    assert actual["downstream"][1] is verdicts[0]
    assert actual["stock_revisions"] is not inputs["stock_revisions"]
    assert list(actual["stock_revisions"]) == ["A-S", "蓝-M"]
    assert [item["sku_id"] for item in actual["lines"]] == ["A-S", "蓝-M"]
    assert [item["sku_id"] for item in actual["reservation_delta"]] == ["A-S", "蓝-M"]
    inputs["evaluation"].lines = dict(reversed(list(inputs["evaluation"].lines.items())))
    inputs["evaluation"].reservation_delta = {"A-S": -2, "蓝-M": 0}
    inputs["stock_revisions"] = {"A-S": 4, "蓝-M": 9}
    assert canonical_hash(kernel._payload(**inputs)) == CHANGE_HASH


@pytest.mark.parametrize("verdicts,expected_docs", [(None, None), ([], {})])
def test_characterization_empty_defaults_allocate_new_containers(verdicts, expected_docs):
    inputs = _create_arguments()
    first = kernel._payload(**inputs, verdicts=verdicts, expected_docs=expected_docs)
    second = kernel._payload(**inputs, verdicts=verdicts, expected_docs=expected_docs)
    assert first == second == CREATE_PAYLOAD
    assert first["downstream"] is not verdicts
    assert first["downstream"] is not second["downstream"]
    assert first["expected_doc_revisions"] is not expected_docs
    assert first["expected_doc_revisions"] is not second["expected_doc_revisions"]
    assert kernel._doc_fingerprint({"production": [], "purchase": []}, verdicts) == {}


def test_characterization_downstream_sort_is_lexical_and_stable():
    verdicts = [{"doc_id": 2, "marker": "first"}, {"doc_id": "2", "marker": "second"}]
    verdicts.append({"doc_id": "10", "marker": "third"})
    actual = kernel._payload(**_create_arguments(), verdicts=verdicts)["downstream"]
    assert actual == [
        {"doc_id": "10", "marker": "third"},
        {"doc_id": 2, "marker": "first"},
        {"doc_id": "2", "marker": "second"},
    ]
    assert actual[1] is verdicts[0]
    assert actual[2] is verdicts[1]


@pytest.mark.parametrize("stage", ["production", "purchase", "absent"])
def test_characterization_duplicate_document_ids_last_write_wins(stage):
    documents = _documents()
    first = documents["production"][0]
    documents["production"].append({**first, "planned_qty": "21"})
    expected = {**DOCUMENT_FINGERPRINT["生产-1"], "planned_qty": 21}
    documents["purchase"][0]["doc_id"] = "生产-1"
    if stage == "production":
        documents["purchase"] = []
    else:
        expected = DOCUMENT_FINGERPRINT["采购-1"]
    verdicts = []
    if stage == "absent":
        verdicts = [{"doc_id": "生产-1", "before": None}]
        expected = {"doc_type": "purchase_order", "exists": False}
    assert kernel._doc_fingerprint(documents, verdicts) == {"生产-1": expected}


@pytest.mark.parametrize("before", [False, 0, "", {}, []])
def test_characterization_only_none_creates_absent_document(before):
    assert (
        kernel._doc_fingerprint(
            {"production": [], "purchase": []}, [{"doc_id": "unused", "before": before}]
        )
        == {}
    )


def test_characterization_document_id_and_integer_conversions():
    documents = _documents()
    documents["purchase"] = []
    documents["production"][0].update(doc_id=123, doc_revision=True, cut_qty=2.9, planned_qty="20")
    actual = kernel._doc_fingerprint(documents, [{"doc_id": 456, "before": None}])
    assert actual == {
        "123": {
            "doc_type": "production_order",
            "doc_revision": 1,
            "status": "PLANNED",
            "cut_qty": 2,
            "planned_qty": 20,
            "sku_id": "蓝-M",
        },
        "456": {"doc_type": "purchase_order", "exists": False},
    }


@pytest.mark.parametrize(
    "documents,verdicts,error,args",
    [
        ({}, None, KeyError, ("production",)),
        ({"production": []}, None, KeyError, ("purchase",)),
        ({"production": [{}], "purchase": []}, None, KeyError, ("doc_revision",)),
        ({"production": [], "purchase": []}, [{}], KeyError, ("before",)),
        ({"production": [], "purchase": []}, [{"before": None}], KeyError, ("doc_id",)),
    ],
)
def test_characterization_missing_document_keys(documents, verdicts, error, args):
    with pytest.raises(error) as caught:
        kernel._doc_fingerprint(documents, verdicts)
    assert caught.value.args == args


@pytest.mark.parametrize("value,error", [("invalid", ValueError), (None, TypeError)])
def test_characterization_invalid_integer_keeps_original_exception(value, error):
    documents = _documents()
    documents["production"][0]["doc_revision"] = value
    with pytest.raises(error):
        kernel._doc_fingerprint(documents)


def test_characterization_missing_verdict_id_is_not_silently_filtered():
    with pytest.raises(KeyError) as caught:
        kernel._payload(**_create_arguments(), verdicts=[{}])
    assert caught.value.args == ("doc_id",)


@pytest.mark.parametrize(
    "field,value",
    [
        ("customer_id", "客户-二"),
        ("order_id", "SO-二"),
        ("plan_kind", "CREATE"),
        ("base_revision", 4),
        ("intent_revision", 6),
        ("stock_revisions", {"A-S": 4, "蓝-M": 10}),
        ("expected_docs", {"MO-1": {"doc_type": "production_order", "doc_revision": 3}}),
    ],
)
def test_characterization_approval_identity_fields_remain_hashed(field, value):
    inputs = _change_arguments()
    inputs[field] = value
    assert canonical_hash(kernel._payload(**inputs)) != CHANGE_HASH


def _run_fresh_python(script):
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_domain_snapshot_import_requires_no_infrastructure():
    _run_fresh_python(
        """
import sys
from importlib.abc import MetaPathFinder

forbidden = ("sqlalchemy", "app.db", "app.services", "app.orchestration", "seeds")

class RejectInfrastructure(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
            raise AssertionError("Unexpected infrastructure import: " + fullname)

sys.meta_path.insert(0, RejectInfrastructure())
from app.domain.plan_snapshot import build_plan_payload, document_fingerprint

assert callable(build_plan_payload)
assert document_fingerprint({"production": [], "purchase": []}) == {}
assert not any(
    module == name or module.startswith(name + ".")
    for module in sys.modules for name in forbidden
)
"""
    )


@pytest.mark.parametrize(
    "modules",
    [
        [
            "app.services.kernel",
            "app.orchestration.inbox",
            "app.services.verification",
            "app.services.task_view",
            "app.orchestration.tools",
            "app.orchestration.dispatch",
        ],
        [
            "app.orchestration.dispatch",
            "app.orchestration.tools",
            "app.services.task_view",
            "app.services.verification",
            "app.orchestration.inbox",
            "app.services.kernel",
        ],
        [
            "app.services.verification",
            "app.orchestration.inbox",
            "app.services.kernel",
            "app.orchestration.tools",
            "app.orchestration.dispatch",
            "app.services.task_view",
        ],
    ],
)
def test_kernel_consumers_import_in_fresh_processes(modules):
    _run_fresh_python(
        "import importlib\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "from app.services import kernel\n"
        "from app.domain import plan_snapshot\n"
        "assert kernel._payload is plan_snapshot.build_plan_payload\n"
        "assert kernel._doc_fingerprint is plan_snapshot.document_fingerprint\n"
    )


def test_kernel_aliases_retain_old_call_signatures_and_literal_results():
    from inspect import signature

    from app.domain.plan_snapshot import build_plan_payload, document_fingerprint

    assert kernel._payload is build_plan_payload
    assert kernel._doc_fingerprint is document_fingerprint
    assert list(signature(kernel._payload).parameters) == [
        "plan_kind",
        "order_id",
        "customer_id",
        "base_revision",
        "intent_revision",
        "evaluation",
        "stock_revisions",
        "verdicts",
        "expected_docs",
    ]
    assert list(signature(kernel._doc_fingerprint).parameters) == ["documents", "verdicts"]
    assert build_plan_payload(**_create_arguments()) == CREATE_PAYLOAD
    assert build_plan_payload(**_change_arguments()) == CHANGE_PAYLOAD
    assert (
        document_fingerprint(_documents(), [{"doc_id": "采购-2", "before": None}])
        == DOCUMENT_FINGERPRINT
    )


@pytest.mark.parametrize("exists", [False, True])
def test_fingerprint_wrappers_keep_kernel_patch_point_and_absent_id_check(monkeypatch, exists):
    documents = {"production": [], "purchase": []}
    fingerprint = {"existing": {"doc_type": "production_order"}}
    calls = []

    def patched_fingerprint(value, verdicts=None):
        assert value is documents
        assert verdicts is None
        calls.append("fingerprint")
        return fingerprint

    class ReadOnlySession:
        def get(self, model, doc_id):
            assert model is kernel.PurchaseOrder
            assert doc_id == "new"
            calls.append("existence")
            return object() if exists else None

    session = ReadOnlySession()

    def load_documents(actual_session, order_id):
        assert actual_session is session
        assert order_id == "SO-patch"
        return documents

    monkeypatch.setattr(kernel, "_doc_fingerprint", patched_fingerprint)
    monkeypatch.setattr(kernel, "_load_documents", load_documents)
    assert kernel._doc_fingerprint_loaded(documents) is fingerprint
    assert kernel.current_doc_fingerprint(
        session,
        "SO-patch",
        {"new": {"doc_type": "purchase_order", "exists": False}},
    ) == {
        "existing": {"doc_type": "production_order"},
        "new": {"doc_type": "purchase_order", "exists": exists},
    }
    assert calls == ["fingerprint", "fingerprint", "existence"]


def test_plan_and_commit_resolve_both_original_kernel_patch_points(
    session_factory, database_url, monkeypatch
):
    from app.services.seed import load_seed

    load_seed(session_factory, database_url=database_url)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    real_payload = kernel._payload
    real_fingerprint = kernel._doc_fingerprint
    payload_calls = []
    fingerprint_calls = []

    def patched_payload(**kwargs):
        result = real_payload(**kwargs)
        payload_calls.append(copy.deepcopy(result))
        return result

    def patched_fingerprint(documents, verdicts=None):
        result = real_fingerprint(documents, verdicts)
        fingerprint_calls.append(copy.deepcopy(result))
        return result

    monkeypatch.setattr(kernel, "_payload", patched_payload)
    monkeypatch.setattr(kernel, "_doc_fingerprint", patched_fingerprint)
    plan = kernel.plan_change(
        session_factory,
        order_id="SO-1001",
        base_revision=1,
        intent_revision=1,
        changes=[
            {
                "mode": "replace_remaining",
                "from_sku": "A-BLACK-M",
                "to_sku": "A-WHITE-M",
                "qty": 40,
            }
        ],
        client_event_key="snapshot-patch-plan",
        now=now,
    )
    assert len(payload_calls) == len(fingerprint_calls) == 1
    approval = kernel.approve_plan(
        session_factory,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        expected_intent_revision=plan.intent_revision,
        now=now,
    )
    result = kernel.commit_plan(
        session_factory,
        plan_id=plan.plan_id,
        approval_id=approval.approval_id,
        plan_hash=plan.plan_hash,
        idempotency_key="snapshot-patch-commit",
        now=now,
    )
    assert result.revision == 2
    assert len(payload_calls) == len(fingerprint_calls) == 2
    assert payload_calls[0] == payload_calls[1]
    assert fingerprint_calls[0] == fingerprint_calls[1]
    assert canonical_hash(payload_calls[0]) == plan.plan_hash
