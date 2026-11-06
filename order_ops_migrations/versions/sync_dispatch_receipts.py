"""Bind synchronous dispatch retries without persisting input text."""

import sqlalchemy as sa
from alembic import op

revision = "sync_dispatch_receipts"
down_revision = "upload_idempotency"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sync_dispatch_receipt",
        sa.Column("client_event_key", sa.String(128), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("task.id"), unique=True, nullable=False),
        sa.Column("request_hash", sa.String(80), nullable=False),
    )


def downgrade():
    op.drop_table("sync_dispatch_receipt")
