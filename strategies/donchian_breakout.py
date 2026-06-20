"""
Donchian Channel Breakout — turtle-style, intraday.

Pure synchronous, IO-free class. Ticks in, decisions out.
Same session-reset / EOD / future-skew / one-attempt-per-side contract as ORB.

Spec: research/backtest/strategy_specs/donchian_breakout.md
"""
import logging
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from core.sessions import EQUITY, MarketSession
from strategies.orb import Decision, IST, MAX_FUTURE_SKEW

logger = logging.getLogger("dhan.strategy.donchian")


@dataclass
class DonchianBreakoutParams:
    channel_period: int = 20          # N — bars in the Donchian channel
    exclude_current_bar: bool = True   # True: channel = last N *completed* bars;
                                       # False: include the current bar
    warmup_skip_open_min: int = 15     # block entries until this many min after MARKET_OPEN
    atr_period: int = 14               # Wilder ATR window
    sl_atr_mult: float = 1.5          # stop-cap distance = sl_atr_mult × ATR
    sl_buffer_pct: float = 0.001      # extra padding beyond the channel-edge stop
    target_r_multiple: float = 2.0    # target = entry ± target_r_multiple × risk
    min_channel_pct: float = 0.003    # skip if channel width < this fraction of price
    squareoff_before_close_min: int = 15


class DonchianBreakout:
    """
    Rolling Donchian channel breakout strategy.

    Channel maintained via O(N) deques of raw highs/lows (maxlen=N).
    ATR uses Wilder smoothing with a simple-average seed.

    Position state is fed back exclusively via notify_fill / notify_flat.
    """

    def __init__(self, security_id: str,
                 params: Optional[DonchianBreakoutParams] = None,
                 session: MarketSession = EQUITY):
        """``session`` binds the warm-up open-anchor + EOD square-off to a
        MarketSession profile (defaults to EQUITY → byte-identical legacy 09:15/15:30
        behaviour). Pass session=MCX for the evening commodity session."""
        self.security_id = security_id
        self.p = params or DonchianBreakoutParams()
        self.session = session

        # Position view — ONLY updated by notify_fill / notify_flat
        self.position: int = 0
        self.entry_price: float = 0.0

        # Stored levels of the open position (for close-based exit checks)
        self._stop: float = 0.0
        self._target: float = 0.0

        # Session state (reset in _reset_session)
        self._session_date: Optional[date] = None
        self._i: int = 0                          # bars seen this session

        # Rolling channel — simple deque with maxlen (O(N), N ≤ 40)
        N = self.p.channel_period
        self._hi_buf: deque = deque(maxlen=N)     # highs of the last N bars
        self._lo_buf: deque = deque(maxlen=N)     # lows  of the last N bars

        # ATR (Wilder) state
        self._prev_close: Optional[float] = None
        self._atr: float = 0.0
        self._atr_count: int = 0
        self._atr_seed_sum: float = 0.0           # accumulator for the seed average

        # One-attempt-per-side flags
        self._long_tried: bool = False
        self._short_tried: bool = False

    # ── Caller feedback ──────────────────────────────────────────────────────

    def notify_fill(self, side: str, qty: int, price: float) -> None:
        self.position += qty if side == "BUY" else -qty
        if self.position != 0:
            self.entry_price = price
        else:
            self.entry_price = 0.0

    def notify_flat(self) -> None:
        self.position = 0
        self.entry_price = 0.0
        self._stop = 0.0
        self._target = 0.0

    # ── Core logic ───────────────────────────────────────────────────────────

    def on_tick(self, now: datetime, price: float,
                high: Optional[float] = None,
                low: Optional[float] = None,
                volume: Optional[float] = None) -> Optional[Decision]:
        """
        now must be IST (naive or aware).
        price = bar close; high/low = current bar extremes.
        """
        # 1. Sanity check
        if price <= 0:
            return None

        # 2. Future-skew guard (verbatim from ORB)
        wall = datetime.now(IST)
        ref = wall if now.tzinfo is not None else wall.replace(tzinfo=None)
        if now - ref > MAX_FUTURE_SKEW:
            logger.warning(
                "[Donchian %s] ignoring future-stamped tick %s (now %s) — skew > %s",
                self.security_id, now, ref, MAX_FUTURE_SKEW,
            )
            return None

        # 3. Session reset on date change
        today = now.date()
        if self._session_date != today:
            self._reset_session(today)

        t = now.time()

        # Default high/low to close if not supplied
        bar_high = high if high is not None else price
        bar_low = low if low is not None else price

        # 4. Update rolling channel + ATR (ALWAYS — even pre-warm-up)
        #    When exclude_current_bar=True: read channel BEFORE pushing this bar.
        #    When exclude_current_bar=False: push first, then read.
        if self.p.exclude_current_bar:
            upper, lower = self._channel()
            self._push_bar(bar_high, bar_low, price)
        else:
            self._push_bar(bar_high, bar_low, price)
            upper, lower = self._channel()

        # 5. EOD square-off — unconditional; must NOT depend on channel ready
        squareoff = (
            datetime.combine(today, self.session.close_time_for(today))
            - timedelta(minutes=self.p.squareoff_before_close_min)
        ).time()
        if t >= squareoff:
            if self.position != 0:
                return Decision(action="EXIT", reason="EOD square-off")
            return None

        # 6. Exits for the open position (before entry checks)
        if self.position > 0:
            # Close-based stop / target (belt-and-suspenders; engine wick check is primary)
            if self._stop > 0 and price <= self._stop:
                return Decision(action="EXIT",
                                reason=f"Stop-loss ₹{self._stop:.2f}")
            if self._target > 0 and price >= self._target:
                return Decision(action="EXIT",
                                reason=f"Target hit ₹{self._target:.2f}")
            # Opposite-channel signal exit (turtle exit)
            if lower is not None and price < lower:
                return Decision(action="EXIT", reason="Opposite channel break")
            return None

        if self.position < 0:
            if self._stop > 0 and price >= self._stop:
                return Decision(action="EXIT",
                                reason=f"Stop-loss ₹{self._stop:.2f}")
            if self._target > 0 and price <= self._target:
                return Decision(action="EXIT",
                                reason=f"Target hit ₹{self._target:.2f}")
            if upper is not None and price > upper:
                return Decision(action="EXIT", reason="Opposite channel break")
            return None

        # 7. Warm-up / armed gate
        warm_until = (
            datetime.combine(today, self.session.open_time)
            + timedelta(minutes=self.p.warmup_skip_open_min)
        ).time()
        channel_ready = self._channel_ready()
        if not channel_ready or t < warm_until:
            return None

        # Defend against None channel (shouldn't happen when ready, but be safe)
        if upper is None or lower is None:
            return None

        # 8. Chop guard
        channel_width = upper - lower
        if channel_width < self.p.min_channel_pct * price:
            return None

        # 9. ATR for stop cap (may be None during very short sessions)
        atr = self._atr if self._atr_count >= self.p.atr_period else None

        # 9a. LONG entry
        if not self._long_tried and price > upper:
            self._long_tried = True
            stop_raw = lower * (1 - self.p.sl_buffer_pct)
            if atr is not None and atr > 0:
                stop_cap = price - self.p.sl_atr_mult * atr
                stop = max(stop_raw, stop_cap)   # LONG: higher stop = smaller risk
            else:
                stop = stop_raw
            risk = price - stop
            if risk <= 0:
                return None
            target = price + self.p.target_r_multiple * risk
            self._stop = stop
            self._target = target
            return Decision(
                action="ENTER", side="BUY",
                stop=stop, target=target,
                reason=(
                    f"Donchian long breakout  "
                    f"UP={upper:.2f} LO={lower:.2f} N={self.p.channel_period}"
                ),
            )

        # 9b. SHORT entry
        if not self._short_tried and price < lower:
            self._short_tried = True
            stop_raw = upper * (1 + self.p.sl_buffer_pct)
            if atr is not None and atr > 0:
                stop_cap = price + self.p.sl_atr_mult * atr
                stop = min(stop_raw, stop_cap)   # SHORT: lower stop = smaller risk
            else:
                stop = stop_raw
            risk = stop - price
            if risk <= 0:
                return None
            target = price - self.p.target_r_multiple * risk
            self._stop = stop
            self._target = target
            return Decision(
                action="ENTER", side="SELL",
                stop=stop, target=target,
                reason=(
                    f"Donchian short breakdown  "
                    f"UP={upper:.2f} LO={lower:.2f} N={self.p.channel_period}"
                ),
            )

        return None

    # ── Indicator helpers ────────────────────────────────────────────────────

    def _push_bar(self, bar_high: float, bar_low: float, close: float) -> None:
        """Push one completed bar into the rolling channel + ATR."""
        self._hi_buf.append(bar_high)
        self._lo_buf.append(bar_low)
        self._i += 1

        # ATR (Wilder)
        if self._prev_close is None:
            tr = bar_high - bar_low
        else:
            tr = max(
                bar_high - bar_low,
                abs(bar_high - self._prev_close),
                abs(bar_low - self._prev_close),
            )
        self._atr_count += 1
        if self._atr_count <= self.p.atr_period:
            # Seed phase — accumulate simple average
            self._atr_seed_sum += tr
            if self._atr_count == self.p.atr_period:
                self._atr = self._atr_seed_sum / self.p.atr_period
        else:
            # Wilder smoothing
            k = self.p.atr_period
            self._atr = (self._atr * (k - 1) + tr) / k

        self._prev_close = close

    def _channel(self):
        """Return (upper, lower) from the current buffer contents, or (None, None)."""
        if not self._hi_buf or not self._lo_buf:
            return None, None
        return max(self._hi_buf), min(self._lo_buf)

    def _channel_ready(self) -> bool:
        """True when ≥ channel_period bars have been pushed this session."""
        return self._i >= self.p.channel_period

    # ── Introspection (for tests and status) ────────────────────────────────

    def upper_channel(self) -> Optional[float]:
        """Public accessor: current upper channel value (for tests)."""
        if not self._hi_buf:
            return None
        return max(self._hi_buf)

    def lower_channel(self) -> Optional[float]:
        """Public accessor: current lower channel value (for tests)."""
        if not self._lo_buf:
            return None
        return min(self._lo_buf)

    def status(self) -> dict:
        upper, lower = self._channel()
        return {
            "security_id": self.security_id,
            "session_date": str(self._session_date),
            "bars_seen": self._i,
            "channel_ready": self._channel_ready(),
            "upper_channel": round(upper, 2) if upper is not None else None,
            "lower_channel": round(lower, 2) if lower is not None else None,
            "atr": round(self._atr, 4),
            "atr_ready": self._atr_count >= self.p.atr_period,
            "position": self.position,
            "entry_price": self.entry_price,
            "stop": self._stop,
            "target": self._target,
            "long_tried": self._long_tried,
            "short_tried": self._short_tried,
        }

    # ── Session management ───────────────────────────────────────────────────

    def _reset_session(self, today: date) -> None:
        """Reset all intraday state. Does NOT touch position/entry_price/_stop/_target."""
        self._session_date = today
        self._i = 0
        N = self.p.channel_period
        self._hi_buf = deque(maxlen=N)
        self._lo_buf = deque(maxlen=N)
        self._prev_close = None
        self._atr = 0.0
        self._atr_count = 0
        self._atr_seed_sum = 0.0
        self._long_tried = False
        self._short_tried = False
        logger.info("[Donchian %s] New session %s — reset", self.security_id, today)
