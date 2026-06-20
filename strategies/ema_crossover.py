"""
EMA Crossover (9/21) — pure signal logic, intraday trend-following.

Incremental dual-EMA from the bar-close stream. Bullish cross (fast > slow) → ENTER BUY;
bearish cross (fast < slow) → ENTER SELL. Exit on the opposite cross, close-based stop,
or EOD square-off. A far ±50% "no-target" satisfies the Decision contract; all real exits
come from on_tick.

Same interface as strategies/orb.py — synchronous, IO-free: ticks in, Decisions out.
Reuses Decision, IST, MAX_FUTURE_SKEW from strategies.orb; session hours come from a
MarketSession profile (EQUITY default → byte-identical legacy behaviour).
"""
import logging
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from core.sessions import EQUITY, MarketSession
from strategies.orb import (
    Decision,
    IST,
    MAX_FUTURE_SKEW,
)

logger = logging.getLogger("dhan.strategy.ema_crossover")


@dataclass
class EmaCrossoverParams:
    n_fast: int = 9
    n_slow: int = 21
    stop_mode: str = "swing"            # "swing" | "pct"
    swing_lookback: int = 10            # bars for swing-extreme stop (stop_mode="swing")
    sl_buffer_pct: float = 0.002        # padding beyond the swing extreme
    stop_pct: float = 0.01              # fixed stop distance (stop_mode="pct")
    min_separation_pct: float = 0.0005  # EMA-gap floor to take a cross; 0 disables
    far_target_mult: float = 0.50       # ±50% from entry — unreachable intraday
    entry_start_min: int = 5            # don't enter in the first N min after open
    squareoff_before_close_min: int = 15


class EmaCrossover:
    def __init__(self, security_id: str, params: Optional[EmaCrossoverParams] = None,
                 session: MarketSession = EQUITY):
        """``session`` binds the entry-start open-anchor + EOD square-off to a
        MarketSession profile (defaults to EQUITY → byte-identical legacy 09:15/15:30
        behaviour). Pass session=MCX for the evening commodity session."""
        self.security_id = security_id
        self.p = params or EmaCrossoverParams()
        self.session = session

        # Position view — updated only via notify_fill / notify_flat
        self.position: int = 0
        self.entry_price: float = 0.0

        # Session state (initialised by _reset_session)
        self._session_date: Optional[date] = None
        self._fast_value: Optional[float] = None
        self._fast_count: int = 0
        self._fast_sum: float = 0.0
        self._slow_value: Optional[float] = None
        self._slow_count: int = 0
        self._slow_sum: float = 0.0
        self._prev_fast: Optional[float] = None
        self._prev_slow: Optional[float] = None
        self._swing_highs: deque = deque()
        self._swing_lows: deque = deque()
        self._stop_level: float = 0.0

        # EMA smoothing factors (constant across sessions)
        self._k_fast: float = 2.0 / (self.p.n_fast + 1)
        self._k_slow: float = 2.0 / (self.p.n_slow + 1)

    # ── Caller feedback ───────────────────────────────────────────────────────

    def notify_fill(self, side: str, qty: int, price: float) -> None:
        self.position += qty if side == "BUY" else -qty
        if self.position != 0:
            self.entry_price = price
        else:
            self.entry_price = 0.0

    def notify_flat(self) -> None:
        self.position = 0
        self.entry_price = 0.0

    # ── Core logic ────────────────────────────────────────────────────────────

    def on_tick(
        self,
        now: datetime,
        price: float,
        high: Optional[float] = None,
        low: Optional[float] = None,
        volume: Optional[float] = None,
    ) -> Optional[Decision]:
        """now must be IST. price = bar close; high/low = bar extremes if known."""
        if price <= 0:
            return None

        # Future-skew guard — copy ORB exactly
        wall = datetime.now(IST)
        ref = wall if now.tzinfo is not None else wall.replace(tzinfo=None)
        if now - ref > MAX_FUTURE_SKEW:
            logger.warning(
                "[EMA %s] ignoring future-stamped tick %s (wall %s) — skew > %s",
                self.security_id, now, ref, MAX_FUTURE_SKEW,
            )
            return None

        today = now.date()
        t = now.time()

        if self._session_date != today:
            self._reset_session(today)

        # 1. EOD square-off — UNCONDITIONAL, top of method (no indicator-readiness gate)
        squareoff = (
            datetime.combine(today, self.session.close_time_for(today))
            - timedelta(minutes=self.p.squareoff_before_close_min)
        ).time()
        if t >= squareoff:
            if self.position != 0:
                return Decision(action="EXIT", reason="EOD square-off")
            return None

        # 2. Track swing window (raw high/low or price) for stop — BEFORE EMA update
        self._push_swing(
            high if high is not None else price,
            low if low is not None else price,
        )

        # 3. Update EMAs from the close (= price)
        fast = self._update_ema_fast(price)
        slow = self._update_ema_slow(price)
        if fast is None or slow is None:
            return None  # warm-up: not both seeded yet

        # 4. Need a prior pair to detect a cross
        if self._prev_fast is None:
            self._prev_fast, self._prev_slow = fast, slow
            return None  # first seeded bar: no prior relationship

        bull = self._prev_fast <= self._prev_slow and fast > slow
        bear = self._prev_fast >= self._prev_slow and fast < slow

        # Advance prior pair NOW — consumed/ignored cross must never re-fire next bar
        self._prev_fast, self._prev_slow = fast, slow

        # 5. Exits first (position open)
        if self.position > 0:
            if bear:
                return Decision(action="EXIT", reason="EMA cross-down")
            if price <= self._stop_level:
                return Decision(
                    action="EXIT", reason=f"Stop-loss ₹{self._stop_level:.2f}"
                )
            return None
        if self.position < 0:
            if bull:
                return Decision(action="EXIT", reason="EMA cross-up")
            if price >= self._stop_level:
                return Decision(
                    action="EXIT", reason=f"Stop-loss ₹{self._stop_level:.2f}"
                )
            return None

        # 6. Entries (flat) — respect the open delay
        entry_start = (
            datetime.combine(today, self.session.open_time)
            + timedelta(minutes=self.p.entry_start_min)
        ).time()
        if t < entry_start:
            return None

        # Anti-churn filter: skip marginal crosses in flat, EMAs-glued markets
        if self.p.min_separation_pct > 0 and abs(fast - slow) < price * self.p.min_separation_pct:
            return None

        if bull:
            self._stop_level = self._long_stop(price)
            return Decision(
                action="ENTER",
                side="BUY",
                stop=self._stop_level,
                target=price * (1 + self.p.far_target_mult),
                reason=f"EMA{self.p.n_fast}>EMA{self.p.n_slow} cross-up",
            )
        if bear:
            self._stop_level = self._short_stop(price)
            return Decision(
                action="ENTER",
                side="SELL",
                stop=self._stop_level,
                target=max(price * (1 - self.p.far_target_mult), 0.05),
                reason=f"EMA{self.p.n_fast}<EMA{self.p.n_slow} cross-down",
            )
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _update_ema_fast(self, close: float) -> Optional[float]:
        if self._fast_value is None:
            self._fast_count += 1
            self._fast_sum += close
            if self._fast_count == self.p.n_fast:
                self._fast_value = self._fast_sum / self.p.n_fast
        else:
            self._fast_value = close * self._k_fast + self._fast_value * (1 - self._k_fast)
        return self._fast_value

    def _update_ema_slow(self, close: float) -> Optional[float]:
        if self._slow_value is None:
            self._slow_count += 1
            self._slow_sum += close
            if self._slow_count == self.p.n_slow:
                self._slow_value = self._slow_sum / self.p.n_slow
        else:
            self._slow_value = close * self._k_slow + self._slow_value * (1 - self._k_slow)
        return self._slow_value

    def _push_swing(self, high: float, low: float) -> None:
        """Append to rolling swing window, trimmed to swing_lookback bars."""
        self._swing_highs.append(high)
        self._swing_lows.append(low)
        if len(self._swing_highs) > self.p.swing_lookback:
            self._swing_highs.popleft()
            self._swing_lows.popleft()

    def _long_stop(self, close: float) -> float:
        if self.p.stop_mode == "swing" and self._swing_lows:
            return min(self._swing_lows) * (1 - self.p.sl_buffer_pct)
        return close * (1 - self.p.stop_pct)

    def _short_stop(self, close: float) -> float:
        if self.p.stop_mode == "swing" and self._swing_highs:
            return max(self._swing_highs) * (1 + self.p.sl_buffer_pct)
        return close * (1 + self.p.stop_pct)

    def _reset_session(self, today: date) -> None:
        self._session_date = today
        # Fast EMA state
        self._fast_value = None
        self._fast_count = 0
        self._fast_sum = 0.0
        # Slow EMA state
        self._slow_value = None
        self._slow_count = 0
        self._slow_sum = 0.0
        # Cross detection
        self._prev_fast = None
        self._prev_slow = None
        # Swing window
        self._swing_highs = deque()
        self._swing_lows = deque()
        # Stop level
        self._stop_level = 0.0
        logger.info("[EMA %s] New session %s — reset", self.security_id, today)

    def status(self) -> dict:
        return {
            "security_id": self.security_id,
            "position": self.position,
            "entry_price": self.entry_price,
            "fast_ema": round(self._fast_value, 4) if self._fast_value is not None else None,
            "slow_ema": round(self._slow_value, 4) if self._slow_value is not None else None,
            "stop_level": round(self._stop_level, 2),
        }
