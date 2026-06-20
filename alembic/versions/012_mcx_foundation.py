"""MCX (commodities) data foundation — FUTCOM near-month bars.

Adds a single additive TimescaleDB hypertable, ``mcx_bars``, that holds MCX
commodity-futures OHLCV + open interest, mirroring the F&O ``index_bars`` /
``futures_bars`` Phase-0 pattern (Alembic 009/010). DhanAIBot today holds zero
MCX data; this migration closes that gap WITHOUT touching any existing table.

  • mcx_bars — MCX_COMM / FUTCOM futures OHLCV + open interest, daily by default
               (intraday also supported via ``timeframe``). ``symbol`` is the
               logical commodity series (e.g. 'CRUDEOILM', 'GOLDM',
               'NATURALGAS'); ``security_id`` is the physical near-month
               contract id from the scrip master; ``expiry_date`` records which
               physical contract the bar came from.

Like ``index_bars``, ``mcx_bars`` is a TimescaleDB hypertable on ``time`` and
its PK/index mirror ``index_bars`` exactly: PK (security_id, timeframe, time)
and a descending (symbol, timeframe, time) lookup index. Numeric(14, 4) for
prices matches the F&O tables (gold/silver run to lakhs/kg).

Continuous-contract stitching across monthly rolls is deliberately NOT solved
here — the backfill (core/mcx_backfill.py) writes the NEAR-MONTH per symbol and
documents the continuous-stitching question as a TODO, exactly as F&O Open Q#2
(docs/fno-handoff.md) does for index futures. The schema is roll-agnostic:
store raw per-contract under distinct ``security_id`` values; build a stitched
series later as a derived layer.

Revision ID: 012
Revises: 011
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── mcx_bars — MCX_COMM / FUTCOM futures OHLCV + OI (hypertable) ────────────
    # PK includes `time` (TimescaleDB requires the partition column in any unique
    # constraint). Mirrors index_bars: (security_id, timeframe, time).
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


def downgrade() -> None:
    # Drop only the table this migration introduced.
    op.execute("DROP TABLE IF EXISTS mcx_bars CASCADE")
