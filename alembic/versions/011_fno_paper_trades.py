"""F&O forward paper-trade log — real-chain condor entries to resolve at expiry.

The EOD collector (core/fno_collector.py) now accrues the REAL per-expiry option chain. This
table records, once per weekly cycle, the gate-filtered iron-condor entry built from the actual
option LTPs in `option_chain_snapshot` (not the VIX proxy), and is resolved at expiry against the
index settlement. Over weeks this yields an honest, real-IV forward track to validate — or
refute — the historical VIX-based preliminary GO.

One condor per (symbol, expiry_date) — UNIQUE — so the daily collector enters the nearest weekly
exactly once and holds to expiry (ON CONFLICT DO NOTHING).

Additive; no hypertable (low volume). Revision ID: 011 / Revises: 010
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fno_paper_trades",
        sa.Column("id",               sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("symbol",           sa.String(50), nullable=False, server_default="NIFTY"),
        sa.Column("entry_date",       sa.Date(), nullable=False),
        sa.Column("expiry_date",      sa.Date(), nullable=False),
        sa.Column("lot",              sa.Integer(), nullable=False),
        # entry context
        sa.Column("spot_entry",       sa.Numeric(14, 4)),
        sa.Column("straddle_iv",      sa.Double()),       # REAL ATM straddle IV (fraction)
        sa.Column("realized_vol_20d", sa.Double()),
        sa.Column("gate_decision",    sa.String(16)),     # SELL_PREMIUM (only these are positions)
        sa.Column("k",                sa.Double()),        # gate threshold used
        # strikes + REAL entry premiums (per unit)
        sa.Column("short_put_k",      sa.Numeric(14, 4)),
        sa.Column("short_call_k",     sa.Numeric(14, 4)),
        sa.Column("long_put_k",       sa.Numeric(14, 4)),
        sa.Column("long_call_k",      sa.Numeric(14, 4)),
        sa.Column("short_put_prem",   sa.Numeric(14, 4)),
        sa.Column("short_call_prem",  sa.Numeric(14, 4)),
        sa.Column("long_put_prem",    sa.Numeric(14, 4)),
        sa.Column("long_call_prem",   sa.Numeric(14, 4)),
        sa.Column("credit",           sa.Numeric(14, 4)),  # per-unit points
        sa.Column("max_loss",         sa.Numeric(14, 4)),  # ₹ per lot (defined risk)
        sa.Column("entry_costs",      sa.Numeric(14, 4)),
        # resolution
        sa.Column("status",           sa.String(10), nullable=False, server_default="OPEN"),
        sa.Column("expiry_spot",      sa.Numeric(14, 4)),  # index settlement proxy (close; FSP TBD)
        sa.Column("gross_pnl",        sa.Numeric(14, 4)),
        sa.Column("exit_costs",       sa.Numeric(14, 4)),
        sa.Column("net_pnl",          sa.Numeric(14, 4)),
        sa.Column("win",              sa.Boolean()),
        sa.Column("resolved_at",      sa.TIMESTAMP(timezone=True)),
        sa.Column("raw",              JSONB),
        sa.Column("created_at",       sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("symbol", "expiry_date", name="uq_fno_paper_symbol_expiry"),
    )
    op.create_index("ix_fno_paper_status", "fno_paper_trades", ["status"])


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_fno_paper_status")
    op.execute("DROP TABLE IF EXISTS fno_paper_trades")
