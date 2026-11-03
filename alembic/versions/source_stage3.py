"""Explicit task ownership for uploaded sources."""

import sqlalchemy as sa
from alembic import op

revision = "source_stage3"
down_revision = "durable_stage2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_source",
        sa.Column("task_id", sa.String(64), sa.ForeignKey("task.id"), primary_key=True),
        sa.Column("source_id", sa.String(64), sa.ForeignKey("input_source.id"), primary_key=True),
    )


def downgrade():
    op.drop_table("task_source")
