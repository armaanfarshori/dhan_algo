"""F&O capture-everything — full instrument master, index bars, full option chain.

Revises the curated Phase-0 schema (009) per the capture-everything rule: persist the
ENTIRE payload received from each source and project only what we query. Live recon
(2026-06-19) showed the existing `instruments` table is lossy for F&O (drops
expiry/strike/option_type; truncates ticker to 20 chars) and that everything we need is
pullable via the Dhan charts/option-chain endpoints. This migration is additive on
`dhan_trading` (new tables; the only drop is `india_vix`, which is folded into the richer
`index_bars`).

Adds:
  • fno_instruments      — the FULL Dhan *detailed* scrip master (all 31 columns + a `raw`
                           JSONB of the whole row), keyed by security_id. Carries the F&O
                           fields the live `instruments` table loses (underlying, strike,
                           option_type, expiry, lot, tick, freeze qty, circuits, margins).
  • index_bars           — daily OHLCV for IDX_I index instruments (NIFTY 50 id 13, India
                           VIX id 21, …) + derived realized_vol_20d. Continuous, multi-year;
                           the realized-vol base (resolves Open Q#2 — no futures-stitching).
  • option_chain_snapshot— the FULL option chain per capture: one row per
                           (snapshot_time, underlying, expiry, strike, option_type) with
                           ltp/oi/volume/bid/ask/prev_*/IV/greeks + a `raw` JSONB of the
                           per-strike node. ATM IV is projected from this into option_atm_iv
                           (kept from 009) at capture time.

Drops:
  • india_vix            — superseded by index_bars (full OHLCV vs close/high/low only).

Revision ID: 010
Revises: 009
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── fno_instruments — full DETAILED scrip master (capture-everything) ──────
    op.create_table(
        "fno_instruments",
        sa.Column("security_id",            sa.String(20),  primary_key=True),
        sa.Column("exch_id",                sa.String(10)),
        sa.Column("segment",                sa.String(10)),
        sa.Column("isin",                   sa.String(20)),
        sa.Column("instrument",             sa.String(20)),   # FUTIDX/OPTIDX/FUTSTK/...
        sa.Column("underlying_security_id", sa.String(20)),
        sa.Column("underlying_symbol",      sa.String(50)),
        sa.Column("symbol_name",            sa.String(100)),
        sa.Column("display_name",           sa.String(100)),
        sa.Column("instrument_type",        sa.String(20)),   # FUT/OP/...
        sa.Column("series",                 sa.String(10)),
        sa.Column("lot_size",               sa.Integer()),
        sa.Column("expiry_date",            sa.Date()),
        sa.Column("strike_price",           sa.Numeric(14, 4)),
        sa.Column("option_type",            sa.String(4)),    # CE/PE/XX
        sa.Column("tick_size",              sa.Numeric(12, 4)),
        sa.Column("expiry_flag",            sa.String(4)),
        # ancillary master fields — kept as text (flags / margin params); full row in `raw`
        sa.Column("bracket_flag",           sa.String(4)),
        sa.Column("cover_flag",             sa.String(4)),
        sa.Column("asm_gsm_flag",           sa.String(4)),
        sa.Column("asm_gsm_category",       sa.String(20)),
        sa.Column("buy_sell_indicator",     sa.String(4)),
        sa.Column("mtf_leverage",           sa.Numeric(12, 4)),
        sa.Column("upper_circuit",          sa.Numeric(14, 4)),   # SM_UPPER_LIMIT
        sa.Column("lower_circuit",          sa.Numeric(14, 4)),   # SM_LOWER_LIMIT
        sa.Column("freeze_qty",             sa.BigInteger()),     # SM_FREEZE_QTY
        sa.Column("raw",                    JSONB),               # the ENTIRE master row
        sa.Column("updated_at",             sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_fno_instruments_underlying", "fno_instruments",
                    ["underlying_symbol", "instrument", "expiry_date"])
    op.create_index("ix_fno_instruments_segment", "fno_instruments", ["segment"])

    # ── index_bars — IDX_I index OHLCV (NIFTY 50, India VIX, …) (hypertable) ───
    op.create_table(
        "index_bars",
        sa.Column("time",             sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("security_id",      sa.String(20), nullable=False),  # IDX_I feed id (13, 21, …)
        sa.Column("symbol",           sa.String(50)),
        sa.Column("timeframe",        sa.String(5), nullable=False, server_default="1d"),
        sa.Column("open",             sa.Numeric(14, 4), nullable=False),
        sa.Column("high",             sa.Numeric(14, 4), nullable=False),
        sa.Column("low",              sa.Numeric(14, 4), nullable=False),
        sa.Column("close",            sa.Numeric(14, 4), nullable=False),
        sa.Column("volume",           sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("realized_vol_20d", sa.Double()),   # derived (core/fno_derived) — price indices only
        sa.PrimaryKeyConstraint("security_id", "timeframe", "time"),
    )
    op.execute("SELECT create_hypertable('index_bars', 'time', if_not_exists => TRUE)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_index_bars_sec_tf_time "
        "ON index_bars (security_id, timeframe, time DESC)"
    )

    # ── option_chain_snapshot — FULL chain per capture (hypertable) ───────────
    op.create_table(
        "option_chain_snapshot",
        sa.Column("snapshot_time",    sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("underlying_scrip", sa.Integer(),  nullable=False),  # Dhan option-chain UnderlyingScrip (13 = NIFTY, IDX_I). DISTINCT id-space from fno_instruments.underlying_security_id (e.g. 26000) — NOT join-compatible.
        sa.Column("underlying_seg",   sa.String(10), nullable=False, server_default="IDX_I"),
        sa.Column("expiry_date",      sa.Date(),     nullable=False),
        sa.Column("strike",           sa.Numeric(14, 4), nullable=False),
        sa.Column("option_type",      sa.String(4),  nullable=False),  # CE/PE
        sa.Column("security_id",      sa.String(20)),
        sa.Column("ltp",              sa.Numeric(14, 4)),
        sa.Column("prev_close",       sa.Numeric(14, 4)),
        sa.Column("volume",           sa.BigInteger()),
        sa.Column("oi",               sa.BigInteger()),
        sa.Column("prev_oi",          sa.BigInteger()),
        sa.Column("prev_volume",      sa.BigInteger()),
        sa.Column("top_bid_price",    sa.Numeric(14, 4)),
        sa.Column("top_ask_price",    sa.Numeric(14, 4)),
        sa.Column("top_bid_qty",      sa.BigInteger()),
        sa.Column("top_ask_qty",      sa.BigInteger()),
        sa.Column("iv",               sa.Double()),   # raw IV as Dhan returns (percent)
        sa.Column("delta",            sa.Double()),
        sa.Column("theta",            sa.Double()),
        sa.Column("gamma",            sa.Double()),
        sa.Column("vega",             sa.Double()),
        sa.Column("spot",             sa.Numeric(14, 4)),   # underlying last_price at snapshot
        sa.Column("raw",              JSONB),               # the per-strike CE/PE node
        sa.PrimaryKeyConstraint("snapshot_time", "underlying_scrip", "expiry_date",
                                "strike", "option_type"),
    )
    op.execute("SELECT create_hypertable('option_chain_snapshot', 'snapshot_time', if_not_exists => TRUE)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ocs_underlying_expiry_time "
        "ON option_chain_snapshot (underlying_scrip, expiry_date, snapshot_time DESC)"
    )
    op.execute(
        "COMMENT ON COLUMN option_chain_snapshot.iv IS "
        "'Implied volatility as Dhan returns it — PERCENT scale (12.0 = 12%); "
        "divide by 100 for a fraction. option_atm_iv stores the normalised fraction.'"
    )

    # ── drop india_vix (009) — folded into index_bars ─────────────────────────
    op.execute("DROP TABLE IF EXISTS india_vix CASCADE")


def downgrade() -> None:
    # Recreate india_vix exactly as 009 defined it.
    op.create_table(
        "india_vix",
        sa.Column("time",  sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("close", sa.Numeric(12, 4), nullable=False),
        sa.Column("high",  sa.Numeric(12, 4)),
        sa.Column("low",   sa.Numeric(12, 4)),
        sa.PrimaryKeyConstraint("time"),
    )
    op.execute("SELECT create_hypertable('india_vix', 'time', if_not_exists => TRUE)")
    op.execute("DROP TABLE IF EXISTS option_chain_snapshot CASCADE")
    op.execute("DROP TABLE IF EXISTS index_bars CASCADE")
    op.execute("DROP INDEX IF EXISTS ix_fno_instruments_segment")
    op.execute("DROP INDEX IF EXISTS ix_fno_instruments_underlying")
    op.execute("DROP TABLE IF EXISTS fno_instruments")
