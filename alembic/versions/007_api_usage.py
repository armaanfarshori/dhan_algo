"""Add api_usage table for cross-process per-category API call accounting.

Each process (trader / backfill / api) periodically flushes the DELTA of
calls_today from its in-memory RateLimiter into this table.  The dashboard
can then show account-wide totals and per-process breakdowns without any
synchronous cross-process communication.

Revision ID: 007
Revises: 006
"""
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE api_usage (
            ts_date    date         NOT NULL,
            category   varchar(20)  NOT NULL,
            process    varchar(20)  NOT NULL,
            calls      bigint       NOT NULL DEFAULT 0,
            updated_at timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (ts_date, category, process)
        )
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS api_usage")
