"""F&O data foundation — NIFTY index-options Phase 0 schema.

Adds the four additive tables that hold the bare-minimum F&O dataset the
options-research track needs (see docs/fno-handoff.md). DhanAIBot today holds
NSE_EQ spot bars only; there is zero F&O data. This migration closes that gap
without touching any existing table:

  • futures_bars  — NIFTY (and future) index-futures OHLCV + open interest,
                    daily by default. Carries a derived `realized_vol_20d`
                    column (20-day close-to-close vol, filled by
                    core/fno_derived.py) so the vol gate can read it directly.
  • option_atm_iv — ATM call/put IV sampled once per day per weekly expiry
                    (NOT the full chain — ~250 rows/yr). Source of the implied
                    move used to price spreads and calibrate the vol gate.
  • india_vix     — India VIX daily close/high/low (NSE public CSV, no API
                    quota). The implied-move baseline.
  • expiry_calendar — symbol → expiry_date → expiry_type, built from actual
                    contract data (NIFTY weekly expiry day changed in 2024–25,
                    so this is derived, not assumed).

futures_bars / option_atm_iv / india_vix are TimescaleDB hypertables on `time`
(matching the existing `bars`/`ticks` convention). expiry_calendar is a plain
table — it is tiny and keyed by (symbol, expiry_date). Row volumes here are
small (hundreds/yr), so no compression or retention policies are attached.

Price/strike columns use Numeric(12, 4) to match the `bars` table; derived
statistics (IV, vol, implied move) use double precision since they are
floating-point analytics, not money.

Revision ID: 009
Revises: 008
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── futures_bars — index-futures OHLCV + OI (hypertable) ───────────────────
    # `symbol` is the logical contract series (e.g. 'NIFTY' for a continuous /
    # front-month series); `expiry_date` records which physical contract the bar
    # came from. PK includes `time` (TimescaleDB requires the partition column
    # in any unique constraint).
    op.create_table(
        "futures_bars",
        sa.Column("time",             sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("symbol",           sa.String(50),  nullable=False),
        sa.Column("timeframe",        sa.String(5),   nullable=False, server_default="1d"),
        sa.Column("open",             sa.Numeric(12, 4), nullable=False),
        sa.Column("high",             sa.Numeric(12, 4), nullable=False),
        sa.Column("low",              sa.Numeric(12, 4), nullable=False),
        sa.Column("close",            sa.Numeric(12, 4), nullable=False),
        sa.Column("volume",           sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("open_interest",    sa.BigInteger()),
        sa.Column("expiry_date",      sa.Date()),
        sa.Column("realized_vol_20d", sa.Float()),   # derived (core/fno_derived.py)
        sa.PrimaryKeyConstraint("symbol", "timeframe", "time"),
    )
    op.execute("SELECT create_hypertable('futures_bars', 'time', if_not_exists => TRUE)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_futures_bars_symbol_tf_time "
        "ON futures_bars (symbol, timeframe, time DESC)"
    )

    # ── option_atm_iv — ATM straddle IV at entry, one sample/day/expiry ────────
    op.create_table(
        "option_atm_iv",
        sa.Column("time",         sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("symbol",       sa.String(50), nullable=False),
        sa.Column("expiry_date",  sa.Date(),     nullable=False),
        sa.Column("expiry_type",  sa.String(10)),                # weekly | monthly
        sa.Column("atm_strike",   sa.Numeric(12, 4)),
        sa.Column("call_iv",      sa.Float()),                   # fraction, e.g. 0.145
        sa.Column("put_iv",       sa.Float()),
        sa.Column("straddle_iv",  sa.Float()),
        sa.Column("dte",          sa.Integer()),                 # calendar days to expiry
        sa.Column("spot_ref",     sa.Numeric(12, 4)),
        sa.Column("implied_move", sa.Float()),                   # derived (core/fno_derived.py)
        sa.PrimaryKeyConstraint("symbol", "expiry_date", "time"),
    )
    op.execute("SELECT create_hypertable('option_atm_iv', 'time', if_not_exists => TRUE)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_option_atm_iv_symbol_expiry_time "
        "ON option_atm_iv (symbol, expiry_date, time DESC)"
    )

    # ── india_vix — daily India VIX (hypertable) ───────────────────────────────
    op.create_table(
        "india_vix",
        sa.Column("time",  sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("close", sa.Numeric(12, 4), nullable=False),
        sa.Column("high",  sa.Numeric(12, 4)),
        sa.Column("low",   sa.Numeric(12, 4)),
        sa.PrimaryKeyConstraint("time"),
    )
    op.execute("SELECT create_hypertable('india_vix', 'time', if_not_exists => TRUE)")

    # ── expiry_calendar — derived contract calendar (plain table) ──────────────
    op.create_table(
        "expiry_calendar",
        sa.Column("symbol",      sa.String(50), nullable=False),
        sa.Column("expiry_date", sa.Date(),     nullable=False),
        sa.Column("expiry_type", sa.String(10)),                 # weekly | monthly
        sa.PrimaryKeyConstraint("symbol", "expiry_date"),
    )


def downgrade() -> None:
    # Drop only the four tables this migration introduced.
    op.execute("DROP TABLE IF EXISTS expiry_calendar")
    op.execute("DROP TABLE IF EXISTS india_vix")
    op.execute("DROP TABLE IF EXISTS option_atm_iv")
    op.execute("DROP TABLE IF EXISTS futures_bars")
