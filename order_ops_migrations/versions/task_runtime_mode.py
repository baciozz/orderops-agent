"""Pin task execution mode; legacy tasks never acquire LIVE authorization."""

import sqlalchemy as sa
from alembic import op

revision = "task_runtime_mode"
down_revision = "sync_dispatch_receipts"
branch_labels = None
depends_on = None


def upgrade():
    # Inline CHECK permits SQLite ADD COLUMN without rebuilding the heavily
    # referenced task table. Existing rows receive FIXTURE, regardless of history.
    op.add_column(
        "task",
        sa.Column(
            "runtime_mode",
            sa.String(16),
            sa.CheckConstraint("runtime_mode IN ('FIXTURE', 'LIVE')", name="ck_task_runtime_mode"),
            nullable=False,
            server_default="FIXTURE",
        ),
    )
    op.execute(
        "CREATE TRIGGER task_runtime_mode_immutable BEFORE UPDATE OF runtime_mode ON task "
        "WHEN NEW.runtime_mode IS NOT OLD.runtime_mode "
        "BEGIN SELECT RAISE(ABORT, 'task runtime mode is immutable'); END"
    )


def downgrade():
    op.execute("DROP TRIGGER task_runtime_mode_immutable")
    op.drop_column("task", "runtime_mode")
