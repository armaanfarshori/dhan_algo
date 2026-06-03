"""
Centralised configuration — reads .env and exposes a typed Config object.
main.py uses os.getenv() directly; this module serves the data pipeline
and new components (backfill, db_journal, strategy_orb).
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Dhan credentials
    dhan_client_id: str = field(default_factory=lambda: os.getenv("DHAN_CLIENT_ID", "mock"))
    dhan_access_token: str = field(default_factory=lambda: os.getenv("DHAN_ACCESS_TOKEN", "mock"))
    dhan_pin: str = field(default_factory=lambda: os.getenv("DHAN_PIN", ""))
    totp_secret: str = field(default_factory=lambda: os.getenv("DHAN_TOTP_SECRET", ""))

    # Trading mode
    paper_trading: bool = field(
        default_factory=lambda: os.getenv("PAPER_TRADING", "true").lower() != "false"
    )
    strategy: str = field(default_factory=lambda: os.getenv("STRATEGY", "scalper"))

    # Risk defaults
    max_daily_loss: float = field(
        default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS", "5000"))
    )
    capital: float = field(default_factory=lambda: float(os.getenv("CAPITAL", "100000")))
    risk_per_trade: float = field(
        default_factory=lambda: float(os.getenv("RISK_PER_TRADE", "0.01"))
    )

    # ORB settings
    orb_range_minutes: int = field(
        default_factory=lambda: int(os.getenv("ORB_RANGE_MINUTES", "15"))
    )

    # Watchlist
    watchlist_security_ids: list[str] = field(default_factory=lambda: [
        s.strip() for s in os.getenv("WATCHLIST_SECURITY_IDS", "2885,1333,1594,11536").split(",")
    ])
    watchlist_exchange_segment: str = field(
        default_factory=lambda: os.getenv("WATCHLIST_EXCHANGE_SEGMENT", "NSE_EQ")
    )

    # TimescaleDB
    db_host: str = field(default_factory=lambda: os.getenv("DB_HOST", "localhost"))
    db_port: int = field(default_factory=lambda: int(os.getenv("DB_PORT", "5432")))
    db_name: str = field(default_factory=lambda: os.getenv("DB_NAME", "dhan_trading"))
    db_user: str = field(default_factory=lambda: os.getenv("DB_USER", "trader"))
    db_password: str = field(default_factory=lambda: os.getenv("DB_PASSWORD", "trader123"))

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


_cfg: Config | None = None


def get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg
