"""
Opening Range Breakout — pure signal logic.

This class is deliberately synchronous and IO-free: ticks in, decisions out.
The same code runs live (driven by StrategyRunner) and in the backtester
(driven by bar replay) — which is the entire point of the Phase-1 rewrite.

Decision protocol — on_tick() returns None or a Decision:
    Decision(action="ENTER", side="BUY"|"SELL", stop=…, target=…, reason=…)
    Decision(action="EXIT",  reason=…)

Position state is fed back by the caller via notify_fill()/notify_flat();
the strategy never assumes an order it requested was actually executed
(risk gate or executor may reject it).
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("dhan.strategy.orb")

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
# A tick stamped further than this ahead of the wall clock is implausible —
# we ignore it rather than trust it to date a session or widen the OR.
MAX_FUTURE_SKEW = timedelta(minutes=2)


@dataclass
class ORBParams:
    orb_minutes: int = 15
    sl_buffer_pct: float = 0.002        # stop padding beyond the opposite OR edge
    target_multiplier: float = 1.5      # target = entry ± multiplier × OR range
    squareoff_before_close_min: int = 15
    min_range_pct: float = 0.003        # skip if OR range < this fraction of price


@dataclass(frozen=True)
class Decision:
    action: str                  # "ENTER" | "EXIT"
    side: str = ""               # for ENTER: "BUY" | "SELL"
    stop: float = 0.0
    target: float = 0.0
    reason: str = ""


class ORB:
    def __init__(self, security_id: str, params: Optional[ORBParams] = None):
        self.security_id = security_id
        self.p = params or ORBParams()

        self._session_date: Optional[date] = None
        self.or_high: float = 0.0
        self.or_low: float = float("inf")
        self.or_locked: bool = False
        self._long_tried = False
        self._short_tried = False

        # Position view — updated only via notify_fill/notify_flat
        self.position: int = 0
        self.entry_price: float = 0.0

    # ── Caller feedback ───────────────────────────────────────────────────────

    def notify_fill(self, side: str, qty: int, price: float):
        self.position += qty if side == "BUY" else -qty
        if self.position != 0:
            self.entry_price = price
        else:
            self.entry_price = 0.0

    def notify_flat(self):
        self.position = 0
        self.entry_price = 0.0

    # ── Core logic ────────────────────────────────────────────────────────────

    def on_tick(self, now: datetime, price: float,
                high: Optional[float] = None, low: Optional[float] = None) -> Optional[Decision]:
        """now must be IST. high/low are the current intrabar extremes if known."""
        if price <= 0:
            return None

        # Defense-in-depth: a tick whose timestamp is implausibly in the future
        # (vs the wall clock) must NOT reset the session (it would derive a
        # future session date and wipe a locked OR) nor widen the OR. Compare in
        # IST; tolerate either naive (assumed IST) or tz-aware timestamps.
        wall = datetime.now(IST)
        ref = wall if now.tzinfo is not None else wall.replace(tzinfo=None)
        if now - ref > MAX_FUTURE_SKEW:
            logger.warning("[ORB %s] ignoring future-stamped tick %s (now %s) — skew > %s",
                           self.security_id, now, ref, MAX_FUTURE_SKEW)
            return None

        today = now.date()
        t = now.time()

        if self._session_date != today:
            self._reset_session(today)

        or_end = (datetime.combine(today, MARKET_OPEN)
                  + timedelta(minutes=self.p.orb_minutes)).time()

        # 1. Build the opening range
        if MARKET_OPEN <= t < or_end and not self.or_locked:
            self.or_high = max(self.or_high, high or price)
            self.or_low = min(self.or_low, low or price)
            return None

        # 2. Lock it once the window closes
        if not self.or_locked and t >= or_end and self.or_high > 0:
            self.or_locked = True
            logger.info("[ORB %s] OR locked  HIGH=%.2f LOW=%.2f RANGE=%.2f",
                        self.security_id, self.or_high, self.or_low, self.or_range)
        # 3. EOD square-off — must NOT depend on a locked OR: after a
        # mid-session restart the range can be unknown, but an open
        # (DB-reconciled) position must still flatten before close.
        squareoff = (datetime.combine(today, MARKET_CLOSE)
                     - timedelta(minutes=self.p.squareoff_before_close_min)).time()
        if t >= squareoff:
            if self.position != 0:
                return Decision(action="EXIT", reason="EOD square-off")
            return None

        if not self.or_locked:
            return None

        # 4. Exits for the open position
        if self.position > 0:
            target = self.entry_price + self.p.target_multiplier * self.or_range
            stop = self.or_low * (1 - self.p.sl_buffer_pct)
            if price >= target:
                return Decision(action="EXIT", reason=f"Target hit ₹{target:.2f}")
            if price <= stop:
                return Decision(action="EXIT", reason=f"Stop-loss ₹{stop:.2f}")
            return None
        if self.position < 0:
            target = self.entry_price - self.p.target_multiplier * self.or_range
            stop = self.or_high * (1 + self.p.sl_buffer_pct)
            if price <= target:
                return Decision(action="EXIT", reason=f"Target hit ₹{target:.2f}")
            if price >= stop:
                return Decision(action="EXIT", reason=f"Stop-loss ₹{stop:.2f}")
            return None

        # 5. Entries
        if self.or_range < price * self.p.min_range_pct:
            return None   # range too narrow

        if not self._long_tried and price > self.or_high:
            self._long_tried = True
            stop = self.or_low * (1 - self.p.sl_buffer_pct)
            return Decision(
                action="ENTER", side="BUY", stop=stop,
                target=price + self.p.target_multiplier * self.or_range,
                reason=f"ORB long breakout  OR={self.or_high:.2f}/{self.or_low:.2f}")
        if not self._short_tried and price < self.or_low:
            self._short_tried = True
            stop = self.or_high * (1 + self.p.sl_buffer_pct)
            return Decision(
                action="ENTER", side="SELL", stop=stop,
                target=price - self.p.target_multiplier * self.or_range,
                reason=f"ORB short breakdown  OR={self.or_high:.2f}/{self.or_low:.2f}")
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def or_range(self) -> float:
        return (self.or_high - self.or_low) if self.or_locked else 0.0

    def seed_opening_range(self, today: date, high: float, low: float,
                           post_or_high: float = 0.0,
                           post_or_low: float = float("inf")):
        """
        Restore today's OR after a mid-session restart (from REST intraday bars).
        post_or_high/low are the extremes AFTER the OR window: if the breakout
        already happened while we weren't watching, mark that side as tried so
        we don't take it hours late at a much worse price.
        """
        if high <= 0 or low <= 0 or high < low:
            return
        self._session_date = today
        self.or_high = high
        self.or_low = low
        self.or_locked = True
        if post_or_high > high:
            self._long_tried = True
        if post_or_low < low:
            self._short_tried = True
        logger.info("[ORB %s] OR seeded  HIGH=%.2f LOW=%.2f (long_tried=%s short_tried=%s)",
                    self.security_id, high, low, self._long_tried, self._short_tried)

    def _reset_session(self, today: date):
        self._session_date = today
        self.or_high = 0.0
        self.or_low = float("inf")
        self.or_locked = False
        self._long_tried = False
        self._short_tried = False
        logger.info("[ORB %s] New session %s — reset", self.security_id, today)

    def status(self) -> dict:
        return {
            "security_id": self.security_id,
            "or_high": round(self.or_high, 2) if self.or_high else 0,
            "or_low": round(self.or_low, 2) if self.or_low != float("inf") else 0,
            "or_locked": self.or_locked,
            "position": self.position,
            "entry_price": self.entry_price,
            "long_tried": self._long_tried,
            "short_tried": self._short_tried,
        }
