"""Approval must bind the customer of a newly created order."""

from datetime import UTC, datetime

import pytest
from app.db.models import Customer, Plan, SalesOrder
from app.errors import AppError
from app.services.kernel import approve_plan, commit_plan, plan_new_order
from app.services.seed import load_seed
from sqlalchemy import select

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture()
def kernel(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    return session_factory


def test_create_commit_rejects_customer_changed_after_approval(kernel) -> None:
    customer_id = "CUST-REVIEW-OTHER"
    with kernel() as session:
        session.add(
            Customer(
                id=customer_id,
                code="REVIEW-OTHER",
                display_name="合成审查客户",
                active=True,
            )
        )
        session.commit()
        assert (
            session.scalar(select(SalesOrder).where(SalesOrder.customer_id == customer_id)) is None
        )

    outcome = plan_new_order(
        kernel,
        customer_code="HD",
        lines=[{"sku_id": "A-WHITE-M", "qty": 1}],
        client_event_key="review-customer-hash",
        now=NOW,
    )
    assert outcome.status == "VALIDATED"
    approval = approve_plan(
        kernel,
        plan_id=outcome.plan_id,
        plan_hash=outcome.plan_hash,
        expected_intent_revision=outcome.intent_revision,
        now=NOW,
    )

    # No public API changes this field; direct database tampering tests defense in depth.
    # Reassign the whole JSON dict so SQLAlchemy persists the modified customer.
    with kernel() as session:
        plan = session.get(Plan, outcome.plan_id)
        assert plan is not None
        assert plan.normalized_changes["customer_id"] == "CUST-HD"
        plan.normalized_changes = {**plan.normalized_changes, "customer_id": customer_id}
        session.commit()
    with kernel() as session:
        plan = session.get(Plan, outcome.plan_id)
        assert plan is not None
        assert plan.normalized_changes["customer_id"] == customer_id

    with pytest.raises(AppError) as exc:
        commit_plan(
            kernel,
            plan_id=outcome.plan_id,
            approval_id=approval.approval_id,
            plan_hash=outcome.plan_hash,
            idempotency_key="review-customer-hash-commit",
            now=NOW,
        )
    assert exc.value.code == "HASH_MISMATCH"
    with kernel() as session:
        assert (
            session.scalar(select(SalesOrder).where(SalesOrder.customer_id == customer_id)) is None
        )
