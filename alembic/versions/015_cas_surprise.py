"""CAS surprise capture — one row per (trade_date, F&O stock) from the closing auction.

Research capture for the N2 "CAS Surprise Continuation" blueprint
(~/dhan_data/research/novel-strategy-blueprints.md): per stock-F&O underlying,
the 15:00-15:15 IST reference VWAP (reconstructed from 1-min bars), the CAS
closing price, and ``surprise = ln(cas_close / ref_vwap)``. Populated daily by
``scripts/cas_surprise_capture.py`` (EOD cron). PLAIN table (not a hypertable):
~190 rows/day. Additive; nothing else references it.

Revision ID: 015
Revises: 014
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cas_surprise",
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("security_id", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=True),
        # 15:00-15:15 IST pre-auction reference window, from 1-min bars.
        sa.Column("ref_vwap", sa.Numeric(12, 4), nullable=False),
        sa.Column("ref_volume", sa.BigInteger(), nullable=False),
        # Official close after the auction (= CAS equilibrium for F&O names).
        sa.Column("cas_close", sa.Numeric(12, 4), nullable=False),
        # Volume printed at/after 15:30 IST if the bars expose it (auction cross).
        sa.Column("auction_volume", sa.BigInteger(), nullable=True),
        # ln(cas_close / ref_vwap) — the N2 signal.
        sa.Column("surprise", sa.Numeric(14, 8), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("trade_date", "security_id"),
    )
    op.create_index("ix_cas_surprise_date", "cas_surprise", ["trade_date"])


def downgrade() -> None:
    op.drop_index("ix_cas_surprise_date", table_name="cas_surprise")
    op.drop_table("cas_surprise")
