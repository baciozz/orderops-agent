"""Explicit reservation lifecycle; historical usage is already settled."""

import sqlalchemy as sa
from alembic import op

revision = "budget_reservations"
down_revision = "source_stage3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("model_usage") as batch:
        batch.add_column(
            sa.Column("status", sa.String(16), nullable=False, server_default="SETTLED")
        )
        batch.add_column(sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            "ck_model_usage_status", "status IN ('RESERVED', 'SETTLED', 'ORPHANED')"
        )
    op.execute("UPDATE model_usage SET reserved_at = at, settled_at = at")


def downgrade():
    with op.batch_alter_table("model_usage") as batch:
        batch.drop_constraint("ck_model_usage_status", type_="check")
        batch.drop_column("settled_at")
        batch.drop_column("reserved_at")
        batch.drop_column("status")
