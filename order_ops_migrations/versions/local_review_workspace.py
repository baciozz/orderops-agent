"""Keep imported review-only snapshots separate from business execution records."""

import sqlalchemy as sa
from alembic import op

revision = "local_review_workspace"
down_revision = "execution_verification_guard"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "review_workspace",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("request_key", sa.String(100), nullable=False, unique=True),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_review_revision"),
    )


def downgrade():
    # Preflight the older evidence guards before any DDL/version transition.
    # Alembic can otherwise finish this revision before a deeper guard refuses.
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
        raise RuntimeError("执行证据绑定非空，拒绝降级；请停写并恢复兼容版本")
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM operation WHERE verification_observation IS NOT NULL LIMIT 1")
        )
        .first()
    ):
        raise RuntimeError("核验观察记录非空，拒绝丢弃范围语义；请停写并恢复兼容版本")
    if op.get_bind().execute(sa.text("SELECT 1 FROM review_workspace LIMIT 1")).first():
        raise RuntimeError("清单工作区非空，拒绝丢失导入资料；请停写并恢复兼容版本备份")
    op.drop_table("review_workspace")
