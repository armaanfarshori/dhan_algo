"""
Centralised configuration — single typed Settings object for the platform.

Every module reads configuration through get_config(); nothing else in the
codebase may call os.getenv(). Values come from the environment / .env file
and are validated at first access — a typo'd numeric env var fails loudly at
startup instead of deep inside a trading session.
"""
import logging
from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_cfg_logger = logging.getLogger("dhan.config")


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

    # ── Risk — ALL limits are fractions of equity, never absolute rupees ─────
    # (so paper rehearses the same geometry live will use; reworked 2026-06-13)
    capital: float = 100_000.0            # live starting capital
    paper_balance: float = 500_000.0      # paper starting equity
    risk_per_trade: float = 0.005         # 0.5% of equity at risk per trade
    max_daily_loss_pct: float = 0.02      # 2% — halt + flatten for the day
    weekly_loss_pct: float = 0.05         # 5% — halt until next week
    max_notional_per_trade_pct: float = 0.20
    max_gross_exposure_pct: float = 1.00  # Σ|position notional| — no implicit leverage
    adv_participation_pct: float = 0.01   # qty ≤ 1% of 20-day avg daily volume
    min_stop_distance_pct: float = 0.0035 # stop floor — tiny ORB ranges can't explode size
    live_risk_scale: float = 0.5          # live mode halves every fraction (M8 training wheels)
    max_orders_per_session: int = 4
    max_open_positions: int = 10
    paper_slippage_bps: float = 2.0       # adverse slippage on simulated fills
    # The risk loop must not price the daily-loss halt / equity snapshot off a
    # frozen feed. A WS tick older than this (or no tick at all) is treated as
    # stale: ltp_lookup falls back to the runner's REST-refreshed last_price and
    # logs a WARNING so a frozen feed is visible.
    risk_ltp_stale_s: float = 60.0

    # ── ORB strategy ────────────────────────────────────────────────────────
    orb_range_minutes: int = 15
    poll_interval: float = 20.0           # seconds between quote polls
    trade_quantity: int = 1

    # ── Watchlist — dynamic from screener; only segment is fixed ────────────
    watchlist_exchange_segment: str = "NSE_EQ"
    # 20 candidates to watch for breakouts. This is NOT exposure — the risk
    # engine caps actual holdings (~4 full-risk positions via the daily
    # budget, max_open_positions=10). More candidates = more shots + faster
    # gate calibration, not more simultaneous risk. (Was 5 — a t4g.micro-era
    # default; raised on t4g.small with the live feed working.)
    watchlist_n: int = 20
    # Day-1 lesson (2026-06-12): the unfloored ATR% screener picks ₹13 penny
    # stocks where 2bps paper slippage is fantasy (tick = 7bps), and a cached
    # watchlist once smuggled in a non-tradeable index.
    screener_min_price: float = 50.0      # ₹ — average close floor
    screener_min_avg_volume: int = 50_000 # shares/day floor

    # ── Kronos ──────────────────────────────────────────────────────────────
    kronos_tokenizer: str = "NeoQuasar/Kronos-Tokenizer-base"
    kronos_model: str = "NeoQuasar/Kronos-base"
    # KRONOS_CHECKPOINT: a fine-tuned checkpoint dir. Accepts a local path OR an
    # s3:// URI (synced to a local cache once on first load — see
    # core/kronos_signal.KronosSignalEngine._resolve_checkpoint). Empty = HF
    # zero-shot from kronos_model above.
    kronos_checkpoint: str = ""
    # Scoring frequency/params follow the Kronos paper (arXiv:2508.02739):
    # NSE is in the pre-training corpus at 5-min+ ONLY (no 1-min), and the
    # paper's price/return-forecasting protocol is lookback 480 @ 5T with
    # T=0.6, top_p=0.90, N=10. 6×5min keeps the 30-min gate horizon.
    kronos_timeframe: str = "5min"        # "1min" = legacy (OOD for NSE)
    kronos_lookback: int = 480            # bars at kronos_timeframe
    kronos_pred_len: int = 6              # forecast bars (6×5min = 30 min)
    kronos_samples: int = 10              # Monte-Carlo rollouts averaged
    kronos_temperature: float = 0.6
    kronos_top_p: float = 0.9
    kronos_thresh: float = 0.001
    # Load from the local HF cache only — no network call on every start, and
    # the model can't silently change under a running calibration (a model
    # swap would invalidate verdicts, like a scorer_version change). A fresh
    # box with no cache downloads once, then runs offline. Updating the model
    # is a deliberate act (clear cache / bump revision), never automatic.
    kronos_offline: bool = True
    kronos_revision: str = ""             # pin a HF commit/tag; empty = whatever's cached
    kronos_min_confidence: float = 0.4
    kronos_scanner_enabled: bool = True
    # Shadow mode: the gate scores and PERSISTS every decision but never
    # blocks a trade. Stays on until calibration shows the gate adds value
    # (all pre-2026-06-11 decisions were scored on stale data — worthless).
    kronos_shadow_mode: bool = True

    # ── Web / dashboard ─────────────────────────────────────────────────────
    # SEC-09: HMAC secret for Dhan postback webhook. Empty = back-compat (no
    # verification). Set to a random hex string in production to authenticate
    # the X-Dhan-Signature header on incoming postback requests.
    dhan_webhook_secret: str = ""
    webhook_port: int = 8765
    # Bind address for the aiohttp TCPSite.  Default 0.0.0.0 preserves
    # Tailscale-direct access (dashboard is reached on the Tailscale interface
    # IP, e.g. 100.x.x.x:8765 — binding to 127.0.0.1 would break that).
    # Set to a specific interface address or 127.0.0.1 to harden if the
    # dashboard is only ever accessed through an SSH tunnel or localhost.
    api_bind_host: str = "0.0.0.0"
    # Shared secret for mutating POST endpoints (/api/killswitch,
    # /api/watchlist/refresh).  Empty = unprotected (fail-open so a
    # misconfigured secret never locks the operator out of the kill-switch).
    dashboard_token: str = ""

    # ── Telegram alerts (plain bot API — free, no LLM) ──────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Backfill paths ───────────────────────────────────────────────────────
    # Absolute paths match what is hardcoded in apps/api.py's backfill_status_handler
    # so production behaviour is unchanged when these env vars are unset.
    backfill_checkpoint_path: str = "/opt/dhan-trading/backfill_ckpt_NSE_EQ.json"
    backfill_log_path: str = "/tmp/backfill.log"

    # ── TimescaleDB ─────────────────────────────────────────────────────────
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "dhan_trading"
    db_user: str = "trader"
    db_password: str = "trader123"
    # Cleaned replica (M2.5, built by scripts/build_clean_db.py). The M3
    # backtester reads bars from HERE, not the raw dhan_trading, so the
    # three-way study runs on the same cleaned universe Kronos trains on.
    backtest_db_name: str = "dhan_clean"

    @property
    def db_url(self) -> str:
        return self._db_url(self.db_name)

    @property
    def backtest_db_url(self) -> str:
        return self._db_url(self.backtest_db_name)

    def _db_url(self, name: str) -> str:
        return (
            f"postgresql+psycopg2://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{name}"
        )

    # SEC-10: warn loudly when the known-weak default DB password is used in
    # LIVE mode.  The default is intentionally kept so local dev / tests
    # continue to work unchanged — this is a runtime guard, not a removal.
    @model_validator(mode="after")
    def _warn_weak_db_password(self) -> "Config":
        if self.db_password == "trader123" and not self.paper_trading:
            _cfg_logger.critical(
                "SEC-10: DB password is the known default 'trader123' while "
                "PAPER_TRADING=false.  Set a strong DB_PASSWORD in .env before "
                "running live."
            )
        return self

    # Back-compat alias: older modules referenced cfg.totp_secret
    @property
    def totp_secret(self) -> str:
        return self.dhan_totp_secret


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
