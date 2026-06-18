"""Add daily_screen table — per-trading-day audit log of screened stocks.

Every watchlist refresh records the day's screener candidate set: which
securities the ATR% screener ranked, their score (ATR% — the screener's
ranking metric), liquidity stats, and whether each one made the final
tradeable watchlist (`selected`).  Persisted like trades/signals so the
selection process is auditable after the fact (why was X in the universe
on day D?).

`score` is the screener's ranking metric: average daily ATR% over the
lookback window (the same float `get_top_volatile` returns as "score").

UNIQUE(trading_day, security_id) makes the per-day write idempotent — a
re-run of the same day UPDATEs the row rather than duplicating it.

Revision ID: 008
Revises: 007
"""
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE daily_screen (
            id          bigserial    PRIMARY KEY,
            trading_day date         NOT NULL,
            security_id text         NOT NULL,
            ticker      text,
            rank        integer,
            score       double precision,
            price       double precision,
            volume      bigint,
            selected    boolean      NOT NULL DEFAULT false,
            reason      text,
            created_at  timestamptz  NOT NULL DEFAULT now(),
            UNIQUE (trading_day, security_id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_daily_screen_trading_day "
        "ON daily_screen (trading_day DESC)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_daily_screen_trading_day")
    op.execute("DROP TABLE IF EXISTS daily_screen")
