"""
Opening Range Breakout (ORB) Strategy  [Kronos-enhanced]
=========================================================
Logic:
  1. During the first `orb_minutes` minutes of the session (9:15–9:29 for 15-min ORB),
     track the rolling high (OR_HIGH) and low (OR_LOW).
  2. After the opening range is locked:
     - Price closes ABOVE OR_HIGH  → BUY candidate
     - Price closes BELOW OR_LOW   → SELL candidate
  3. If `use_kronos=True` (default when DB is available), the Kronos foundation model
     scores the direction. Trade is only taken if Kronos agrees (same side) AND
     confidence >= `kronos_min_confidence`.
  4. Stop-loss = other side of the OR + buffer.
  5. Target = entry + 1.5× OR range.
  6. Exit any open position 15 minutes before market close (3:15 PM IST).
  7. Only one trade per direction per security per session.

Extends BaseStrategy — drop this into the existing strategy engine with:
    strategy = ORBStrategy(client, risk_manager, config, orb_config)
    asyncio.create_task(strategy.run())
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Dict, List, Optional

import pytz

from strategies.strategy_base import BaseStrategy, Signal, StrategyConfig

logger = logging.getLogger("dhan.strategy.orb")

IST = pytz.timezone("Asia/Kolkata")

_MARKET_OPEN  = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)


@dataclass
class ORBConfig:
    # Opening range duration in minutes (15 or 30 recommended)
    orb_minutes: int = 15

    # Buffer added/subtracted to stop-loss to avoid slippage noise (% of OR range)
    sl_buffer_pct: float = 0.002

    # Target as a multiple of the OR range
    target_multiplier: float = 1.5

    # Square off N minutes before close
    squareoff_before_close_min: int = 15

    # Only trade if OR range is at least this fraction of the stock price
    min_range_pct: float = 0.003

    # Kronos pre-filter: require model agreement before entering
    use_kronos: bool = True
    kronos_min_confidence: float = 0.4   # minimum Kronos confidence to allow entry


class ORBStrategy(BaseStrategy):
    """
    Single-security ORB strategy with optional Kronos pre-filter.
    Instantiate one per security.
    """

    def __init__(
        self,
        client,
        risk_manager,
        config: StrategyConfig,
        orb_config: Optional[ORBConfig] = None,
        trade_logger=None,
        kronos_engine=None,
        db_backend=None,
        run_id=None,
        poll_offset: float = 0.0,
    ):
        super().__init__(client, risk_manager, config, db_backend=db_backend,
                         run_id=run_id, poll_offset=poll_offset)
        self.orb_cfg = orb_config or ORBConfig()
        self._trade_logger = trade_logger
        self._kronos = kronos_engine  # KronosSignalEngine instance or None

        # Opening range state — reset each session
        self._or_high: float = 0.0
        self._or_low: float = float("inf")
        self._or_locked: bool = False
        self._session_date: Optional[str] = None

        # Per-session trade guards
        self._long_taken: bool = False
        self._short_taken: bool = False

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def or_range(self) -> float:
        return self._or_high - self._or_low if self._or_locked else 0.0

    def reset_session(self):
        self._or_high = 0.0
        self._or_low = float("inf")
        self._or_locked = False
        self._long_taken = False
        self._short_taken = False
        self.position = 0
        self.entry_price = 0.0

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    async def on_tick(self, tick: Dict) -> Optional[Signal]:
        """
        tick = {
            "last_price": float,
            "ohlc": {"open": float, "high": float, "low": float, "close": float},
            "volume": int,
        }
        """
        now_ist = datetime.now(IST)
        today = now_ist.strftime("%Y-%m-%d")
        now_t = now_ist.time().replace(tzinfo=None)

        # Reset at start of each new trading session
        if self._session_date != today:
            self.reset_session()
            self._session_date = today
            logger.info("[%s] New session %s — ORB reset", self.config.name, today)

        price = float(tick.get("last_price", 0))
        if price <= 0:
            return None

        # ── 1. Build opening range ──────────────────────────────────────
        or_end_t = dtime(
            _MARKET_OPEN.hour,
            _MARKET_OPEN.minute + self.orb_cfg.orb_minutes,
        )

        if _MARKET_OPEN <= now_t < or_end_t and not self._or_locked:
            ohlc = tick.get("ohlc", {})
            candle_high = float(ohlc.get("high", price))
            candle_low  = float(ohlc.get("low",  price))
            self._or_high = max(self._or_high, candle_high)
            self._or_low  = min(self._or_low,  candle_low)
            logger.debug(
                "[%s] OR building  H=%.2f  L=%.2f",
                self.config.name, self._or_high, self._or_low,
            )
            return None

        # Lock OR after range period ends
        if not self._or_locked and now_t >= or_end_t and self._or_high > 0:
            self._or_locked = True
            logger.info(
                "[%s] OR locked  HIGH=%.2f  LOW=%.2f  RANGE=%.2f",
                self.config.name, self._or_high, self._or_low, self.or_range,
            )

        if not self._or_locked:
            return None

        # ── 2. Auto square-off near close ──────────────────────────────
        squareoff_t = dtime(
            _MARKET_CLOSE.hour,
            _MARKET_CLOSE.minute - self.orb_cfg.squareoff_before_close_min,
        )
        if now_t >= squareoff_t and self.position != 0:
            return await self._exit("EOD square-off", price)

        # ── 3. Exit management for open positions ──────────────────────
        if self.position > 0:
            target = self.entry_price + self.orb_cfg.target_multiplier * self.or_range
            stop   = self._or_low * (1 - self.orb_cfg.sl_buffer_pct)
            if price >= target:
                return await self._exit(f"Target hit ₹{target:.2f}", price)
            if price <= stop:
                return await self._exit(f"Stop-loss ₹{stop:.2f}", price)

        elif self.position < 0:
            target = self.entry_price - self.orb_cfg.target_multiplier * self.or_range
            stop   = self._or_high * (1 + self.orb_cfg.sl_buffer_pct)
            if price <= target:
                return await self._exit(f"Target hit ₹{target:.2f}", price)
            if price >= stop:
                return await self._exit(f"Stop-loss ₹{stop:.2f}", price)

        # ── 4. Entry signals ────────────────────────────────────────────
        if self.position != 0:
            return None  # already in a trade

        min_range = price * self.orb_cfg.min_range_pct
        if self.or_range < min_range:
            return None  # range too narrow — skip

        if not self._long_taken and price > self._or_high:
            logger.info("[%s] Long breakout @ %.2f > OR_HIGH %.2f", self.config.name, price, self._or_high)
            if not await self._kronos_allows("BUY"):
                self._long_taken = True  # don't retry same direction
                return None
            self._long_taken = True
            result = await self.buy(price, f"ORB long breakout  OR={self._or_high:.2f}/{self._or_low:.2f}")
            if result is not None:
                sig = Signal("BUY", price, f"ORB breakout above {self._or_high:.2f}")
                self.signals.append(sig)
                self._log_entry("BUY", price)
                return sig

        elif not self._short_taken and price < self._or_low:
            logger.info("[%s] Short breakout @ %.2f < OR_LOW %.2f", self.config.name, price, self._or_low)
            if not await self._kronos_allows("SELL"):
                self._short_taken = True
                return None
            self._short_taken = True
            result = await self.sell(price, f"ORB short breakout  OR={self._or_high:.2f}/{self._or_low:.2f}")
            if result is not None:
                sig = Signal("SELL", price, f"ORB breakdown below {self._or_low:.2f}")
                self.signals.append(sig)
                self._log_entry("SELL", price)
                return sig

        return None

    async def _kronos_allows(self, direction: str) -> bool:
        """Return True if Kronos agrees with the proposed trade direction (or is disabled)."""
        if not self.orb_cfg.use_kronos or self._kronos is None:
            return True
        try:
            result = await self._kronos.score_from_db(
                security_id=self.config.security_id,
                exchange_segment=self.config.exchange_segment,
            )
            model_side = result.get("side", "HOLD")
            confidence = result.get("confidence", 0.0)
            agrees = (model_side == direction) and (confidence >= self.orb_cfg.kronos_min_confidence)
            logger.info(
                "[%s] Kronos says %s (conf=%.2f)  ORB wants %s  → %s",
                self.config.name, model_side, confidence, direction,
                "ALLOW" if agrees else "BLOCK",
            )
            return agrees
        except Exception as exc:
            logger.warning("[%s] Kronos check failed (%s) — allowing trade", self.config.name, exc)
            return True  # fail-open: don't block trades due to model errors

    # ------------------------------------------------------------------
    # Sell (short) — mirrors buy() from BaseStrategy
    # ------------------------------------------------------------------

    async def sell(self, price: float, reason: str = "") -> Optional[Dict]:
        if self.position < 0:
            logger.debug("[%s] Already short, skip SELL", self.config.name)
            return None
        ok, msg = self.risk.check_order(self.config.quantity, price, "SELL")
        if not ok:
            logger.warning("[%s] Risk block: %s", self.config.name, msg)
            return None
        if self.orders_placed >= self.config.max_orders:
            return None

        if self.config.paper_trading:
            logger.info("📝 [PAPER] SELL %d @ ₹%.2f — %s", self.config.quantity, price, reason)
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
        return result

    # ------------------------------------------------------------------
    # Exit helper
    # ------------------------------------------------------------------

    async def _exit(self, reason: str, price: float) -> Optional[Signal]:
        if self.position == 0:
            return None

        pnl = (price - self.entry_price) * abs(self.position)
        if self.position < 0:
            pnl = (self.entry_price - price) * abs(self.position)

        if self.config.paper_trading:
            logger.info(
                "📝 [PAPER] EXIT %d @ ₹%.2f  PnL ₹%+.2f — %s",
                abs(self.position), price, pnl, reason,
            )
        else:
            exit_side = "BUY" if self.position < 0 else "SELL"
            await self.client.place_order(
                transaction_type=exit_side,
                exchange_segment=self.config.exchange_segment,
                product_type=self.config.product_type,
                order_type="MARKET",
                security_id=self.config.security_id,
                quantity=abs(self.position),
            )
            self.orders_placed += 1

        self._log_exit(price, pnl, reason)
        sig = Signal("EXIT", price, f"{reason}  PnL ₹{pnl:+.2f}")
        self.signals.append(sig)
        self.position = 0
        self.entry_price = 0.0
        return sig

    # ------------------------------------------------------------------
    # Trade logger integration
    # ------------------------------------------------------------------

    def _log_entry(self, action: str, price: float):
        if self._trade_logger is None:
            return
        self._trade_logger.log_entry(
            engine="EQ",
            symbol=self.config.security_id,
            action=action,
            price=price,
            qty=self.config.quantity,
            mode="PAPER" if self.config.paper_trading else "LIVE",
            strategy="ORB",
            target=price + self.orb_cfg.target_multiplier * self.or_range,
            stop=(self._or_low if action == "BUY" else self._or_high),
        )

    def _log_exit(self, price: float, pnl: float, reason: str):
        if self._trade_logger is None:
            return
        self._trade_logger.log_exit(
            engine="EQ",
            symbol=self.config.security_id,
            action="EXIT",
            price=price,
            qty=abs(self.position),
            mode="PAPER" if self.config.paper_trading else "LIVE",
            entry_price=self.entry_price,
            pnl=pnl,
            reason=reason,
            strategy="ORB",
        )
