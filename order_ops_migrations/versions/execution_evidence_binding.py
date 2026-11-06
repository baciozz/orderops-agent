"""Keep evidence metadata separate from the established business receipt shape."""

import sqlalchemy as sa
from alembic import op

revision = "execution_evidence_binding"
down_revision = "execution_evidence"
branch_labels = None
depends_on = None


def upgrade():
    # No rewrite/backfill. The short-lived draft's JSON binding remains readable
    # as its original protocol; old operations without evidence stay unknown.
    op.add_column("operation", sa.Column("execution_evidence_version", sa.Integer(), nullable=True))
    op.add_column("operation", sa.Column("execution_evidence_digest", sa.String(80), nullable=True))


def downgrade():
    # Alembic may complete this revision before attempting an earlier downgrade.
    # Refuse here as well, so an older JSON-bound receipt cannot leave the DB
    # partly downgraded when the preceding evidence-table guard then refuses.
    if op.get_bind().execute(sa.text("SELECT 1 FROM execution_evidence LIMIT 1")).first():
        raise RuntimeError("执行证据非空，拒绝降级；请停写并使用兼容版本恢复")
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM operation WHERE execution_evidence_version IS NOT NULL "
                "OR execution_evidence_digest IS NOT NULL LIMIT 1"
            )
        )
        .first()
    ):
        raise RuntimeError("执行证据绑定非空，拒绝删除；请停写并使用兼容版本恢复")
    op.drop_column("operation", "execution_evidence_digest")
    op.drop_column("operation", "execution_evidence_version")
