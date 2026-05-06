"""
Strategy Engine
Base class for all strategies + a ready-to-deploy SMA Crossover example.

To build your own strategy:
    1. Subclass BaseStrategy
    2. Implement async on_tick(tick_data) for signal logic
    3. Call self.buy() / self.sell() / self.exit_all()
    4. Run via StrategyRunner
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, Dict, List, Optional

logger = logging.getLogger("dhan.strategy")


@dataclass
class StrategyConfig:
    name: str = "BaseStrategy"
    security_id: str = ""
    exchange_segment: str = "NSE_EQ"
    product_type: str = "INTRADAY"
    quantity: int = 1
    # How often to poll market data (seconds)
    poll_interval: float = 5.0
    # Max orders this strategy can place per session
    max_orders: int = 20
    paper_trading: bool = True   # Set False for live


@dataclass
class Signal:
    action: str   # "BUY", "SELL", "EXIT", "HOLD"
    price: float
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)


class BaseStrategy(ABC):
    """
    Abstract base strategy. Subclass and implement on_tick().
    """

    def __init__(self, client, risk_manager, config: StrategyConfig):
        self.client = client
        self.risk = risk_manager
        self.config = config
        self.position: int = 0          # net qty (+ = long, - = short)
        self.entry_price: float = 0.0
        self.orders_placed: int = 0
        self.signals: List[Signal] = []
        self._running = False

    @abstractmethod
    async def on_tick(self, tick: Dict) -> Optional[Signal]:
        """
        Called every poll_interval with latest market data.
        Return a Signal or None.
        tick = {
            "last_price": float,
            "ohlc": {"open":, "high":, "low":, "close":},
            "volume": int, ...
        }
        """

    async def buy(self, price: float, reason: str = "") -> Optional[Dict]:
        if self.position > 0:
            logger.debug(f"[{self.config.name}] Already long, skip BUY")
            return None
        ok, msg = self.risk.check_order(self.config.quantity, price, "BUY")
        if not ok:
            logger.warning(f"[{self.config.name}] Risk block: {msg}")
            return None
        if self.orders_placed >= self.config.max_orders:
            logger.warning(f"[{self.config.name}] Max orders reached")
            return None

        if self.config.paper_trading:
            logger.info(f"📝 [PAPER] BUY {self.config.quantity} @ ₹{price:.2f} — {reason}")
            self.position = self.config.quantity
            self.entry_price = price
            self.orders_placed += 1
            return {"paper": True, "action": "BUY", "price": price}

        result = await self.client.place_order(
            transaction_type="BUY",
            exchange_segment=self.config.exchange_segment,
            product_type=self.config.product_type,
            order_type="MARKET",
            security_id=self.config.security_id,
            quantity=self.config.quantity,
        )
        self.position = self.config.quantity
        self.entry_price = price
        self.orders_placed += 1
        logger.info(f"🟢 BUY order placed: {result}")
        return result

    async def sell(self, price: float, reason: str = "") -> Optional[Dict]:
        if self.position < 0:
            logger.debug(f"[{self.config.name}] Already short, skip SELL")
            return None
        ok, msg = self.risk.check_order(self.config.quantity, price, "SELL")
        if not ok:
            logger.warning(f"[{self.config.name}] Risk block: {msg}")
            return None

        if self.config.paper_trading:
            logger.info(f"📝 [PAPER] SELL {self.config.quantity} @ ₹{price:.2f} — {reason}")
            self.position = -self.config.quantity
            self.entry_price = price
            self.orders_placed += 1
            return {"paper": True, "action": "SELL", "price": price}

        result = await self.client.place_order(
            transaction_type="SELL",
            exchange_segment=self.config.exchange_segment,
            product_type=self.config.product_type,
            order_type="MARKET",
            security_id=self.config.security_id,
            quantity=self.config.quantity,
        )
        self.position = -self.config.quantity
        self.entry_price = price
        self.orders_placed += 1
        logger.info(f"🔴 SELL order placed: {result}")
        return result

    async def exit_position(self, price: float, reason: str = "") -> Optional[Dict]:
        if self.position == 0:
            return None
        side = "SELL" if self.position > 0 else "BUY"
        pnl = (price - self.entry_price) * self.position
        logger.info(f"📤 EXIT {side} @ ₹{price:.2f} | PnL ≈ ₹{pnl:+.2f} — {reason}")

        if self.config.paper_trading:
            self.position = 0
            return {"paper": True, "action": "EXIT", "pnl": pnl}

        result = await self.client.place_order(
            transaction_type=side,
            exchange_segment=self.config.exchange_segment,
            product_type=self.config.product_type,
            order_type="MARKET",
            security_id=self.config.security_id,
            quantity=abs(self.position),
        )
        self.position = 0
        return result

    async def run(self):
        """Main strategy loop — polls data and calls on_tick()."""
        self._running = True
        logger.info(f"▶ Strategy '{self.config.name}' started (paper={self.config.paper_trading})")
        while self._running:
            try:
                data = await self.client.get_ohlc(
                    {self.config.exchange_segment: [int(self.config.security_id)]}
                )
                segment_data = data.get("data", {}).get(self.config.exchange_segment, {})
                tick = segment_data.get(self.config.security_id, {})

                if tick:
                    signal = await self.on_tick(tick)
                    if signal:
                        self.signals.append(signal)
                        if signal.action == "BUY":
                            await self.buy(signal.price, signal.reason)
                        elif signal.action == "SELL":
                            await self.sell(signal.price, signal.reason)
                        elif signal.action == "EXIT":
                            await self.exit_position(signal.price, signal.reason)
            except Exception as e:
                logger.error(f"Strategy tick error: {e}")
            await asyncio.sleep(self.config.poll_interval)

    def stop(self):
        self._running = False
        logger.info(f"⏹ Strategy '{self.config.name}' stopped")


# ======================================================================
#  READY-TO-USE STRATEGY: SMA Crossover
# ======================================================================

@dataclass
class SMAConfig(StrategyConfig):
    name: str = "SMA_Crossover"
    fast_period: int = 9
    slow_period: int = 21


class SMACrossoverStrategy(BaseStrategy):
    """
    Simple Moving Average (SMA) Crossover Strategy.

    Signal logic:
        - BUY  when fast SMA crosses above slow SMA (golden cross)
        - SELL when fast SMA crosses below slow SMA (death cross)
        - EXIT existing position on reverse cross

    Suitable for equity intraday (CNC/MIS) or futures.
    """

    def __init__(self, client, risk_manager, config: SMAConfig):
        super().__init__(client, risk_manager, config)
        self.sma_config: SMAConfig = config
        fp = config.fast_period
        sp = config.slow_period
        self._fast_prices: Deque[float] = deque(maxlen=fp)
        self._slow_prices: Deque[float] = deque(maxlen=sp)
        self._prev_fast_sma: Optional[float] = None
        self._prev_slow_sma: Optional[float] = None

    def _sma(self, dq: Deque[float]) -> Optional[float]:
        if len(dq) < dq.maxlen:
            return None
        return sum(dq) / len(dq)

    async def on_tick(self, tick: Dict) -> Optional[Signal]:
        price = tick.get("last_price", 0.0)
        if not price:
            return None

        self._fast_prices.append(price)
        self._slow_prices.append(price)

        fast = self._sma(self._fast_prices)
        slow = self._sma(self._slow_prices)

        if fast is None or slow is None:
            logger.debug(
                f"Warming up — fast:{len(self._fast_prices)}/{self.sma_config.fast_period} "
                f"slow:{len(self._slow_prices)}/{self.sma_config.slow_period}"
            )
            self._prev_fast_sma = fast
            self._prev_slow_sma = slow
            return None

        prev_fast = self._prev_fast_sma
        prev_slow = self._prev_slow_sma
        self._prev_fast_sma = fast
        self._prev_slow_sma = slow

        if prev_fast is None or prev_slow is None:
            return None

        # Golden cross: fast crosses above slow
        if prev_fast <= prev_slow and fast > slow:
            action = "EXIT" if self.position < 0 else "BUY"
            return Signal(
                action=action,
                price=price,
                reason=f"Golden cross — fast SMA {fast:.2f} > slow SMA {slow:.2f}",
            )

        # Death cross: fast crosses below slow
        if prev_fast >= prev_slow and fast < slow:
            action = "EXIT" if self.position > 0 else "SELL"
            return Signal(
                action=action,
                price=price,
                reason=f"Death cross — fast SMA {fast:.2f} < slow SMA {slow:.2f}",
            )

        return None


# ======================================================================
#  READY-TO-USE STRATEGY: Options Straddle Seller (short volatility)
# ======================================================================

class StraddleSellerConfig(StrategyConfig):
    name: str = "Straddle_Seller"
    call_security_id: str = ""   # ATM Call securityId
    put_security_id: str = ""    # ATM Put securityId
    exchange_segment: str = "NSE_FNO"
    product_type: str = "MARGIN"
    # Stop loss as % of premium collected
    stop_loss_pct: float = 50.0
    # Target profit as % of premium collected
    target_pct: float = 30.0


class StraddleSellerStrategy(BaseStrategy):
    """
    Short Straddle options strategy.
    Sells ATM CE + PE at open, exits on SL or target.

    ⚠  Always use with adequate margin and risk controls.
    """

    def __init__(self, client, risk_manager, config: StraddleSellerConfig):
        super().__init__(client, risk_manager, config)
        self.straddle_cfg: StraddleSellerConfig = config
        self.premium_collected: float = 0.0
        self.entered: bool = False

    async def on_tick(self, tick: Dict) -> Optional[Signal]:
        """
        Override: this strategy needs two-leg quotes.
        The tick here is for monitoring PnL against premium.
        """
        if not self.entered:
            return Signal(action="BUY", price=0, reason="Enter straddle at open")

        current_val = tick.get("last_price", 0.0)
        pnl_pct = ((self.premium_collected - current_val) / self.premium_collected) * 100

        if pnl_pct >= self.straddle_cfg.target_pct:
            return Signal(
                action="EXIT",
                price=current_val,
                reason=f"Target hit: {pnl_pct:.1f}% profit",
            )
        if pnl_pct <= -self.straddle_cfg.stop_loss_pct:
            return Signal(
                action="EXIT",
                price=current_val,
                reason=f"Stop loss hit: {pnl_pct:.1f}% loss",
            )
        return None
