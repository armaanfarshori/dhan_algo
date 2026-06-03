"""full schema — ticks, bars, orders, fills, positions, equity_curve, journal, runs

Revision ID: 002
Revises: 001
Create Date: 2026-06-03
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Extend instruments with fields from the plan ──────────────────────────
    op.add_column("instruments", sa.Column("isin", sa.String(12)))
    op.add_column("instruments", sa.Column("tick_size", sa.Numeric(10, 4)))
    op.add_column("instruments", sa.Column("symbol", sa.String(50)))

    # ── ticks — raw WebSocket L1 feed (hypertable) ────────────────────────────
    op.create_table(
        "ticks",
        sa.Column("time",        sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("security_id", sa.String(20),                nullable=False),
        sa.Column("ltp",         sa.Numeric(12, 4),            nullable=False),
        sa.Column("ltq",         sa.Integer()),
        sa.Column("bid",         sa.Numeric(12, 4)),
        sa.Column("ask",         sa.Numeric(12, 4)),
        sa.Column("bid_qty",     sa.Integer()),
        sa.Column("ask_qty",     sa.Integer()),
        sa.Column("volume",      sa.BigInteger()),
        sa.Column("oi",          sa.BigInteger()),
        sa.PrimaryKeyConstraint("security_id", "time"),
    )
    op.execute("SELECT create_hypertable('ticks', 'time', if_not_exists => TRUE)")
    # Compress after 7 days; purge raw ticks after 90 days
    op.execute("""
        ALTER TABLE ticks SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'security_id',
            timescaledb.compress_orderby   = 'time DESC'
        )
    """)
    op.execute("SELECT add_compression_policy('ticks', INTERVAL '7 days')")
    op.execute("SELECT add_retention_policy('ticks', INTERVAL '90 days')")

    # ── bars — multi-timeframe OHLCV (hypertable) ─────────────────────────────
    # Timeframes: '1m', '5m', '15m', '1d'
    # 1m bars are built from ticks (or from ohlcv_1min backfill data)
    op.create_table(
        "bars",
        sa.Column("time",        sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("security_id", sa.String(20),               nullable=False),
        sa.Column("timeframe",   sa.String(5),                nullable=False),
        sa.Column("open",        sa.Numeric(12, 4),           nullable=False),
        sa.Column("high",        sa.Numeric(12, 4),           nullable=False),
        sa.Column("low",         sa.Numeric(12, 4),           nullable=False),
        sa.Column("close",       sa.Numeric(12, 4),           nullable=False),
        sa.Column("volume",      sa.BigInteger(),              nullable=False),
        sa.Column("vwap",        sa.Numeric(12, 4)),
        sa.PrimaryKeyConstraint("security_id", "timeframe", "time"),
    )
    op.execute("SELECT create_hypertable('bars', 'time', if_not_exists => TRUE)")
    op.execute("""
        ALTER TABLE bars SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'security_id, timeframe',
            timescaledb.compress_orderby   = 'time DESC'
        )
    """)
    op.execute("SELECT add_compression_policy('bars', INTERVAL '30 days')")
    op.create_index("ix_bars_security_timeframe_time", "bars",
                    ["security_id", "timeframe", "time"])

    # Back-fill bars from existing ohlcv_1min data
    op.execute("""
        INSERT INTO bars (time, security_id, timeframe, open, high, low, close, volume)
        SELECT ts, security_id, '1m', open, high, low, close, volume
        FROM ohlcv_1min
        ON CONFLICT (security_id, timeframe, time) DO NOTHING
    """)

    # ── runs — one row per agent session ─────────────────────────────────────
    op.create_table(
        "runs",
        sa.Column("id",          sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("started_at",  sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("stopped_at",  sa.TIMESTAMP(timezone=True)),
        sa.Column("mode",        sa.String(20),  nullable=False),  # mock|readonly|live
        sa.Column("strategy",    sa.String(50)),
        sa.Column("config_hash", sa.String(64)),   # sha256 of resolved config
        sa.Column("version",     sa.String(40)),   # git commit SHA
        sa.Column("outcome",     sa.String(20)),   # running|stopped|crashed
        sa.Column("note",        sa.Text()),
    )

    # ── orders — every order sent (or simulated) ──────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id",              sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id",          sa.BigInteger(), sa.ForeignKey("runs.id")),
        sa.Column("signal_id",       sa.BigInteger(), sa.ForeignKey("signals.id")),
        sa.Column("dhan_order_id",   sa.String(50)),
        sa.Column("client_order_id", sa.String(50)),   # correlation_id we send
        sa.Column("created_at",      sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("security_id",     sa.String(20), nullable=False),
        sa.Column("exchange_segment",sa.String(20), nullable=False),
        sa.Column("side",            sa.String(4),  nullable=False),   # BUY | SELL
        sa.Column("qty",             sa.Integer(),  nullable=False),
        sa.Column("order_type",      sa.String(20), nullable=False),   # MARKET | LIMIT | SL
        sa.Column("product_type",    sa.String(20), nullable=False),   # INTRADAY | CNC
        sa.Column("price",           sa.Numeric(12, 4)),
        sa.Column("trigger_price",   sa.Numeric(12, 4)),
        sa.Column("status",          sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("mode",            sa.String(20), nullable=False),   # mock|readonly|live
        sa.Column("reject_reason",   sa.Text()),
    )
    op.create_index("ix_orders_run_id",        "orders", ["run_id"])
    op.create_index("ix_orders_dhan_order_id", "orders", ["dhan_order_id"])
    op.create_index("ix_orders_security_id",   "orders", ["security_id"])

    # ── order_events — append-only lifecycle log ──────────────────────────────
    op.create_table(
        "order_events",
        sa.Column("id",           sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("order_id",     sa.BigInteger(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("time",         sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("status",       sa.String(20), nullable=False),
        sa.Column("raw_payload",  JSONB),
    )
    op.create_index("ix_order_events_order_id", "order_events", ["order_id"])

    # ── fills — actual execution records ──────────────────────────────────────
    op.create_table(
        "fills",
        sa.Column("id",        sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("order_id",  sa.BigInteger(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("time",      sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("qty",       sa.Integer(),     nullable=False),
        sa.Column("price",     sa.Numeric(12, 4), nullable=False),
        sa.Column("fees",      sa.Numeric(10, 4), server_default="0"),
        sa.Column("taxes",     sa.Numeric(10, 4), server_default="0"),
    )
    op.create_index("ix_fills_order_id", "fills", ["order_id"])

    # ── positions — periodic snapshots (hypertable) ───────────────────────────
    op.create_table(
        "positions",
        sa.Column("time",           sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("security_id",    sa.String(20), nullable=False),
        sa.Column("qty",            sa.Integer(),  nullable=False),
        sa.Column("avg_price",      sa.Numeric(12, 4)),
        sa.Column("ltp",            sa.Numeric(12, 4)),
        sa.Column("unrealized_pnl", sa.Numeric(12, 4)),
        sa.Column("product_type",   sa.String(20)),
        sa.PrimaryKeyConstraint("security_id", "time"),
    )
    op.execute("SELECT create_hypertable('positions', 'time', if_not_exists => TRUE)")
    op.execute("""
        ALTER TABLE positions SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'security_id',
            timescaledb.compress_orderby   = 'time DESC'
        )
    """)
    op.execute("SELECT add_compression_policy('positions', INTERVAL '7 days')")

    # ── equity_curve — total account value over time (hypertable) ────────────
    op.create_table(
        "equity_curve",
        sa.Column("time",            sa.TIMESTAMP(timezone=True), nullable=False,
                  primary_key=True),
        sa.Column("cash",            sa.Numeric(14, 4), nullable=False),
        sa.Column("holdings_value",  sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("total_equity",    sa.Numeric(14, 4), nullable=False),
        sa.Column("realized_pnl",    sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl",  sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("drawdown",        sa.Numeric(10, 6)),  # fraction, e.g. -0.02 = -2%
    )
    op.execute("SELECT create_hypertable('equity_curve', 'time', if_not_exists => TRUE)")
    op.execute("""
        ALTER TABLE equity_curve SET (
            timescaledb.compress,
            timescaledb.compress_orderby = 'time DESC'
        )
    """)
    op.execute("SELECT add_compression_policy('equity_curve', INTERVAL '30 days')")

    # ── journal — human-readable + structured decision log ────────────────────
    op.create_table(
        "journal",
        sa.Column("id",          sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("time",        sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("run_id",      sa.BigInteger(), sa.ForeignKey("runs.id")),
        sa.Column("level",       sa.String(10), nullable=False, server_default="INFO"),
        # INFO | SIGNAL | ORDER | RISK | ERROR | OPERATOR
        sa.Column("category",    sa.String(20)),
        sa.Column("security_id", sa.String(20)),
        sa.Column("message",     sa.Text(), nullable=False),
        sa.Column("detail",      JSONB),
    )
    op.create_index("ix_journal_time",        "journal", ["time"])
    op.create_index("ix_journal_run_id",      "journal", ["run_id"])
    op.create_index("ix_journal_security_id", "journal", ["security_id"])


def downgrade() -> None:
    op.drop_table("journal")
    op.drop_table("equity_curve")
    op.drop_table("positions")
    op.drop_table("fills")
    op.drop_table("order_events")
    op.drop_table("orders")
    op.drop_table("runs")
    op.drop_table("bars")
    op.drop_table("ticks")
    op.drop_column("instruments", "symbol")
    op.drop_column("instruments", "tick_size")
    op.drop_column("instruments", "isin")
