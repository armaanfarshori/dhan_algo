"""
Supertrend — ATR-based trailing-stop trend follower.

Pure, synchronous, IO-free: ticks in, decisions out. Mirrors strategies/orb.py
in structure and contract. See research/backtest/strategy_specs/supertrend.md
for the full spec.

Signal logic:
- When close flips above the Supertrend line (direction +1): go long.
- When close flips below the Supertrend line (direction -1): go short.
- The Supertrend line is the trailing stop (carried as decision.stop at entry).
- No fixed profit target — exit is the opposite direction flip or EOD.
- decision.target is set to a wide sentinel (price ± target_atr_mult * atr)
  so the engine contract is satisfied; it should never be the binding exit.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from strategies.orb import Decision, IST, MARKET_CLOSE, MAX_FUTURE_SKEW

logger = logging.getLogger("dhan.strategy.supertrend")


@dataclass
class SupertrendParams:
    atr_period: int = 10                   # Wilder ATR lookback
    multiplier: float = 3.0               # band width = multiplier * ATR
    target_atr_mult: float = 100.0        # WIDE sentinel target (never the real exit)
    squareoff_before_close_min: int = 15  # EOD flatten lead (matches ORB)
    min_atr_pct: float = 0.0             # optional vol floor; 0.0 = disabled


class Supertrend:
    """
    Supertrend strategy — incremental Wilder ATR + band-locking recurrence.
    One position at a time; EOD square-off is unconditional.
    """

    def __init__(self, security_id: str, params: Optional[SupertrendParams] = None):
        self.security_id = security_id
        self.p = params or SupertrendParams()

        # Session state
        self._session_date: Optional[date] = None
        self._bars_seen: int = 0
        self._tr_seed: list = []          # accumulates TRs for the seed average

        # Indicator state
        self._atr: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._prev_final_upper: Optional[float] = None
        self._prev_final_lower: Optional[float] = None
        self._prev_dir: Optional[int] = None
        self._prev_supertrend: Optional[float] = None

        # Current bar computed values (exposed for tests via status())
        self._direction: Optional[int] = None
        self._supertrend: Optional[float] = None
        self._final_lower: Optional[float] = None
        self._final_upper: Optional[float] = None

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
                high: Optional[float] = None, low: Optional[float] = None,
                volume: Optional[float] = None) -> Optional[Decision]:
        """now must be IST. price = bar close; high/low = current bar extremes."""
        if price <= 0:
            return None

        # Future-skew guard (mirrors ORB lines 88-96 verbatim):
        # A tick stamped further than MAX_FUTURE_SKEW ahead of wall clock is
        # implausible — ignore it; do NOT reset session or update indicators.
        wall = datetime.now(IST)
        ref = wall if now.tzinfo is not None else wall.replace(tzinfo=None)
        if now - ref > MAX_FUTURE_SKEW:
            logger.warning(
                "[ST %s] ignoring future-stamped tick %s (now %s) — skew > %s",
                self.security_id, now, ref, MAX_FUTURE_SKEW,
            )
            return None

        today = now.date()
        t = now.time()

        # Session reset on date change
        if self._session_date != today:
            self._reset_session(today)

        # Step 1: update indicators for this bar (before EOD check so they stay
        # current even in the EOD window — but EOD check itself never reads them).
        h = high if high is not None else price
        lo = low if low is not None else price
        self._update_indicator(price, h, lo)

        # Step 2: EOD square-off — UNCONDITIONAL, sits above warm-up gate
        squareoff = (
            datetime.combine(today, MARKET_CLOSE)
            - timedelta(minutes=self.p.squareoff_before_close_min)
        ).time()
        if t >= squareoff:
            if self.position != 0:
                return Decision(action="EXIT", reason="EOD square-off")
            return None

        # Step 3: warm-up gate — wait until ATR is seeded AND past seed bar
        # (seed bar itself takes no trade; _atr is set on seed bar but _prev_dir
        # is set to +1 as initial direction — we gate on bars_seen > atr_period)
        if self._atr is None or self._bars_seen <= self.p.atr_period:
            return None

        # Step 4: trade logic — direction and line for THIS bar
        direction = self._direction
        line = self._supertrend

        if direction is None or line is None:
            return None

        # Capture the direction from the PREVIOUS bar to detect a flip
        # _prev_dir was updated at the END of _update_indicator before we
        # advanced prev state. We need to track the PRIOR direction separately.
        # We store it before updating in _update_indicator via _prev_dir_before.
        prev_dir = self._prev_dir_before_update  # set inside _update_indicator

        flipped = (direction != prev_dir) if prev_dir is not None else False

        atr = self._atr  # for sentinel target computation

        # Optional vol floor
        if self.p.min_atr_pct > 0.0 and atr < price * self.p.min_atr_pct:
            return None

        # ── Flat: enter on flip ────────────────────────────────────────────
        if self.position == 0:
            if flipped:
                if direction == 1:
                    # Long flip: Supertrend line = final_lower, below price
                    target = price + self.p.target_atr_mult * atr
                    return Decision(
                        action="ENTER", side="BUY",
                        stop=line,
                        target=target,
                        reason=f"ST long flip line={line:.2f}",
                    )
                else:
                    # Short flip: Supertrend line = final_upper, above price
                    target = price - self.p.target_atr_mult * atr
                    return Decision(
                        action="ENTER", side="SELL",
                        stop=line,
                        target=target,
                        reason=f"ST short flip line={line:.2f}",
                    )

        # ── Long: exit on opposite flip or close below line ───────────────
        elif self.position > 0:
            if direction == -1 or price < line:
                return Decision(
                    action="EXIT",
                    reason=f"ST flip down / stop line={line:.2f}",
                )

        # ── Short: exit on opposite flip or close above line ─────────────
        elif self.position < 0:
            if direction == 1 or price > line:
                return Decision(
                    action="EXIT",
                    reason=f"ST flip up / stop line={line:.2f}",
                )

        return None

    # ── Indicator update (incremental, causal) ────────────────────────────────

    def _update_indicator(self, close: float, high: float, low: float):
        """Compute TR, ATR (Wilder), basic/final bands, direction, Supertrend."""
        self._bars_seen += 1

        # True Range
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )

        # ATR — Wilder smoothing, seeded on bar atr_period
        if self._bars_seen < self.p.atr_period:
            self._tr_seed.append(tr)
            self._atr = None
            # No bands or direction yet; save prev state and return
            self._prev_dir_before_update = None
            self._prev_close = close
            return
        elif self._bars_seen == self.p.atr_period:
            self._tr_seed.append(tr)
            atr = sum(self._tr_seed) / len(self._tr_seed)
        else:
            # self._atr is defined from the seed bar onward
            atr = (self._atr * (self.p.atr_period - 1) + tr) / self.p.atr_period

        # Basic bands
        hl2 = (high + low) / 2.0
        basic_upper = hl2 + self.p.multiplier * atr
        basic_lower = hl2 - self.p.multiplier * atr

        # Final bands — band-locking recurrence
        if self._prev_final_upper is None:
            # Seed bar: initialise directly
            final_upper = basic_upper
            final_lower = basic_lower
        else:
            prev_fu = self._prev_final_upper
            prev_fl = self._prev_final_lower
            pc = self._prev_close  # previous bar's close (guaranteed non-None here)

            # Upper band tightens downward; locked if prior close was below it
            if basic_upper < prev_fu or pc > prev_fu:
                final_upper = basic_upper
            else:
                final_upper = prev_fu

            # Lower band tightens upward; locked if prior close was above it
            if basic_lower > prev_fl or pc < prev_fl:
                final_lower = basic_lower
            else:
                final_lower = prev_fl

        # Direction + Supertrend line
        # Save the direction that was in effect BEFORE this bar (for flip detection)
        self._prev_dir_before_update = self._prev_dir

        if self._prev_dir is None:
            # Seed bar: initialise direction to +1 (conventional default)
            direction = 1
            supertrend = final_lower
        else:
            prev_dir = self._prev_dir
            if prev_dir == 1:
                direction = -1 if close < final_lower else 1
            else:  # prev_dir == -1
                direction = 1 if close > final_upper else -1
            supertrend = final_lower if direction == 1 else final_upper

        # Advance all state
        self._atr = atr
        self._prev_close = close
        self._prev_final_upper = final_upper
        self._prev_final_lower = final_lower
        self._prev_dir = direction
        self._prev_supertrend = supertrend

        # Expose current bar values
        self._direction = direction
        self._supertrend = supertrend
        self._final_lower = final_lower
        self._final_upper = final_upper

    # ── Session reset ─────────────────────────────────────────────────────────

    def _reset_session(self, today: date):
        """Reset all intraday indicator state on a date change. Position is NOT reset."""
        self._session_date = today
        self._bars_seen = 0
        self._tr_seed = []
        self._atr = None
        self._prev_close = None
        self._prev_final_upper = None
        self._prev_final_lower = None
        self._prev_dir = None
        self._prev_supertrend = None
        self._direction = None
        self._supertrend = None
        self._final_lower = None
        self._final_upper = None
        self._prev_dir_before_update = None
        logger.info("[ST %s] New session %s — reset", self.security_id, today)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "security_id": self.security_id,
            "bars_seen": self._bars_seen,
            "atr": round(self._atr, 4) if self._atr is not None else None,
            "direction": self._direction,
            "supertrend": round(self._supertrend, 2) if self._supertrend is not None else None,
            "final_lower": round(self._final_lower, 2) if self._final_lower is not None else None,
            "final_upper": round(self._final_upper, 2) if self._final_upper is not None else None,
            "position": self.position,
            "entry_price": self.entry_price,
        }
