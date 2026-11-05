"""Durable payloads and consumer receipts."""

import sqlalchemy as sa
from alembic import op

revision = "durable_stage2"
down_revision = "3e057b273982"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "input_payload",
        sa.Column("event_id", sa.String(64), sa.ForeignKey("event.id"), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("task.id"), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
    )
    op.create_table(
        "consumer_receipt",
        sa.Column("event_id", sa.String(64), sa.ForeignKey("event.id"), primary_key=True),
        sa.Column("consumer", sa.String(64), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("consumer_receipt")
    op.drop_table("input_payload")
