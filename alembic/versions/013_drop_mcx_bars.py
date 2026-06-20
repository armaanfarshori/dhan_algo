"""Drop the unused MCX (commodities) ``mcx_bars`` hypertable.

The MCX futures-backtest experiment was abandoned (no live coupling; the
backfill/backtest/costs modules and their tests were stripped). This migration
removes the now-orphaned ``mcx_bars`` hypertable introduced by migration 012.

The drop is purely additive in reverse: no other table references ``mcx_bars``,
so removing it touches nothing else. ``downgrade`` recreates the table exactly
as 012 did (same columns, PK, hypertable, and lookup index) so the migration is
fully reversible.

Revision ID: 013
Revises: 012
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the orphaned MCX hypertable (CASCADE clears the hypertable metadata).
    op.execute("DROP TABLE IF EXISTS mcx_bars CASCADE")


def downgrade() -> None:
    # Recreate mcx_bars exactly as migration 012 did.
    op.create_table(
        "mcx_bars",
        sa.Column("time",          sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("security_id",   sa.String(20),  nullable=False),  # MCX_COMM contract id
        sa.Column("symbol",        sa.String(50)),                    # logical series (CRUDEOILM, GOLDM, …)
        sa.Column("expiry_date",   sa.Date()),                        # physical near-month contract expiry
        sa.Column("timeframe",     sa.String(5),   nullable=False, server_default="1d"),
        sa.Column("open",          sa.Numeric(14, 4), nullable=False),
        sa.Column("high",          sa.Numeric(14, 4), nullable=False),
        sa.Column("low",           sa.Numeric(14, 4), nullable=False),
        sa.Column("close",         sa.Numeric(14, 4), nullable=False),
        sa.Column("volume",        sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("open_interest", sa.BigInteger()),
        sa.PrimaryKeyConstraint("security_id", "timeframe", "time"),
    )
    op.execute("SELECT create_hypertable('mcx_bars', 'time', if_not_exists => TRUE)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_mcx_bars_symbol_tf_time "
        "ON mcx_bars (symbol, timeframe, time DESC)"
    )
