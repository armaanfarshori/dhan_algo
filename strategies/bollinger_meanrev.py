"""
Bollinger Band Mean-Reversion — pure signal logic.

Fades the edges of a rolling Bollinger Band back to the mean: go LONG when
close pierces the lower band, SHORT when it pierces the upper band, and take
profit at the middle band (the SMA).  A protective stop sits a configurable
distance beyond the triggering band (band_pct or ATR-based).

Indicator update ordering (standard Bollinger approach):
  1. Compute bands from the PREVIOUS N closes (already in the deque).
  2. Check signal against those bands.
  3. THEN append the current close to the deque (evicting the oldest if full).
This ensures an entry fires when the bar close pierces a band established by
prior bars — a self-referential check (close vs band-including-itself) would
be mathematically impossible to satisfy.

The SMA take-profit is a *moving* target (it tracks the live SMA every bar);
the engine therefore carries a far placeholder target at ENTER time so intrabar
wick logic never fires on it.  The real take-profit exits via on_tick when
price crosses the live mid.  The protective stop IS a fixed absolute level set
at entry and caught both intrabar by the engine and close-based here.

Position state is driven entirely by notify_fill / notify_flat; the strategy
never assumes an emitted ENTER was executed.
"""
import collections
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from strategies.orb import Decision, IST, MARKET_CLOSE, MAX_FUTURE_SKEW

logger = logging.getLogger("dhan.strategy.bollinger_mr")


@dataclass
class BollingerMeanReversionParams:
    period: int = 20                       # SMA / stdev lookback (bars) — Bollinger window
    k: float = 2.0                         # band width in stdevs: bands = sma ± k*stdev
    stop_method: str = "band_pct"          # "band_pct" | "atr"
    stop_band_pct: float = 0.003           # band_pct: stop is 0.3% beyond the entry band
    stop_atr_mult: float = 1.5             # atr: stop is this many ATR beyond the entry band
    atr_period: int = 14                   # ATR lookback (bars); only used for stop_method=="atr"
    enable_short: bool = True              # symmetric short side on/off
    exit_on_opposite_band: bool = True     # also exit if price tags the OPPOSITE band
    squareoff_before_close_min: int = 15
    min_price: float = 1.0                 # ignore degenerate sub-₹1 ticks
    min_band_width_pct: float = 0.0        # skip entries when band half-width < this fraction of price


class BollingerMeanReversion:
    def __init__(self, security_id: str,
                 params: Optional[BollingerMeanReversionParams] = None):
        self.security_id = security_id
        self.p = params or BollingerMeanReversionParams()

        self._session_date: Optional[date] = None

        # Rolling SMA / stdev state (deque + running sums).
        # The deque holds the last N COMPLETED closes; the current bar's close
        # is appended AFTER the signal check (see module docstring).
        self.closes: collections.deque = collections.deque(maxlen=self.p.period)
        self.close_sum: float = 0.0
        self.sq_sum: float = 0.0

        # ATR state (only used when stop_method == "atr")
        self.atr: Optional[float] = None
        self.prev_close_atr: Optional[float] = None
        self.tr_count: int = 0
        self._tr_seed_sum: float = 0.0

        # Position state — updated only via notify_fill / notify_flat
        self.position: int = 0
        self.entry_price: float = 0.0
        self.stop_price: float = 0.0

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
        self.stop_price = 0.0

    # ── Core logic ────────────────────────────────────────────────────────────

    def on_tick(self, now: datetime, price: float,
                high: Optional[float] = None, low: Optional[float] = None,
                volume: Optional[float] = None) -> Optional[Decision]:
        """now must be IST.  price = bar close; high/low = current bar extremes."""
        # 0a. Sanity guard
        if price <= 0 or price < self.p.min_price:
            return None

        # 0b. Future-skew guard — copy verbatim from ORB
        wall = datetime.now(IST)
        ref = wall if now.tzinfo is not None else wall.replace(tzinfo=None)
        if now - ref > MAX_FUTURE_SKEW:
            logger.warning(
                "[BollingerMR %s] ignoring future-stamped tick %s (wall %s) — skew > %s",
                self.security_id, now, ref, MAX_FUTURE_SKEW,
            )
            return None

        today = now.date()
        t = now.time()

        # 1. Session reset on date change
        if self._session_date != today:
            self._reset_session(today)

        # 2. Compute bands from the CURRENT deque (previous bars), BEFORE appending
        #    the current close.  This is the standard Bollinger approach: the signal
        #    on bar N fires against bands established by bars 1..N-1.
        sma, upper, lower, half_width = self._compute_bands()
        bands_ready = sma is not None

        # Compute ATR pre-update (we need ATR for the stop calculation at entry;
        # ATR from prior bars is correct — same "use previous state" principle).
        atr_before = self.atr  # may be None during warm-up

        # Update the deque and ATR with the current bar's close / high / low.
        self._update_indicators(price, high, low)

        # After the indicator update, the deque now includes the current bar.
        # For the EXIT take-profit check we want the FRESH (post-update) SMA so
        # that the mean target tracks the latest bar, not the one before it.
        sma_new, upper_new, lower_new, _ = self._compute_bands()

        # Determine readiness
        atr_ready = (self.p.stop_method != "atr") or (atr_before is not None)

        # 3. EOD square-off — UNCONDITIONAL, above every readiness gate
        squareoff = (
            datetime.combine(today, MARKET_CLOSE)
            - timedelta(minutes=self.p.squareoff_before_close_min)
        ).time()
        if t >= squareoff:
            if self.position != 0:
                return Decision(action="EXIT", reason="EOD square-off")
            return None

        # 4. Warm-up gate — need bands from previous bars for entries/exits
        if not bands_ready or not atr_ready:
            return None

        mid = sma  # pre-update middle band (used for entry check)
        # For exits, use the post-update SMA so the moving target is current
        mid_new = sma_new if sma_new is not None else mid

        # 5. Manage an OPEN position (exits only; return after this block)
        if self.position > 0:  # LONG
            if price >= mid_new:
                return Decision(
                    action="EXIT",
                    reason=f"Mean reversion target ₹{mid_new:.2f}",
                )
            if self.p.exit_on_opposite_band and upper_new is not None and price >= upper_new:
                return Decision(
                    action="EXIT",
                    reason=f"Opposite band ₹{upper_new:.2f}",
                )
            if price <= self.stop_price:
                return Decision(
                    action="EXIT",
                    reason=f"Stop-loss ₹{self.stop_price:.2f}",
                )
            return None

        if self.position < 0:  # SHORT
            if price <= mid_new:
                return Decision(
                    action="EXIT",
                    reason=f"Mean reversion target ₹{mid_new:.2f}",
                )
            if self.p.exit_on_opposite_band and lower_new is not None and price <= lower_new:
                return Decision(
                    action="EXIT",
                    reason=f"Opposite band ₹{lower_new:.2f}",
                )
            if price >= self.stop_price:
                return Decision(
                    action="EXIT",
                    reason=f"Stop-loss ₹{self.stop_price:.2f}",
                )
            return None

        # 6. Entries (only when flat); use PRE-UPDATE bands so entry fires when
        #    the current close pierces the band built from prior bars.

        # Band-width filter (uses pre-update half_width)
        if self.p.min_band_width_pct > 0 and half_width < price * self.p.min_band_width_pct:
            return None

        # Compute stop distance using pre-update ATR (atr_before)
        if self.p.stop_method == "band_pct":
            stop_dist_long = lower * self.p.stop_band_pct
            stop_dist_short = upper * self.p.stop_band_pct
        else:  # atr — atr_before is guaranteed non-None here (atr_ready == True)
            stop_dist_long = self.p.stop_atr_mult * atr_before
            stop_dist_short = self.p.stop_atr_mult * atr_before

        # LONG: price pierces lower band
        if price <= lower:
            stop = lower - stop_dist_long
            if stop >= price:  # degenerate: stop not below entry
                return None
            # Far placeholder target (real exit is SMA tag via on_tick)
            if self.p.stop_method == "band_pct":
                target = price * (1 + 10 * self.p.stop_band_pct)
            else:
                target = price + 10 * stop_dist_long
            self.stop_price = stop
            return Decision(
                action="ENTER", side="BUY",
                stop=stop, target=target,
                reason=f"BB long: px={price:.2f} <= lower={lower:.2f} (mid={mid:.2f})",
            )

        # SHORT: price pierces upper band (only if enabled)
        if self.p.enable_short and price >= upper:
            stop = upper + stop_dist_short
            if stop <= price:  # degenerate
                return None
            if self.p.stop_method == "band_pct":
                target = price * (1 - 10 * self.p.stop_band_pct)
            else:
                target = price - 10 * stop_dist_short
            self.stop_price = stop
            return Decision(
                action="ENTER", side="SELL",
                stop=stop, target=target,
                reason=f"BB short: px={price:.2f} >= upper={upper:.2f} (mid={mid:.2f})",
            )

        return None

    # ── Indicators ────────────────────────────────────────────────────────────

    def _update_indicators(self, c: float,
                           high: Optional[float], low: Optional[float]) -> None:
        """Append the current bar's close to the rolling deque and update ATR."""
        # Rolling SMA + stdev (population, deque + running sums)
        if len(self.closes) == self.p.period:
            # Evict oldest before append (deque is full)
            old = self.closes[0]
            self.close_sum -= old
            self.sq_sum -= old * old
        self.closes.append(c)
        self.close_sum += c
        self.sq_sum += c * c

        # ATR (Wilder smoothing) — only when stop_method == "atr"
        if self.p.stop_method == "atr":
            h = high if high is not None else c
            l = low if low is not None else c
            if self.prev_close_atr is None:
                tr = h - l
            else:
                pc = self.prev_close_atr
                tr = max(h - l, abs(h - pc), abs(l - pc))
            self.tr_count += 1
            n = self.p.atr_period
            if self.tr_count <= n:
                self._tr_seed_sum += tr
                if self.tr_count == n:
                    self.atr = self._tr_seed_sum / n
            else:
                self.atr = (self.atr * (n - 1) + tr) / n
            self.prev_close_atr = c

    def _compute_bands(self):
        """Return (sma, upper, lower, half_width) from the CURRENT deque contents,
        or (None, None, None, None) if the deque is not yet full."""
        if len(self.closes) < self.p.period:
            return None, None, None, None
        N = self.p.period
        mean = self.close_sum / N
        var = self.sq_sum / N - mean * mean
        var = max(var, 0.0)  # clamp floating-point negatives on flat windows
        stdev = math.sqrt(var)
        hw = self.p.k * stdev
        return mean, mean + hw, mean - hw, hw

    # ── Session reset ─────────────────────────────────────────────────────────

    def _reset_session(self, today: date) -> None:
        self._session_date = today
        self.closes = collections.deque(maxlen=self.p.period)
        self.close_sum = 0.0
        self.sq_sum = 0.0
        # ATR (reset unconditionally for safety, even when stop_method != "atr")
        self.atr = None
        self.prev_close_atr = None
        self.tr_count = 0
        self._tr_seed_sum = 0.0
        logger.info("[BollingerMR %s] New session %s — reset", self.security_id, today)

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        sma, upper, lower, _ = self._compute_bands()
        return {
            "security_id": self.security_id,
            "sma": round(sma, 2) if sma is not None else None,
            "upper": round(upper, 2) if upper is not None else None,
            "lower": round(lower, 2) if lower is not None else None,
            "atr": round(self.atr, 4) if self.atr is not None else None,
            "position": self.position,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
        }
