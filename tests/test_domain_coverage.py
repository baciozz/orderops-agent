"""Behavior at quantity, execution and downstream document boundaries."""

from typing import Any

import pytest
from app.domain.changes import SnapLine, business_error, evaluate_changes
from app.domain.downstream import assess_downstream, locked_preview
from app.domain.orders import derive_order_status, unexecuted_qty
from app.errors import AppError


@pytest.fixture()
def catalog():
    return {"A-BLACK-M": "STOCK", "A-BLACK-L": "STOCK", "C-BLACK-M": "MTO"}


def evaluate(catalog, changes, lines=None):
    return evaluate_changes(
        lines=lines if lines is not None else {"A-BLACK-M": SnapLine("A-BLACK-M", 10, "STOCK", 3)},
        changes=changes,
        stock_available={sku: 100 for sku in catalog},
        known_skus=catalog,
        candidates_for={"A-BLACK-XX": ["A-BLACK-M", "A-BLACK-L"]},
    )


@pytest.mark.parametrize("qty", [True, False, "2", 1.5, None])
def test_non_integer_quantity_is_a_business_error(catalog, qty):
    result = evaluate(catalog, [{"mode": "set_total", "sku_id": "A-BLACK-M", "qty": qty}])
    error = business_error(result)
    assert isinstance(error, AppError)
    assert error.code == "BUSINESS_CONFLICT"
    assert error.http_status == 422
    assert error.details == {"issues": ["数量必须是整数"]}


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "unknown", "qty": 1},
        {"mode": "replace_remaining", "from_sku": "A-BLACK-M", "qty": 0},
        {"mode": "set_total", "sku_id": "A-BLACK-M", "qty": -1},
        {"mode": "set_total", "sku_id": "A-BLACK-M", "qty": 2},
        {"mode": "increment", "sku_id": "A-BLACK-M", "qty": 0},
        {"mode": "increment", "sku_id": "A-BLACK-M", "qty": -2},
        {"mode": "cancel_remaining", "sku_id": "missing"},
        {"mode": "replace_remaining", "from_sku": "A-BLACK-M", "to_sku": "C-BLACK-M", "qty": 1},
    ],
)
def test_invalid_changes_are_rejected_without_changing_the_order(catalog, change):
    lines = {"A-BLACK-M": SnapLine("A-BLACK-M", 10, "STOCK", 3)}
    result = evaluate(catalog, [change], lines)
    assert (result.kind, result.code) == ("reject", "BUSINESS_CONFLICT")
    assert result.issues == [result.message]
    assert lines["A-BLACK-M"].ordered_qty == 10


@pytest.mark.parametrize("mode", ["set_total", "increment", "replace_remaining"])
@pytest.mark.parametrize("sku", ["A-BLACK-XX", "A-PINK-M", ""])
def test_unknown_size_color_or_empty_sku_requires_clarification(catalog, mode, sku):
    result = evaluate(
        catalog, [{"mode": mode, "sku_id": sku, "from_sku": "A-BLACK-M", "to_sku": sku, "qty": 1}]
    )
    assert result.kind == "clarify"
    assert result.candidates == (["A-BLACK-M", "A-BLACK-L"] if sku == "A-BLACK-XX" else [])
    assert result.questions == [result.message]
    assert business_error(result) is None


def test_replacing_a_line_missing_from_order_asks_for_candidates(catalog):
    result = evaluate(
        catalog,
        [{"mode": "replace_remaining", "from_sku": "A-BLACK-XX", "to_sku": "A-BLACK-L", "qty": 1}],
    )
    assert result.kind == "clarify"
    assert result.candidates == ["A-BLACK-M", "A-BLACK-L"]
    assert "订单上没有" in result.message


def test_multiple_changes_merge_target_and_release_only_unshipped_reservations(catalog):
    lines = {"A-BLACK-M": SnapLine("A-BLACK-M", 10, "STOCK", 3)}
    result = evaluate(
        catalog,
        [
            {"mode": "increment", "sku_id": "A-BLACK-L", "qty": 2},
            {"mode": "replace_remaining", "from_sku": "A-BLACK-M", "to_sku": "A-BLACK-L", "qty": 4},
            {"mode": "cancel_remaining", "sku_id": "A-BLACK-M"},
        ],
        lines,
    )
    assert result.kind == "validated"
    assert result.lines == {
        "A-BLACK-M": SnapLine("A-BLACK-M", 3, "STOCK", 3),
        "A-BLACK-L": SnapLine("A-BLACK-L", 6, "STOCK"),
    }
    assert result.reservation_delta == {"A-BLACK-M": -7, "A-BLACK-L": 6}
    assert lines["A-BLACK-M"].ordered_qty == 10


@pytest.mark.parametrize("mode", ["set_total", "cancel_remaining"])
def test_removing_an_unexecuted_line_releases_its_reservation(catalog, mode):
    result = evaluate(
        catalog,
        [{"mode": mode, "sku_id": "A-BLACK-L", "qty": 0}],
        {"A-BLACK-L": SnapLine("A-BLACK-L", 5, "STOCK")},
    )
    assert result.kind == "validated"
    assert result.lines == {}
    assert result.reservation_delta == {"A-BLACK-L": -5}


@pytest.mark.parametrize(
    "include_executed,kind,code",
    [(False, "clarify", "LOCKED_PORTION"), (True, "reject", "EXECUTED_PORTION_IMMUTABLE")],
)
def test_mto_replacement_cannot_move_cut_quantity(catalog, include_executed, kind, code):
    result = evaluate(
        catalog,
        [
            {
                "mode": "replace_remaining",
                "from_sku": "C-BLACK-M",
                "to_sku": "C-BLACK-M",
                "qty": 8,
                "include_executed": include_executed,
            }
        ],
        {"C-BLACK-M": SnapLine("C-BLACK-M", 10, "MTO", cut_qty=3)},
    )
    assert (result.kind, result.code) == (kind, code)
    assert "7" in result.message


def test_unknown_fulfillment_mode_is_rejected():
    with pytest.raises(ValueError, match="未知履约方式"):
        unexecuted_qty(fulfillment_mode="UNKNOWN", ordered_qty=10)


@pytest.mark.parametrize(
    "lines,shipped,status",
    [
        ([], {}, "CANCELLED_UNSHIPPED"),
        ([("A", 0, "STOCK")], {}, "CANCELLED_UNSHIPPED"),
        ([("A", 3, "STOCK")], {"A": 3}, "FULFILLED"),
        ([("C", 1, "MTO")], {}, "OPEN"),
        ([], {"A": 3}, "FULFILLED"),
    ],
)
def test_order_status_preserves_shipment_facts(lines, shipped, status):
    assert derive_order_status(lines=lines, shipped_by_sku=shipped) == status


def mo(sku="A", planned=10, cut=0, status="NOT_STARTED", doc_id="MO") -> dict[str, Any]:
    return dict(doc_id=doc_id, sku_id=sku, planned_qty=planned, cut_qty=cut, status=status)


def po(
    received=0, material="FAB-A", ordered=20, status="OPEN", doc_id="PO", production_order_id="MO"
) -> dict[str, Any]:
    return dict(
        doc_id=doc_id,
        production_order_id=production_order_id,
        material_id=material,
        ordered_qty=ordered,
        received_qty=received,
        status=status,
        change_state="CONFIRMED",
        order_line_sku="A",
    )


def assess(lines, production, purchase=None, usage=None):
    return assess_downstream(
        new_lines={sku: SnapLine(sku, qty, "MTO") for sku, qty in lines.items()},
        production=production,
        purchase=purchase or [],
        usage=usage if usage is not None else {"A": ("FAB-A", 2), "B": ("FAB-B", 3)},
    )


@pytest.mark.parametrize(
    "qty,planned,status", [(3, 3, "CUT"), (6, 6, "PARTIALLY_CUT"), (10, 10, "PARTIALLY_CUT")]
)
def test_partially_cut_work_order_keeps_cut_units_and_resizes_remainder(qty, planned, status):
    verdicts, error = assess({"A": qty}, [mo(cut=3, status="PARTIALLY_CUT")])
    assert error is None
    assert verdicts[0]["after"] == dict(sku_id="A", planned_qty=planned, cut_qty=3, status=status)
    assert verdicts[0]["verdict"] == ("UNAFFECTED_RELINK" if qty == 10 else "CHANGE_DRAFT")


@pytest.mark.parametrize(
    "cut,message",
    [(10, "已裁剪的生产单无法保持在原 SKU 上"), (3, "已裁剪数量高于该 SKU 的新订购量")],
)
def test_demand_cannot_drop_below_cut_work_order_quantity(cut, message):
    assert assess({"A": 2}, [mo(cut=cut)]) == ([], message)


@pytest.mark.parametrize(
    "demand,target,qty",
    [({"B": 10}, "B", 10), ({"A": 4, "B": 6}, None, None), ({"A": 4}, "A", 4), ({"B": 4}, "B", 4)],
)
def test_uncut_work_order_moves_or_shrinks_to_cover_new_demand(demand, target, qty):
    verdicts, error = assess(demand, [mo()])
    if target is None:
        assert verdicts == []
        assert error == "P0 不支持为剩余数量追加生产单"
    else:
        assert error is None
        assert verdicts[0]["after"] == dict(
            sku_id=target, planned_qty=qty, cut_qty=0, status="NOT_STARTED"
        )
        assert verdicts[0]["order_line_sku"] == target


def test_cancelling_unstarted_production_also_cancels_unreceived_purchase():
    verdicts, error = assess({}, [mo()], [po()])
    assert error is None
    assert [v["verdict"] for v in verdicts] == ["CANCEL_DRAFT", "CANCEL_DRAFT"]
    assert verdicts[0]["after"]["planned_qty"] == 0
    assert verdicts[1]["after"] == dict(
        material_id="FAB-A",
        ordered_qty=20,
        received_qty=0,
        status="CANCELLED",
        change_state="PENDING_SUPPLIER",
    )


@pytest.mark.parametrize(
    "demand,production,purchase,usage,message",
    [
        ({}, [mo()], [po(received=1)], None, "PO 已到料，不能取消"),
        ({"B": 10}, [mo()], [po(received=1)], None, "PO 已到料部分不能改面料或减量"),
        ({"A": 3}, [mo()], [po(received=8)], None, "PO 已到料部分不能改面料或减量"),
        ({"A": 10}, [mo()], [po()], {}, "A 没有面料单耗"),
        ({}, [], [po()], None, "采购单 PO 没有对应的生产单"),
        ({"A": 11}, [mo()], [], None, "P0 不支持为剩余数量追加生产单"),
    ],
)
def test_downstream_refuses_changes_that_cannot_preserve_execution(
    demand, production, purchase, usage, message
):
    assert assess(demand, production, purchase, usage) == ([], message)


def test_purchase_material_and_quantity_are_recomputed_for_replacement_sku():
    verdicts, error = assess({"B": 10}, [mo()], [po()])
    assert error is None
    assert verdicts[1]["verdict"] == "CHANGE_DRAFT"
    assert verdicts[1]["order_line_sku"] == "B"
    assert verdicts[1]["after"] == dict(
        material_id="FAB-B",
        ordered_qty=30,
        received_qty=0,
        status="OPEN",
        change_state="PENDING_SUPPLIER",
    )


def test_purchase_reduction_preserves_received_fabric_when_still_sufficient():
    verdicts, error = assess({"A": 6}, [mo()], [po(received=8)])
    assert error is None
    assert verdicts[1]["after"]["ordered_qty"] == 12
    assert verdicts[1]["after"]["received_qty"] == 8
    assert verdicts[1]["after"]["change_state"] == "PENDING_SUPPLIER"


def test_cancelled_documents_are_ignored_and_unchanged_purchase_is_relinked():
    verdicts, error = assess(
        {"A": 10},
        [mo(), mo(status="CANCELLED", doc_id="OLD")],
        [po(), po(status="CANCELLED", doc_id="OLD-PO")],
    )
    assert error is None
    assert [v["doc_id"] for v in verdicts] == ["MO", "PO"]
    assert all(v["verdict"] == "UNAFFECTED_RELINK" and v["before"] == v["after"] for v in verdicts)


def test_locked_preview_excludes_cancelled_documents_and_locks_executed_links():
    preview = locked_preview(
        [mo(), mo(cut=3, doc_id="CUT"), mo(status="CANCELLED", doc_id="OLD")],
        [
            po(),
            po(received=20, doc_id="FULL"),
            po(production_order_id="CUT", doc_id="LINK"),
            po(status="CANCELLED", doc_id="OLD-PO"),
        ],
    )
    assert {v["doc_id"]: v["verdict"] for v in preview} == {
        "MO": "CHANGEABLE",
        "CUT": "EXECUTED_LOCKED",
        "PO": "CHANGEABLE",
        "FULL": "EXECUTED_LOCKED",
        "LINK": "EXECUTED_LOCKED",
    }


def test_fully_cut_production_remains_linked_to_original_sku():
    verdicts, error = assess({"A": 10}, [mo(cut=10, status="CUT")])
    assert error is None
    assert verdicts[0]["verdict"] == "UNAFFECTED_RELINK"
    assert verdicts[0]["after"] == dict(sku_id="A", planned_qty=10, cut_qty=10, status="CUT")


def test_replacing_all_unexecuted_units_removes_source_line(catalog):
    result = evaluate(
        catalog,
        [{"mode": "replace_remaining", "from_sku": "A-BLACK-M", "to_sku": "A-BLACK-L", "qty": 10}],
        {"A-BLACK-M": SnapLine("A-BLACK-M", 10, "STOCK")},
    )
    assert result.kind == "validated"
    assert result.lines == {"A-BLACK-L": SnapLine("A-BLACK-L", 10, "STOCK")}
    assert result.reservation_delta == {"A-BLACK-M": -10, "A-BLACK-L": 10}


def test_mto_total_cannot_be_reduced_below_cut_quantity(catalog):
    result = evaluate(
        catalog,
        [{"mode": "set_total", "sku_id": "C-BLACK-M", "qty": 2}],
        {"C-BLACK-M": SnapLine("C-BLACK-M", 10, "MTO", cut_qty=3)},
    )
    assert (result.kind, result.code) == ("reject", "INVARIANT_6")
    assert "已裁剪 3" in result.message


def test_insufficient_stock_reports_all_shortages_and_preserves_input(catalog):
    lines = {"A-BLACK-M": SnapLine("A-BLACK-M", 10, "STOCK", 3)}
    result = evaluate_changes(
        lines=lines,
        changes=[
            {"mode": "increment", "sku_id": "A-BLACK-M", "qty": 5},
            {"mode": "increment", "sku_id": "A-BLACK-L", "qty": 4},
        ],
        stock_available={"A-BLACK-M": 2},
        known_skus=catalog,
        candidates_for={},
    )
    assert (result.kind, result.code) == ("shortage", "SHORTAGE")
    assert result.shortages == {"A-BLACK-M": 3, "A-BLACK-L": 4}
    assert result.reservation_delta == {"A-BLACK-M": 5, "A-BLACK-L": 4}
    assert lines == {"A-BLACK-M": SnapLine("A-BLACK-M", 10, "STOCK", 3)}
    assert business_error(result) is None


@pytest.mark.parametrize("mode", ["set_total", "increment"])
def test_catalog_mode_mismatch_cannot_change_existing_fulfillment(catalog, mode):
    result = evaluate(
        catalog,
        [{"mode": mode, "sku_id": "A-BLACK-M", "qty": 12}],
        {"A-BLACK-M": SnapLine("A-BLACK-M", 10, "MTO", cut_qty=3)},
    )
    assert (result.kind, result.code) == ("reject", "BUSINESS_CONFLICT")
    assert result.message == "不能改变履约方式"


def test_setting_total_retains_shipped_units_and_reserves_only_added_stock(catalog):
    result = evaluate(catalog, [{"mode": "set_total", "sku_id": "A-BLACK-M", "qty": 12}])
    assert result.kind == "validated"
    assert result.lines == {"A-BLACK-M": SnapLine("A-BLACK-M", 12, "STOCK", 3)}
    assert result.reservation_delta == {"A-BLACK-M": 2}
