"""Persistent local call traces; metadata only, no prompt or secret text."""

import sqlalchemy as sa
from alembic import op

revision = "call_trace"
down_revision = "local_review_workspace"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "call_trace",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("ledger_row_id", sa.String(64), nullable=True),
        sa.Column("review_id", sa.String(64), nullable=True),
        sa.Column("task_id", sa.String(64), nullable=True),
        sa.Column("agent_id", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(128), nullable=True),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("schema_sha256", sa.String(64), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("schema_valid", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["ledger_row_id"], ["model_usage.id"]),
        sa.UniqueConstraint("ledger_row_id", name="uq_call_trace_ledger_row_id"),
        sa.CheckConstraint("elapsed_ms >= 0", name="ck_call_trace_elapsed"),
        sa.CheckConstraint("input_tokens >= 0", name="ck_call_trace_in_tok"),
        sa.CheckConstraint("output_tokens >= 0", name="ck_call_trace_out_tok"),
        sa.CheckConstraint("status IN ('ok', 'error')", name="ck_call_trace_status"),
    )
    op.create_index("ix_call_trace_started_at", "call_trace", ["started_at"])
    op.create_index("ix_call_trace_task_id", "call_trace", ["task_id"])


def downgrade():
    # Run the same preflight as earlier revisions so a refused downgrade
    # cannot first drop call_trace and stamp an older version.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM execution_evidence LIMIT 1")).first():
        raise RuntimeError("执行证据非空，拒绝降级；请停写并使用兼容版本恢复")
    if bind.execute(
        sa.text(
            "SELECT 1 FROM operation WHERE execution_evidence_version IS NOT NULL "
            "OR execution_evidence_digest IS NOT NULL LIMIT 1"
        )
    ).first():
        raise RuntimeError("执行证据绑定非空，拒绝降级；请停写并恢复兼容版本")
    if bind.execute(
        sa.text("SELECT 1 FROM operation WHERE verification_observation IS NOT NULL LIMIT 1")
    ).first():
        raise RuntimeError("核验观察记录非空，拒绝丢弃范围语义；请停写并恢复兼容版本")
    if bind.execute(sa.text("SELECT 1 FROM review_workspace LIMIT 1")).first():
        raise RuntimeError("清单工作区非空，拒绝丢失导入资料；请停写并恢复兼容版本备份")
    op.drop_index("ix_call_trace_task_id", table_name="call_trace")
    op.drop_index("ix_call_trace_started_at", table_name="call_trace")
    op.drop_table("call_trace")
