"""Migrate signals.features_snapshot from json to jsonb + add GIN index.

Changing the column type to jsonb lets the DB index its contents and allows
operators to use the native jsonb operators (?, ->>, @>, etc.) without an
explicit ::jsonb cast on every query.

NOTE: existing queries in ml/calibration.py and the gate-panel handler in
apps/api.py currently cast features_snapshot::jsonb before using jsonb
operators.  Those casts are now redundant but harmless on an already-jsonb
column, so they are left in place to keep this change's scope minimal.

Revision ID: 006
Revises: 005
"""
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE signals "
        "ALTER COLUMN features_snapshot TYPE jsonb "
        "USING features_snapshot::jsonb"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_signals_features "
        "ON signals USING GIN (features_snapshot)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_signals_features")
    op.execute(
        "ALTER TABLE signals "
        "ALTER COLUMN features_snapshot TYPE json "
        "USING features_snapshot::json"
    )
