"""
Engine value types — shared by strategies, executors, portfolio, and backtester.

Strategies emit OrderIntent; executors turn intents into Fills; the Portfolio
applies Fills. Nothing in this module touches IO.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class OrderIntent:
    """What a strategy wants to do. Pure data — no client, no mode."""
    security_id: str
    exchange_segment: str
    side: str                    # "BUY" | "SELL"
    qty: int
    strategy: str
    reason: str = ""
    # Risk metadata — lets the risk engine size/validate by stop distance
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    product_type: str = "INTRADAY"
    # Per-order PAPER slippage override (bps). None → the executor's default.
    # The options scalper sets this to a realistic option-leg half-spread; equity/
    # ORB intents leave it None, so their fill price is byte-identical.
    slippage_bps: Optional[float] = None
    # Optional hint to slice a large order into child orders (LIVE only). Default
    # False; equity/ORB intents never set it, so their live path is unchanged.
    slice_order: bool = False


@dataclass(frozen=True)
class Fill:
    """An executed order (real or simulated)."""
    security_id: str
    side: str                    # "BUY" | "SELL"
    qty: int
    price: float
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    order_id: Optional[str] = None    # Dhan order id (live) or None (paper)
    mode: str = "PAPER"               # "PAPER" | "LIVE"


@dataclass
class Position:
    """Net position in one security. qty is signed: + long, − short."""
    security_id: str
    qty: int = 0
    avg_price: float = 0.0
    strategy: str = ""

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    def unrealized_pnl(self, ltp: float) -> float:
        if self.qty == 0 or ltp <= 0:
            return 0.0
        return (ltp - self.avg_price) * self.qty
