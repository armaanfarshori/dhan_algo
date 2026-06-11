"""
Strategy Engine
Base class for all strategies. ORBStrategy (strategies/strategy_orb.py) is the
sole production strategy; legacy examples were removed in the Phase-0 cleanup.

To build a strategy:
    1. Subclass BaseStrategy
    2. Implement async on_tick(tick_data) for signal logic
    3. Call self.buy() / self.sell() / self.exit_position()
    4. asyncio.create_task(strategy.run())
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

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

    def __init__(self, client, risk_manager, config: StrategyConfig,
                 db_backend=None, run_id=None, poll_offset: float = 0.0):
        self.client = client
        self.risk = risk_manager
        self.config = config
        self.position: int = 0
        self.entry_price: float = 0.0
        self.orders_placed: int = 0
        self.signals: List[Signal] = []
        self._running = False
        self._db = db_backend   # AsyncDBBackend — optional, fails silently if None
        self._poll_offset = poll_offset  # stagger start so N strategies don't burst together
        self._run_id = run_id   # links all orders/signals to this agent session

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

        mode_str = "PAPER" if self.config.paper_trading else "LIVE"
        if self.config.paper_trading:
            logger.info(f"📝 [PAPER] BUY {self.config.quantity} @ ₹{price:.2f} — {reason}")
            self.position = self.config.quantity
            self.entry_price = price
            self.orders_placed += 1
            result = {"paper": True, "action": "BUY", "price": price}
        else:
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

        if self._db:
            oid = result.get("orderId") if not result.get("paper") else None
            await self._db.log_order(
                security_id=self.config.security_id,
                exchange_segment=self.config.exchange_segment,
                side="BUY", qty=self.config.quantity,
                order_type="MARKET", product_type=self.config.product_type,
                mode=mode_str, price=price, dhan_order_id=oid,
                run_id=self._run_id, status="TRADED" if self.config.paper_trading else "PENDING",
            )
            await self._db.log_trade_entry(
                security_id=self.config.security_id, side="BUY",
                qty=self.config.quantity, entry_price=price,
                strategy=self.config.name, dhan_order_id=oid,
            )
        return result

    async def sell(self, price: float, reason: str = "") -> Optional[Dict]:
        if self.position < 0:
            logger.debug(f"[{self.config.name}] Already short, skip SELL")
            return None
        ok, msg = self.risk.check_order(self.config.quantity, price, "SELL")
        if not ok:
            logger.warning(f"[{self.config.name}] Risk block: {msg}")
            return None

        mode_str = "PAPER" if self.config.paper_trading else "LIVE"
        if self.config.paper_trading:
            logger.info(f"📝 [PAPER] SELL {self.config.quantity} @ ₹{price:.2f} — {reason}")
            self.position = -self.config.quantity
            self.entry_price = price
            self.orders_placed += 1
            result = {"paper": True, "action": "SELL", "price": price}
        else:
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

        if self._db:
            oid = result.get("orderId") if not result.get("paper") else None
            await self._db.log_order(
                security_id=self.config.security_id,
                exchange_segment=self.config.exchange_segment,
                side="SELL", qty=self.config.quantity,
                order_type="MARKET", product_type=self.config.product_type,
                mode=mode_str, price=price, dhan_order_id=oid,
                run_id=self._run_id, status="TRADED" if self.config.paper_trading else "PENDING",
            )
            await self._db.log_trade_entry(
                security_id=self.config.security_id, side="SELL",
                qty=self.config.quantity, entry_price=price,
                strategy=self.config.name, dhan_order_id=oid,
            )
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
        pnl_realized = pnl
        self.position = 0
        if self._db:
            oid = result.get("orderId") if result and not result.get("paper") else None
            await self._db.log_trade_exit(
                security_id=self.config.security_id,
                exit_price=price, pnl=pnl_realized, dhan_order_id=oid,
            )
        return result

    @staticmethod
    def _is_market_hours() -> bool:
        """True only during NSE trading hours (9:00–15:35 IST) on weekdays."""
        from datetime import datetime, time as dtime
        import pytz
        now = datetime.now(pytz.timezone("Asia/Kolkata"))
        if now.weekday() >= 5:          # Saturday / Sunday
            return False
        t = now.time()
        return dtime(9, 0) <= t <= dtime(15, 35)

    async def run(self):
        """Main strategy loop — polls live OHLC only during NSE market hours."""
        self._running = True
        logger.info(f"▶ Strategy '{self.config.name}' started (paper={self.config.paper_trading}, offset={self._poll_offset:.1f}s)")
        if self._poll_offset > 0:
            await asyncio.sleep(self._poll_offset)
        while self._running:
            if not self._is_market_hours():
                # Outside market hours: sleep 60s, no API calls
                await asyncio.sleep(60)
                continue
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
