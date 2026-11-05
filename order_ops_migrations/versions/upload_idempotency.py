"""Optional task-scoped upload receipts; original sources stay untouched."""

import sqlalchemy as sa
from alembic import op

revision = "upload_idempotency"
down_revision = "verification_retries"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_upload_receipt",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("client_upload_key", sa.String(128), primary_key=True),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id", "source_id"],
            ["task_source.task_id", "task_source.source_id"],
            name="fk_upload_receipt_owner",
        ),
    )


def downgrade():
    # Remove retry identities only, never task ownership, evidence or spans.
    op.drop_table("source_upload_receipt")
