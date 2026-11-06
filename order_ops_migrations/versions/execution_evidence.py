"""Retain approved execution fields; legacy operations are deliberately not backfilled."""

import sqlalchemy as sa
from alembic import op

revision = "execution_evidence"
down_revision = "task_runtime_mode"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "execution_evidence",
        sa.Column("operation_id", sa.String(64), sa.ForeignKey("operation.id"), primary_key=True),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(80), nullable=False),
    )


def downgrade():
    # Dropping evidence after new business writes is not a safe rollback. Keep
    # the database and its receipt pointers intact for an explicit recovery plan.
    if op.get_bind().execute(sa.text("SELECT 1 FROM execution_evidence LIMIT 1")).first():
        raise RuntimeError("执行证据非空，拒绝删除；请停写并保留数据库，使用兼容版本恢复")
    op.drop_table("execution_evidence")
