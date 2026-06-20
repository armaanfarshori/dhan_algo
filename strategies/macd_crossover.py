"""
MACD Crossover — pure signal logic.

Trend-following intraday strategy on 1-minute bars. Enters long when the MACD
line (fast EMA − slow EMA) crosses above the signal line, and short when it
crosses below. Primary exit is the reverse cross; a Wilder ATR-based protective
stop backstops it; EOD square-off is unconditional.

This class is synchronous and IO-free: ticks in, decisions out.
The same code runs live (driven by StrategyRunner) and in the backtester
(driven by bar replay).

See research/backtest/strategy_specs/macd_crossover.md for the full spec.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional

from core.sessions import EQUITY, MarketSession
from strategies.orb import Decision, IST, MAX_FUTURE_SKEW

logger = logging.getLogger("dhan.strategy.macd_crossover")

MARKET_OPEN = dtime(9, 15)


@dataclass
class MacdCrossoverParams:
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
    atr_period: int = 14
    atr_mult: float = 1.5          # protective stop = atr_mult × ATR from entry close
    reward_mult: float = 1.5       # target = reward_mult × stop-distance (R-multiple)
    stop_pct: float = 0.01         # fallback stop if ATR not ready (1% of price)
    require_zero_filter: bool = False  # if True, long needs macd>0, short needs macd<0
    squareoff_before_close_min: int = 15


class MacdCrossover:
    def __init__(self, security_id: str, params: Optional[MacdCrossoverParams] = None,
                 session: MarketSession = EQUITY):
        """``session`` binds the EOD square-off to a MarketSession profile (defaults
        to EQUITY → byte-identical legacy 15:30 behaviour). Pass session=MCX for the
        evening commodity session (DST-aware close)."""
        self.security_id = security_id
        self.p = params or MacdCrossoverParams()
        self.session = session

        assert self.p.slow_period > self.p.fast_period, (
            f"slow_period ({self.p.slow_period}) must be > fast_period ({self.p.fast_period})"
        )
        assert self.p.fast_period >= 2 and self.p.slow_period >= 2 and self.p.signal_period >= 2, (
            "all periods must be >= 2"
        )

        self._session_date: Optional[date] = None

        # EMA / MACD state — initialised in _reset_session
        self._close_buf: list[float] = []
        self.ema_fast: Optional[float] = None
        self.ema_slow: Optional[float] = None
        self._ema_seeded: bool = False

        self._macd_buf: list[float] = []
        self.signal: Optional[float] = None
        self._signal_seeded: bool = False
        self.prev_hist: Optional[float] = None

        # ATR state
        self._tr_buf: list[float] = []
        self.atr: Optional[float] = None
        self._prev_close: Optional[float] = None

        # Pending stop/target stash — filled when we emit an ENTER, consumed in notify_fill
        self._pending_stop: float = 0.0
        self._pending_target: float = 0.0

        # Position view — updated ONLY via notify_fill / notify_flat
        self.position: int = 0
        self.entry_price: float = 0.0
        self._stop: float = 0.0    # stored protective stop for the open position
        self._target: float = 0.0  # stored target for the open position

    # ── Caller feedback ───────────────────────────────────────────────────────

    def notify_fill(self, side: str, qty: int, price: float) -> None:
        self.position += qty if side == "BUY" else -qty
        if self.position != 0:
            self.entry_price = price
            # Absorb the pending stop/target that were stashed when ENTER was emitted
            self._stop = self._pending_stop
            self._target = self._pending_target
        else:
            self.entry_price = 0.0
        self._pending_stop = 0.0
        self._pending_target = 0.0

    def notify_flat(self) -> None:
        self.position = 0
        self.entry_price = 0.0
        self._stop = 0.0
        self._target = 0.0

    # ── Core logic ────────────────────────────────────────────────────────────

    def on_tick(
        self,
        now: datetime,
        price: float,
        high: Optional[float] = None,
        low: Optional[float] = None,
        volume: Optional[float] = None,  # accepted but unused (contract parity)
    ) -> Optional[Decision]:
        """now must be IST. price = bar close; high/low = current bar extremes."""
        # 1. Sanity
        if price <= 0:
            return None

        # 2. Future-skew guard (copied from ORB verbatim)
        wall = datetime.now(IST)
        ref = wall if now.tzinfo is not None else wall.replace(tzinfo=None)
        if now - ref > MAX_FUTURE_SKEW:
            logger.warning(
                "[MACD %s] ignoring future-stamped tick %s (now %s) — skew > %s",
                self.security_id, now, ref, MAX_FUTURE_SKEW,
            )
            return None

        # 3. Session reset on date change
        today = now.date()
        if self._session_date != today:
            self._reset_session(today)

        t = now.time()

        # 4. Update indicators with THIS bar
        #    Snapshot prev_hist BEFORE advancing indicators
        prev_hist_snap = self.prev_hist

        h = high if high is not None else price
        l = low if low is not None else price

        self._update_atr(price, h, l)
        macd_line, hist = self._update_ema_macd(price)

        # Advance prev_hist once we have a valid histogram
        if hist is not None:
            self.prev_hist = hist

        # prev_close update always
        self._prev_close = price

        # 5. EOD square-off (unconditional — indicator-independent)
        squareoff_time = (
            datetime.combine(today, self.session.close_time_for(today))
            - timedelta(minutes=self.p.squareoff_before_close_min)
        ).time()
        if t >= squareoff_time:
            if self.position != 0:
                return Decision(action="EXIT", reason="EOD square-off")
            return None

        # 6. Exit checks for open positions
        if self.position != 0:
            # Crossover detection requires two consecutive histograms
            cross_up, cross_down = self._detect_cross(prev_hist_snap, hist)

            # 6a. Signal exit — opposite crossover
            if self.position > 0 and cross_down:
                return Decision(action="EXIT", reason="MACD cross down (exit long)")
            if self.position < 0 and cross_up:
                return Decision(action="EXIT", reason="MACD cross up (exit short)")

            # 6b. Stored stop/target close backstop (defense-in-depth)
            if self._stop != 0.0 or self._target != 0.0:
                if self.position > 0:
                    if price <= self._stop:
                        return Decision(action="EXIT",
                                        reason=f"Stop-loss ₹{self._stop:.2f}")
                    if price >= self._target:
                        return Decision(action="EXIT",
                                        reason=f"Target hit ₹{self._target:.2f}")
                else:  # short
                    if price >= self._stop:
                        return Decision(action="EXIT",
                                        reason=f"Stop-loss ₹{self._stop:.2f}")
                    if price <= self._target:
                        return Decision(action="EXIT",
                                        reason=f"Target hit ₹{self._target:.2f}")

        # 7. Entry (only when flat and indicators ready)
        if self.position == 0 and self._indicators_ready(prev_hist_snap, hist):
            cross_up, cross_down = self._detect_cross(prev_hist_snap, hist)

            if cross_up:
                if not self.p.require_zero_filter or (macd_line is not None and macd_line > 0):
                    stop, target = self._compute_levels_long(price)
                    if stop is None:
                        return None  # degenerate stop — skip
                    self._pending_stop = stop
                    self._pending_target = target
                    return Decision(
                        action="ENTER", side="BUY", stop=stop, target=target,
                        reason=f"MACD cross up  macd={macd_line:.3f} sig={self.signal:.3f}",
                    )

            if cross_down:
                if not self.p.require_zero_filter or (macd_line is not None and macd_line < 0):
                    stop, target = self._compute_levels_short(price)
                    if stop is None:
                        return None
                    self._pending_stop = stop
                    self._pending_target = target
                    return Decision(
                        action="ENTER", side="SELL", stop=stop, target=target,
                        reason=f"MACD cross down  macd={macd_line:.3f} sig={self.signal:.3f}",
                    )

        return None

    # ── Indicator helpers ─────────────────────────────────────────────────────

    def _update_atr(self, close: float, high: float, low: float) -> None:
        """Advance Wilder ATR incrementally."""
        if self._prev_close is None:
            # First bar of session: no prev_close, TR = high − low
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )

        if self.atr is None:
            self._tr_buf.append(tr)
            if len(self._tr_buf) >= self.p.atr_period:
                self.atr = sum(self._tr_buf) / len(self._tr_buf)
                self._tr_buf = []  # no longer needed
        else:
            n = self.p.atr_period
            self.atr = (self.atr * (n - 1) + tr) / n

    def _update_ema_macd(self, close: float):
        """
        Advance fast/slow EMA, MACD line, signal EMA, and histogram.

        Returns (macd_line, hist) — both None while still warming up.
        """
        alpha_fast = 2.0 / (self.p.fast_period + 1)
        alpha_slow = 2.0 / (self.p.slow_period + 1)
        alpha_sig = 2.0 / (self.p.signal_period + 1)

        if not self._ema_seeded:
            self._close_buf.append(close)
            if len(self._close_buf) >= self.p.slow_period:
                # Seed slow EMA from SMA of all slow_period closes
                self.ema_slow = sum(self._close_buf) / len(self._close_buf)
                # Seed fast EMA from SMA of last fast_period closes
                self.ema_fast = (
                    sum(self._close_buf[-self.p.fast_period:]) / self.p.fast_period
                )
                self._ema_seeded = True
                self._close_buf = []  # no longer needed
                # Compute and buffer the first MACD line value (bar 26, 1-based)
                macd_line = self.ema_fast - self.ema_slow
                self._macd_buf.append(macd_line)
                # Signal still needs more values — fall through below
                if len(self._macd_buf) >= self.p.signal_period:
                    self.signal = sum(self._macd_buf) / len(self._macd_buf)
                    hist = macd_line - self.signal
                    self._signal_seeded = True
                    self._macd_buf = []
                    return macd_line, hist
                return macd_line, None
            return None, None

        # Advance both EMAs
        self.ema_fast = alpha_fast * close + (1 - alpha_fast) * self.ema_fast
        self.ema_slow = alpha_slow * close + (1 - alpha_slow) * self.ema_slow
        macd_line = self.ema_fast - self.ema_slow

        if not self._signal_seeded:
            self._macd_buf.append(macd_line)
            if len(self._macd_buf) >= self.p.signal_period:
                self.signal = sum(self._macd_buf) / len(self._macd_buf)
                hist = macd_line - self.signal
                self._signal_seeded = True
                self._macd_buf = []  # no longer needed
                return macd_line, hist
            return macd_line, None

        # Advance signal EMA
        self.signal = alpha_sig * macd_line + (1 - alpha_sig) * self.signal
        hist = macd_line - self.signal
        return macd_line, hist

    def _indicators_ready(self, prev_hist: Optional[float], hist: Optional[float]) -> bool:
        """True when we have both a current and previous histogram (crossover-eligible)."""
        return prev_hist is not None and hist is not None

    def _detect_cross(
        self, prev_hist: Optional[float], hist: Optional[float]
    ):
        """Returns (cross_up, cross_down) booleans."""
        if prev_hist is None or hist is None:
            return False, False
        cross_up = prev_hist <= 0 and hist > 0
        cross_down = prev_hist >= 0 and hist < 0
        return cross_up, cross_down

    def _compute_levels_long(self, price: float):
        """Compute (stop, target) for a long entry. Returns (None, None) on degenerate stop."""
        if self.atr is not None and self.atr > 0:
            stop_dist = self.p.atr_mult * self.atr
        else:
            stop_dist = self.p.stop_pct * price

        if stop_dist <= 0:
            return None, None

        stop = price - stop_dist
        target = price + self.p.reward_mult * stop_dist

        if stop >= price:  # degenerate
            return None, None
        return stop, target

    def _compute_levels_short(self, price: float):
        """Compute (stop, target) for a short entry. Returns (None, None) on degenerate stop."""
        if self.atr is not None and self.atr > 0:
            stop_dist = self.p.atr_mult * self.atr
        else:
            stop_dist = self.p.stop_pct * price

        if stop_dist <= 0:
            return None, None

        stop = price + stop_dist
        target = price - self.p.reward_mult * stop_dist
        # No degenerate-stop guard needed here: stop_dist > 0 is enforced above,
        # so stop = price + stop_dist is always strictly > price for a short.
        return stop, target

    # ── Session reset ─────────────────────────────────────────────────────────

    def _reset_session(self, today: date) -> None:
        """Wipe all intraday indicator state on date change. Does NOT touch position."""
        self._session_date = today

        self._close_buf = []
        self.ema_fast = None
        self.ema_slow = None
        self._ema_seeded = False

        self._macd_buf = []
        self.signal = None
        self._signal_seeded = False
        self.prev_hist = None

        self._tr_buf = []
        self.atr = None
        self._prev_close = None

        logger.info("[MACD %s] New session %s — reset", self.security_id, today)

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "security_id": self.security_id,
            "position": self.position,
            "entry_price": self.entry_price,
            "ema_fast": round(self.ema_fast, 4) if self.ema_fast is not None else None,
            "ema_slow": round(self.ema_slow, 4) if self.ema_slow is not None else None,
            "signal": round(self.signal, 4) if self.signal is not None else None,
            "atr": round(self.atr, 4) if self.atr is not None else None,
            "prev_hist": self.prev_hist,
            "stop": self._stop,
            "target": self._target,
        }
