"""
Centralised configuration — single typed Settings object for the platform.

Every module reads configuration through get_config(); nothing else in the
codebase may call os.getenv(). Values come from the environment / .env file
and are validated at first access — a typo'd numeric env var fails loudly at
startup instead of deep inside a trading session.
"""
from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",          # .env carries vars for other tools too
        case_sensitive=False,
    )

    # ── Dhan credentials ────────────────────────────────────────────────────
    dhan_client_id: str = "mock"
    dhan_access_token: str = "mock"
    dhan_pin: str = ""
    dhan_totp_secret: str = ""

    # ── Trading mode ────────────────────────────────────────────────────────
    paper_trading: bool = True
    # Flipping to live via POST /api/mode additionally requires this flag —
    # there is no auth layer until M6, so live must never be one request away.
    allow_live_toggle: bool = False
    strategy: str = "orb"

    # ── Risk ────────────────────────────────────────────────────────────────
    max_daily_loss: float = 5_000.0
    capital: float = 100_000.0
    risk_per_trade: float = 0.01          # fraction of equity risked per trade
    paper_balance: float = 500_000.0
    max_orders_per_session: int = 4
    max_open_positions: int = 10
    max_notional_per_trade: float = 100_000.0
    paper_slippage_bps: float = 2.0       # adverse slippage on simulated fills

    # ── ORB strategy ────────────────────────────────────────────────────────
    orb_range_minutes: int = 15
    poll_interval: float = 20.0           # seconds between quote polls
    trade_quantity: int = 1

    # ── Watchlist — dynamic from screener; only segment is fixed ────────────
    watchlist_exchange_segment: str = "NSE_EQ"
    watchlist_n: int = 5

    # ── Kronos ──────────────────────────────────────────────────────────────
    kronos_tokenizer: str = "NeoQuasar/Kronos-Tokenizer-base"
    kronos_model: str = "NeoQuasar/Kronos-small"
    kronos_checkpoint: str = ""           # S3 path after fine-tuning; empty = HF zero-shot
    kronos_lookback: int = 400
    kronos_pred_len: int = 30
    kronos_samples: int = 5
    kronos_thresh: float = 0.001
    kronos_min_confidence: float = 0.4
    kronos_scanner_enabled: bool = True
    # Shadow mode: the gate scores and PERSISTS every decision but never
    # blocks a trade. Stays on until calibration shows the gate adds value
    # (all pre-2026-06-11 decisions were scored on stale data — worthless).
    kronos_shadow_mode: bool = True

    # ── Web / dashboard ─────────────────────────────────────────────────────
    webhook_port: int = 8765

    # ── TimescaleDB ─────────────────────────────────────────────────────────
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "dhan_trading"
    db_user: str = "trader"
    db_password: str = "trader123"

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # Back-compat alias: older modules referenced cfg.totp_secret
    @property
    def totp_secret(self) -> str:
        return self.dhan_totp_secret


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
