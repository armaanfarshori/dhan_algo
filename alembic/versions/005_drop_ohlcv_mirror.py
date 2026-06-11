"""Drop the ohlcv_1min legacy mirror.

It duplicated the bars hypertable's 1m rows UNCOMPRESSED — 42GB vs 6.9GB for
identical data — and every backfill upsert was executed twice. The double
write/cache pressure OOM-killed Postgres twice (2026-06-10, 2026-06-11) on
the 4GB DB instance, and the mirror's growth would have filled the 196GB
volume before the backfill completed. All readers were repointed to bars in
the same commit.

Revision ID: 005
Revises: 004
"""
from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP TABLE IF EXISTS ohlcv_1min")


def downgrade():
    # Recreates the (empty) hypertable shape only — the mirrored data lives
    # in bars and can be re-materialized from there if ever needed.
    op.execute("""
        CREATE TABLE ohlcv_1min (
            security_id      VARCHAR(20)  NOT NULL,
            exchange_segment VARCHAR(20)  NOT NULL,
            ts               TIMESTAMPTZ  NOT NULL,
            open  NUMERIC(12,4), high NUMERIC(12,4),
            low   NUMERIC(12,4), close NUMERIC(12,4),
            volume BIGINT,
            PRIMARY KEY (security_id, ts)
        )
    """)
    op.execute("SELECT create_hypertable('ohlcv_1min', 'ts', if_not_exists => TRUE)")
