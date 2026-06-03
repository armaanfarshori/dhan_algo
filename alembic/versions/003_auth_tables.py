"""auth tables — users, sessions, mfa_credentials, recovery_codes, auth_events

Revision ID: 003
Revises: 002
Create Date: 2026-06-03
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── users ─────────────────────────────────────────────────────────────────
    # password_hash = Argon2id output; never store plaintext.
    # role: 'viewer' (read-only dashboard) | 'operator' (full control)
    op.create_table(
        "users",
        sa.Column("id",            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username",      sa.String(50),  nullable=False, unique=True),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("role",          sa.String(20),  nullable=False, server_default="viewer"),
        sa.Column("status",        sa.String(20),  nullable=False, server_default="active"),
        # active | locked | disabled
        sa.Column("failed_attempts",   sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until",      sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at",        sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_login",        sa.TIMESTAMP(timezone=True)),
        sa.Column("last_login_ip",     sa.String(45)),
    )

    # ── mfa_credentials — WebAuthn passkeys + TOTP backup ────────────────────
    # secrets (totp_secret, webauthn_credential) are encrypted at rest
    # before insert (app-level AES-256-GCM, key in SSM Parameter Store).
    op.create_table(
        "mfa_credentials",
        sa.Column("id",                  sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id",             sa.Integer(), sa.ForeignKey("users.id",
                  ondelete="CASCADE"), nullable=False),
        sa.Column("type",                sa.String(20), nullable=False),
        # 'webauthn' | 'totp'
        sa.Column("credential_id",       sa.Text()),       # WebAuthn credential ID (base64url)
        sa.Column("public_key",          sa.Text()),       # WebAuthn CBOR public key (base64url)
        sa.Column("sign_count",          sa.BigInteger(), server_default="0"),
        sa.Column("totp_secret_enc",     sa.Text()),       # encrypted TOTP secret
        sa.Column("name",                sa.String(100)),  # user-visible label, e.g. "iPhone Face ID"
        sa.Column("created_at",          sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_used",           sa.TIMESTAMP(timezone=True)),
    )
    op.create_index("ix_mfa_user_id", "mfa_credentials", ["user_id"])

    # ── sessions — server-side session store ──────────────────────────────────
    # session_token is the cookie value (random 256-bit hex, stored hashed).
    op.create_table(
        "sessions",
        sa.Column("id",                sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id",           sa.Integer(), sa.ForeignKey("users.id",
                  ondelete="CASCADE"), nullable=False),
        sa.Column("session_token_hash",sa.String(64),  nullable=False, unique=True),
        # SHA-256 of the raw token; raw token lives only in the cookie
        sa.Column("issued_at",         sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at",        sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_seen",         sa.TIMESTAMP(timezone=True)),
        sa.Column("device_fingerprint",sa.String(200)),
        sa.Column("ip",                sa.String(45)),
        sa.Column("revoked",           sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("revoked_at",        sa.TIMESTAMP(timezone=True)),
        # elevated = step-up re-auth completed (allows dangerous actions)
        sa.Column("elevated",          sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("elevated_until",    sa.TIMESTAMP(timezone=True)),
    )
    op.create_index("ix_sessions_user_id",    "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    # ── recovery_codes — single-use, hashed ──────────────────────────────────
    op.create_table(
        "recovery_codes",
        sa.Column("id",          sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id",     sa.Integer(), sa.ForeignKey("users.id",
                  ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash",   sa.String(64), nullable=False),   # SHA-256 of raw code
        sa.Column("created_at",  sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("used_at",     sa.TIMESTAMP(timezone=True)),
        sa.Column("used",        sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index("ix_recovery_codes_user_id", "recovery_codes", ["user_id"])

    # ── auth_events — append-only audit log ───────────────────────────────────
    # Every login attempt, MFA challenge, logout, lockout, privilege change.
    # Never deleted; feed anomaly alerts (new device, off-hours login, repeat failures).
    op.create_table(
        "auth_events",
        sa.Column("id",          sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("time",        sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("user_id",     sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("username",    sa.String(50)),   # captured even if user lookup fails
        sa.Column("event_type",  sa.String(30), nullable=False),
        # login_ok | login_fail | logout | mfa_ok | mfa_fail
        # lockout | unlock | password_change | role_change
        # kill_switch_on | kill_switch_off | mode_change | step_up_ok | step_up_fail
        sa.Column("ip",          sa.String(45)),
        sa.Column("user_agent",  sa.Text()),
        sa.Column("result",      sa.String(10)),   # ok | fail | blocked
        sa.Column("detail",      JSONB),           # free-form; e.g. {"reason": "wrong_password"}
    )
    op.create_index("ix_auth_events_time",      "auth_events", ["time"])
    op.create_index("ix_auth_events_user_id",   "auth_events", ["user_id"])
    op.create_index("ix_auth_events_event_type","auth_events", ["event_type"])


def downgrade() -> None:
    op.drop_table("auth_events")
    op.drop_table("recovery_codes")
    op.drop_table("sessions")
    op.drop_table("mfa_credentials")
    op.drop_table("users")
