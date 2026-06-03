"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-06-03
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")

    op.create_table(
        "instruments",
        sa.Column("security_id", sa.String(20), primary_key=True),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("name", sa.String(200)),
        sa.Column("exchange_segment", sa.String(20), nullable=False),
        sa.Column("instrument_type", sa.String(20), nullable=False, server_default="EQUITY"),
        sa.Column("lot_size", sa.Integer(), nullable=False, server_default="1"),
    )

    op.create_table(
        "ohlcv_1min",
        sa.Column("security_id", sa.String(20), nullable=False),
        sa.Column("exchange_segment", sa.String(20), nullable=False),
        sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(12, 4), nullable=False),
        sa.Column("high", sa.Numeric(12, 4), nullable=False),
        sa.Column("low", sa.Numeric(12, 4), nullable=False),
        sa.Column("close", sa.Numeric(12, 4), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("security_id", "ts"),
    )
    # Convert to TimescaleDB hypertable partitioned by time
    op.execute("SELECT create_hypertable('ohlcv_1min', 'ts', if_not_exists => TRUE)")

    op.create_table(
        "signals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("security_id", sa.String(20), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),        # BUY | SELL
        sa.Column("score", sa.Numeric(6, 4)),
        sa.Column("confidence", sa.Numeric(6, 4)),
        sa.Column("strategy", sa.String(50), nullable=False),
        sa.Column("features_snapshot", sa.JSON()),
        sa.Column("acted", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index("ix_signals_ts", "signals", ["ts"])
    op.create_index("ix_signals_security_id", "signals", ["security_id"])

    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("signal_id", sa.BigInteger(), sa.ForeignKey("signals.id"), nullable=True),
        sa.Column("security_id", sa.String(20), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("entry_ts", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("exit_ts", sa.TIMESTAMP(timezone=True)),
        sa.Column("exit_price", sa.Numeric(12, 4)),
        sa.Column("pnl", sa.Numeric(12, 4)),
        sa.Column("strategy", sa.String(50), nullable=False),
        sa.Column("dhan_order_id", sa.String(50)),
        sa.Column("status", sa.String(20), nullable=False, server_default="OPEN"),
    )
    op.create_index("ix_trades_entry_ts", "trades", ["entry_ts"])
    op.create_index("ix_trades_security_id", "trades", ["security_id"])

    op.create_table(
        "daily_pnl",
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("realized_pnl", sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("trades_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("losses", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("daily_pnl")
    op.drop_table("trades")
    op.drop_table("signals")
    op.drop_table("ohlcv_1min")
    op.drop_table("instruments")
