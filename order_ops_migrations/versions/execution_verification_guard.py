"""Retain explicit limited verification observations without certifying legacy rows."""

import sqlalchemy as sa
from alembic import op

revision = "execution_verification_guard"
down_revision = "execution_evidence_binding"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("operation", sa.Column("verification_observation", sa.JSON(), nullable=True))


def downgrade():
    # Reject before advancing a multi-step downgrade past the evidence guards.
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
        raise RuntimeError("执行证据绑定非空，拒绝降级；请停写并使用兼容版本恢复")
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM operation WHERE verification_observation IS NOT NULL LIMIT 1")
        )
        .first()
    ):
        raise RuntimeError("核验观察记录非空，拒绝丢弃范围语义；请停写并使用兼容版本恢复")
    op.drop_column("operation", "verification_observation")
