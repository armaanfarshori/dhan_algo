"""engine_positions — DB-persisted portfolio state (Phase 1 engine rewrite)

One row per (security_id, mode). The trader upserts on every fill and
reconciles on boot, so process restarts never orphan a position.

Revision ID: 004
Revises: 003
"""
import sqlalchemy as sa
from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "engine_positions",
        sa.Column("security_id",  sa.String(20),  nullable=False),
        sa.Column("mode",         sa.String(10),  nullable=False),   # PAPER | LIVE
        sa.Column("session_date", sa.Date(),      nullable=False),
        sa.Column("qty",          sa.Integer(),   nullable=False, server_default="0"),
        sa.Column("avg_price",    sa.Numeric(12, 4)),
        sa.Column("strategy",     sa.String(50)),
        sa.Column("updated_at",   sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("security_id", "mode"),
    )


def downgrade():
    op.drop_table("engine_positions")
