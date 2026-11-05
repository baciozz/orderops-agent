"""Bound readback retries separately from already-committed business writes."""

import sqlalchemy as sa
from alembic import op

revision = "verification_retries"
down_revision = "budget_price_snapshot"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("operation") as batch:
        batch.add_column(
            sa.Column(
                "verification_retry_attempts", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch.add_column(sa.Column("verification_next_attempt_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("verification_lease_until", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("verification_lease_token", sa.String(64)))
        batch.add_column(
            sa.Column(
                "verification_manual_required", sa.Boolean(), nullable=False, server_default="0"
            )
        )
        batch.add_column(sa.Column("verification_last_error_code", sa.String(64)))


def downgrade():
    with op.batch_alter_table("operation") as batch:
        for column in (
            "verification_last_error_code",
            "verification_manual_required",
            "verification_lease_token",
            "verification_lease_until",
            "verification_next_attempt_at",
            "verification_retry_attempts",
        ):
            batch.drop_column(column)
