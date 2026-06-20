"""
RSI(2) Mean-Reversion (Connors-style) — pure signal logic.

Buy short-term oversold dips while the longer trend is up; sell overbought spikes
while the trend is down.  Exit on RSI snap-back, protective stop, or EOD square-off.

Synchronous and IO-free: ticks in, decisions out.  Mirrors strategies/orb.py exactly.
"""
import logging
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

# Reuse the shared market-session constants from ORB so they never drift.
from strategies.orb import (  # noqa: F401 — reuse, do not redefine
    IST,
    MARKET_CLOSE,
    MARKET_OPEN,
    MAX_FUTURE_SKEW,
    Decision,
)

logger = logging.getLogger("dhan.strategy.rsi2_meanrev")


@dataclass
class Rsi2MeanRevParams:
    rsi_period: int = 2
    rsi_oversold: float = 10.0
    rsi_overbought: float = 90.0
    rsi_exit_long: float = 65.0
    rsi_exit_short: float = 35.0
    trend_period: int = 50
    stop_pct: float = 0.006
    target_pct: float = 0.0
    enable_short: bool = True
    squareoff_before_close_min: int = 15
    min_price: float = 1.0


class Rsi2MeanRev:
    def __init__(self, security_id: str, params: Optional[Rsi2MeanRevParams] = None):
        self.security_id = security_id
        self.p = params or Rsi2MeanRevParams()

        self._session_date: Optional[date] = None

        # RSI(2) Wilder state
        self.prev_close: Optional[float] = None
        self.avg_gain: float = 0.0
        self.avg_loss: float = 0.0
        self._delta_count: int = 0
        self._seed_gain: float = 0.0
        self._seed_loss: float = 0.0
        self.rsi: Optional[float] = None

        # Trend filter SMA state
        self.closes: deque = deque(maxlen=self.p.trend_period)
        self.close_sum: float = 0.0
        self.sma: Optional[float] = None

        # Position state — updated only via notify_fill/notify_flat
        self.position: int = 0
        self.entry_price: float = 0.0

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
        """now must be IST. price = bar close; high/low = current bar extremes."""

        # 0a. Basic guard
        if price <= 0 or price < self.p.min_price:
            return None

        # 0b. Future-skew guard — copy from ORB verbatim
        wall = datetime.now(IST)
        ref = wall if now.tzinfo is not None else wall.replace(tzinfo=None)
        if now - ref > MAX_FUTURE_SKEW:
            logger.warning(
                "[RSI2 %s] ignoring future-stamped tick %s (now %s) — skew > %s",
                self.security_id, now, ref, MAX_FUTURE_SKEW,
            )
            return None

        today = now.date()
        t = now.time()

        # 1. Session reset
        if self._session_date != today:
            self._reset_session(today)

        # 2. Update indicators
        self._update_indicators(price)

        # 3. EOD square-off — unconditional, ABOVE every readiness gate
        squareoff = (
            datetime.combine(today, MARKET_CLOSE)
            - timedelta(minutes=self.p.squareoff_before_close_min)
        ).time()
        if t >= squareoff:
            if self.position != 0:
                return Decision(action="EXIT", reason="EOD square-off")
            return None

        # 4. Warm-up gate
        if self.rsi is None or self.sma is None:
            return None

        rsi = self.rsi
        sma = self.sma

        # 5. Manage an open position (exits)
        if self.position > 0:
            stop = self.entry_price * (1 - self.p.stop_pct)
            if self.p.target_pct > 0:
                target = self.entry_price * (1 + self.p.target_pct)
                if price >= target:
                    return Decision(
                        action="EXIT", reason=f"Target hit ₹{target:.2f}"
                    )
            if price <= stop:
                return Decision(
                    action="EXIT", reason=f"Stop-loss ₹{stop:.2f}"
                )
            if rsi >= self.p.rsi_exit_long:
                return Decision(
                    action="EXIT", reason=f"RSI exit {rsi:.1f}"
                )
            return None

        if self.position < 0:
            stop = self.entry_price * (1 + self.p.stop_pct)
            if self.p.target_pct > 0:
                target = self.entry_price * (1 - self.p.target_pct)
                if price <= target:
                    return Decision(
                        action="EXIT", reason=f"Target hit ₹{target:.2f}"
                    )
            if price >= stop:
                return Decision(
                    action="EXIT", reason=f"Stop-loss ₹{stop:.2f}"
                )
            if rsi <= self.p.rsi_exit_short:
                return Decision(
                    action="EXIT", reason=f"RSI exit {rsi:.1f}"
                )
            return None

        # 6. Entries (only when flat)
        # LONG: oversold dip in uptrend
        if rsi <= self.p.rsi_oversold and price > sma:
            stop = price * (1 - self.p.stop_pct)
            if self.p.target_pct > 0:
                target = price * (1 + self.p.target_pct)
            else:
                target = price * (1 + 10 * self.p.stop_pct)
            return Decision(
                action="ENTER",
                side="BUY",
                stop=stop,
                target=target,
                reason=f"RSI2 long: rsi={rsi:.1f} < {self.p.rsi_oversold} & px>{sma:.2f}",
            )

        # SHORT: overbought spike in downtrend
        if self.p.enable_short and rsi >= self.p.rsi_overbought and price < sma:
            stop = price * (1 + self.p.stop_pct)
            if self.p.target_pct > 0:
                target = price * (1 - self.p.target_pct)
            else:
                target = price * (1 - 10 * self.p.stop_pct)
            return Decision(
                action="ENTER",
                side="SELL",
                stop=stop,
                target=target,
                reason=f"RSI2 short: rsi={rsi:.1f} > {self.p.rsi_overbought} & px<{sma:.2f}",
            )

        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _update_indicators(self, c: float) -> None:
        """Update RSI(2) Wilder and SMA(trend_period) with bar close c."""
        n = self.p.rsi_period

        # --- SMA (trend filter) ---
        if len(self.closes) == self.p.trend_period:
            self.close_sum -= self.closes[0]
        self.closes.append(c)
        self.close_sum += c
        if len(self.closes) == self.p.trend_period:
            self.sma = self.close_sum / self.p.trend_period

        # --- RSI Wilder ---
        if self.prev_close is None:
            # First bar of session: seed prev_close only, no delta yet
            self.prev_close = c
            return

        delta = c - self.prev_close
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        self._delta_count += 1

        if self._delta_count <= n:
            # Seeding phase: accumulate
            self._seed_gain += gain
            self._seed_loss += loss
            if self._delta_count == n:
                # Seed complete — set initial averages
                self.avg_gain = self._seed_gain / n
                self.avg_loss = self._seed_loss / n
                self.rsi = self._compute_rsi()
        else:
            # Steady-state Wilder recurrence
            self.avg_gain = (self.avg_gain * (n - 1) + gain) / n
            self.avg_loss = (self.avg_loss * (n - 1) + loss) / n
            self.rsi = self._compute_rsi()

        self.prev_close = c

    def _compute_rsi(self) -> float:
        if self.avg_gain == 0.0 and self.avg_loss == 0.0:
            return 50.0
        if self.avg_loss == 0.0:
            return 100.0
        if self.avg_gain == 0.0:
            return 0.0
        rs = self.avg_gain / self.avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def _reset_session(self, today: date) -> None:
        self._session_date = today

        # RSI state
        self.prev_close = None
        self.avg_gain = 0.0
        self.avg_loss = 0.0
        self._delta_count = 0
        self._seed_gain = 0.0
        self._seed_loss = 0.0
        self.rsi = None

        # SMA state
        self.closes = deque(maxlen=self.p.trend_period)
        self.close_sum = 0.0
        self.sma = None

        logger.info("[RSI2 %s] New session %s — reset", self.security_id, today)

    def status(self) -> dict:
        return {
            "security_id": self.security_id,
            "rsi": round(self.rsi, 2) if self.rsi is not None else None,
            "sma": round(self.sma, 2) if self.sma is not None else None,
            "position": self.position,
            "entry_price": self.entry_price,
        }
