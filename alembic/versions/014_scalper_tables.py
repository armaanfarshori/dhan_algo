"""Scalper forward-paper tables — intraday options scalper log + daily governor.

The intraday long-option ladder scalper (`strategies/options_scalper.py`) is
**un-backtestable** today (no intraday option-premium series) -> it is validated by
forward paper-trading on a live feed. These two plain tables capture that track so the
dashboard's Scalper tab can surface it:

  * ``scalper_trades``   — one row per ladder tranche fill->exit (entry/exit premium,
    realized net-of-charges P&L). Newest-first list + summary feeds /api/scalper/trades.
  * ``scalper_governor`` — one row per trading day: the Daily Risk Governor's configured
    limits and the day's running totals (realized P&L, trades, profit-lock floor, state).
    Feeds /api/scalper/governor (limits vs today's totals).

Both are PLAIN tables (NOT TimescaleDB hypertables) — low volume (a few rows per day),
no time-partitioning needed. Additive: nothing else references them, so the drop in
``downgrade`` touches nothing.

Revision ID: 014
Revises: 013
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scalper_trades",
        sa.Column("id",             sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("symbol",         sa.String(50), nullable=False, server_default="NIFTY"),
        sa.Column("trade_date",     sa.Date(), nullable=False),
        # ladder / tranche context
        sa.Column("ladder_id",      sa.String(40)),     # groups tranches of one ladder
        sa.Column("direction",      sa.String(8)),       # LONG | SHORT
        sa.Column("option_type",    sa.String(4)),       # CE | PE
        sa.Column("strike",         sa.Integer()),
        sa.Column("lots",           sa.Integer(), nullable=False, server_default="0"),
        # entry
        sa.Column("entry_time",     sa.TIMESTAMP(timezone=True)),
        sa.Column("entry_premium",  sa.Numeric(14, 4)),  # rupees/unit
        sa.Column("entry_underlying", sa.Numeric(14, 4)),
        sa.Column("entry_reason",   sa.String(120)),
        # exit / resolution
        sa.Column("status",         sa.String(10), nullable=False, server_default="OPEN"),  # OPEN | CLOSED
        sa.Column("exit_time",      sa.TIMESTAMP(timezone=True)),
        sa.Column("exit_premium",   sa.Numeric(14, 4)),
        sa.Column("exit_reason",    sa.String(120)),
        # P&L (net of modeled charges)
        sa.Column("gross_pnl",      sa.Numeric(14, 4)),
        sa.Column("charges",        sa.Numeric(14, 4)),
        sa.Column("net_pnl",        sa.Numeric(14, 4)),  # net-of-charges realized
        sa.Column("win",            sa.Boolean()),
        sa.Column("created_at",     sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_scalper_trades_date", "scalper_trades", ["trade_date"])
    op.create_index("ix_scalper_trades_status", "scalper_trades", ["status"])

    op.create_table(
        "scalper_governor",
        sa.Column("id",                sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trade_date",        sa.Date(), nullable=False),
        sa.Column("symbol",            sa.String(50), nullable=False, server_default="NIFTY"),
        # configured limits (the governor's rails for the day)
        sa.Column("daily_loss_cap",    sa.Numeric(14, 2)),
        sa.Column("profit_lock_arm",   sa.Numeric(14, 2)),
        sa.Column("profit_lock_giveback", sa.Numeric(14, 2)),
        sa.Column("profit_lock_mode",  sa.String(16)),     # trail_only | stop
        sa.Column("max_trades",        sa.Integer()),
        sa.Column("consec_loss_limit", sa.Integer()),
        # today's running totals (the meter — source of truth)
        sa.Column("realized_pnl",      sa.Numeric(14, 4), server_default="0"),
        sa.Column("trades_today",      sa.Integer(), server_default="0"),
        sa.Column("consec_losses",     sa.Integer(), server_default="0"),
        sa.Column("profit_floor",      sa.Numeric(14, 4)),
        sa.Column("profit_hi_water",   sa.Numeric(14, 4)),
        sa.Column("lock_armed",        sa.Boolean(), server_default=sa.text("false")),
        sa.Column("state",             sa.String(16), server_default="ACTIVE"),  # ACTIVE|LOCKED|TRAIL_ONLY|COOLDOWN|STOOD_DOWN
        sa.Column("updated_at",        sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        # one governor row per (symbol, trade_date) — daily upsert
        sa.UniqueConstraint("symbol", "trade_date", name="uq_scalper_gov_symbol_date"),
    )
    op.create_index("ix_scalper_governor_date", "scalper_governor", ["trade_date"])


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_scalper_governor_date")
    op.execute("DROP TABLE IF EXISTS scalper_governor")
    op.execute("DROP INDEX IF EXISTS ix_scalper_trades_status")
    op.execute("DROP INDEX IF EXISTS ix_scalper_trades_date")
    op.execute("DROP TABLE IF EXISTS scalper_trades")
