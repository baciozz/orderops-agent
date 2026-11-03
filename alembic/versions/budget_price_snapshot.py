"""Snapshot reservation prices so settlement survives configuration changes.

Backfill both prices only when price_version is exactly in=X;out=Y and X/Y
are finite, nonnegative Decimals quantizable by Money to six decimal places.
Unparseable/invalid versions leave both columns NULL, meaning unknown historical
prices. Historical rows are already SETTLED; their costs/statuses stay unchanged.
"""

from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from alembic import op
from app.db.types import Money

revision = "budget_price_snapshot"
down_revision = "budget_reservations"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("model_usage") as batch:
        batch.add_column(sa.Column("price_in_per_mtok", Money(), nullable=True))
        batch.add_column(sa.Column("price_out_per_mtok", Money(), nullable=True))
    usage = sa.table(
        "model_usage",
        sa.column("id", sa.String(64)),
        sa.column("price_version", sa.String(128)),
        sa.column("price_in_per_mtok", Money()),
        sa.column("price_out_per_mtok", Money()),
    )
    connection = op.get_bind()
    for row_id, version in connection.execute(sa.select(usage.c.id, usage.c.price_version)):
        try:
            in_part, out_part = version.split(";")
            if not in_part.startswith("in=") or not out_part.startswith("out="):
                continue
            prices = [Decimal(in_part[3:]), Decimal(out_part[4:])]
            if any(not price.is_finite() or price < 0 for price in prices):
                continue
            prices = [price.quantize(Decimal("0.000001")) for price in prices]
        except (ValueError, InvalidOperation):
            continue
        connection.execute(
            usage.update()
            .where(usage.c.id == row_id)
            .values(price_in_per_mtok=prices[0], price_out_per_mtok=prices[1])
        )


def downgrade():
    with op.batch_alter_table("model_usage") as batch:
        batch.drop_column("price_out_per_mtok")
        batch.drop_column("price_in_per_mtok")
