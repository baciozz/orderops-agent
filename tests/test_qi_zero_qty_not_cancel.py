"""Qi confirmed that zero quantities must direct users to explicit cancellation."""

from copy import deepcopy

import pytest
from app.db.models import OrderLine
from app.domain.changes import SnapLine, business_error, evaluate_changes
from app.errors import AppError
from app.services.kernel import plan_new_order
from app.services.seed import load_seed
from sqlalchemy import select


@pytest.mark.parametrize("mode", ["replace_remaining", "increment"])
def test_zero_quantity_rejection_explains_explicit_cancel_flow(mode):
    lines = {"A-BLACK-M": SnapLine("A-BLACK-M", 10, "STOCK", 3)}
    before = deepcopy(lines)
    result = evaluate_changes(
        lines=lines,
        changes=[
            {
                "mode": mode,
                "sku_id": "A-BLACK-M",
                "from_sku": "A-BLACK-M",
                "to_sku": "A-BLACK-L",
                "qty": 0,
            }
        ],
        stock_available={"A-BLACK-M": 100, "A-BLACK-L": 100},
        known_skus={"A-BLACK-M": "STOCK", "A-BLACK-L": "STOCK"},
        candidates_for={},
    )
    assert lines == before
    assert (result.kind, result.code) == ("reject", "BUSINESS_CONFLICT")
    error = business_error(result)
    assert isinstance(error, AppError)
    assert error.code == "BUSINESS_CONFLICT"
    assert error.http_status == 422
    assert error.message == result.message
    assert error.details == {"issues": [result.message]}
    assert result.message is not None
    assert "不表示取消" in result.message
    assert "cancel_remaining" in result.message


def test_new_order_zero_quantity_explains_explicit_cancel_flow(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    statement = select(
        OrderLine.revision_id,
        OrderLine.sku_id,
        OrderLine.ordered_qty,
        OrderLine.fulfillment_mode,
    ).order_by(OrderLine.revision_id, OrderLine.sku_id)
    with session_factory() as session:
        before = session.execute(statement).all()
    lines: list[dict[str, object]] = [{"sku_id": "A-BLACK-M", "qty": 0}]
    input_before = deepcopy(lines)
    with pytest.raises(AppError) as caught:
        plan_new_order(
            session_factory,
            lines=lines,
            customer_code="HD",
            client_event_key="qi-new-zero",
        )
    with session_factory() as session:
        assert session.execute(statement).all() == before
    assert lines == input_before
    assert caught.value.code == "BUSINESS_CONFLICT"
    assert caught.value.http_status == 422
    assert "不表示取消" in caught.value.message
    assert "cancel_remaining" in caught.value.message


def test_negative_increment_keeps_existing_message_without_cancel_hint():
    lines = {"A-BLACK-M": SnapLine("A-BLACK-M", 10, "STOCK", 3)}
    before = deepcopy(lines)
    result = evaluate_changes(
        lines=lines,
        changes=[{"mode": "increment", "sku_id": "A-BLACK-M", "qty": -2}],
        stock_available={"A-BLACK-M": 100},
        known_skus={"A-BLACK-M": "STOCK"},
        candidates_for={},
    )
    assert lines == before
    assert (result.kind, result.code) == ("reject", "BUSINESS_CONFLICT")
    assert result.message == "增加的数量必须是正整数"
    assert "取消" not in result.message
    assert "cancel_remaining" not in result.message
