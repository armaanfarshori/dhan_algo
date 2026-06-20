"""
VWAP Trend — trend-following around session VWAP (pullback continuation / rejection).

Trades WITH the intraday trend as defined by session VWAP and its slope:
  Long  — price above a rising VWAP, pulls back to / touches VWAP, then holds/reclaims.
  Short — price below a falling VWAP, rallies into VWAP, then rejects.

Primary exit: price closes back through VWAP against the position (VWAP cross-back).
Protective stop: a buffer beyond VWAP on the wrong side.
Far "no-target" arm so the engine's target never fires intraday.

This is NOT the mean-reversion (vwap_mr) variant — it does NOT fade extensions away from
VWAP. A long here needs price > VWAP and slope > 0; the MR variant is the opposite stance.

Interface mirrors strategies/orb.py exactly. Reuses Decision, IST, MARKET_OPEN,
MARKET_CLOSE, MAX_FUTURE_SKEW from that module.
"""
import collections
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from strategies.orb import Decision, IST, MARKET_CLOSE, MARKET_OPEN, MAX_FUTURE_SKEW

logger = logging.getLogger("dhan.strategy.vwap_trend")


@dataclass
class VwapTrendParams:
    slope_lag: int = 5                   # bars back for the VWAP-slope difference
    slope_eps: float = 0.0002            # fractional slope floor; 0 = no flat filter
    touch_band_pct: float = 0.0010       # how close to VWAP a wick must come to arm
    reclaim_band_pct: float = 0.0005     # margin past VWAP a close needs to confirm hold/reject
    invalidate_band_pct: float = 0.0015  # margin past VWAP (wrong side) that voids armed zone
    vwap_exit_band_pct: float = 0.0005   # margin past VWAP (wrong side) for VWAP cross-back exit
    sl_buffer_pct: float = 0.0030        # protective stop distance beyond VWAP
    far_target_mult: float = 0.50        # "no-target" arm: ±50% from entry (unreachable intraday)
    entry_start_min: int = 15            # no entries in the first N minutes
    squareoff_before_close_min: int = 15


class VwapTrend:
    """Pure, synchronous, IO-free VWAP-trend strategy.

    Volume MUST be supplied via the ``volume`` keyword argument of ``on_tick``.
    If volume is None (or 0), the bar is consumed but accumulators do not advance;
    the strategy stays in warm-up (returns None) until valid volume arrives.
    """

    def __init__(self, security_id: str, params: Optional[VwapTrendParams] = None):
        self.security_id = security_id
        self.p = params or VwapTrendParams()

        # Position state — updated only by notify_fill / notify_flat
        self.position: int = 0
        self.entry_price: float = 0.0

        self._session_date: Optional[date] = None
        self._reset_session(None)  # initialise all intraday state

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
        self._zone = "NONE"  # defensive clear

    # ── Core logic ────────────────────────────────────────────────────────────

    def on_tick(
        self,
        now: datetime,
        price: float,
        high: Optional[float] = None,
        low: Optional[float] = None,
        volume: Optional[float] = None,
    ) -> Optional[Decision]:
        """now must be IST (naive assumed IST, or tz-aware).
        price = bar close; high/low = bar extremes (fall back to close if absent).
        volume = bar volume; if None or 0, VWAP accumulators are not updated
        and the strategy returns None (warm-up) until volume arrives.
        """
        if price <= 0:
            return None

        # Future-skew guard — copy ORB exactly
        wall = datetime.now(IST)
        ref = wall if now.tzinfo is not None else wall.replace(tzinfo=None)
        if now - ref > MAX_FUTURE_SKEW:
            logger.warning(
                "[VwapTrend %s] ignoring future-stamped tick %s (wall %s)",
                self.security_id, now, ref,
            )
            return None

        today = now.date()
        t = now.time()

        if self._session_date != today:
            self._reset_session(today)

        # 1. EOD square-off — UNCONDITIONAL, before any VWAP-readiness gate
        squareoff = (
            datetime.combine(today, MARKET_CLOSE)
            - timedelta(minutes=self.p.squareoff_before_close_min)
        ).time()
        if t >= squareoff:
            if self.position != 0:
                return Decision(action="EXIT", reason="EOD square-off")
            return None

        # 2. Consume volume, update VWAP accumulators
        vol = float(volume) if (volume is not None and volume > 0) else 0.0
        hi = high if high is not None else price
        lo = low if low is not None else price
        typical = (hi + lo + price) / 3.0

        if vol > 0:
            self._cum_pv += typical * vol
            self._cum_vol += vol

        if self._cum_vol <= 0:
            return None  # warm-up: no volume yet

        vwap = self._cum_pv / self._cum_vol

        # 3. VWAP slope (incremental, lag-slope_lag difference)
        self._vwap_hist.append(vwap)
        slope: Optional[float] = None
        if len(self._vwap_hist) > self.p.slope_lag:
            prev_vwap_lag = self._vwap_hist[0]
            if prev_vwap_lag > 0:
                slope = (vwap - prev_vwap_lag) / prev_vwap_lag

        # Snapshot prior-bar values BEFORE overwriting at the end
        prev_close = self._prev_close
        prev_vwap = self._prev_vwap

        # 4. EXITS first (position open) — check before entries
        if self.position > 0:
            result: Optional[Decision] = None
            if vwap > 0 and price < vwap * (1 - self.p.vwap_exit_band_pct):
                result = Decision(action="EXIT", reason="VWAP cross-back (long)")
            elif price <= self._stop_level:
                result = Decision(
                    action="EXIT",
                    reason=f"Stop-loss ₹{self._stop_level:.2f}",
                )
            self._prev_close = price
            self._prev_vwap = vwap
            return result

        if self.position < 0:
            result = None
            if vwap > 0 and price > vwap * (1 + self.p.vwap_exit_band_pct):
                result = Decision(action="EXIT", reason="VWAP cross-back (short)")
            elif price >= self._stop_level:
                result = Decision(
                    action="EXIT",
                    reason=f"Stop-loss ₹{self._stop_level:.2f}",
                )
            self._prev_close = price
            self._prev_vwap = vwap
            return result

        # 5. ENTRIES (flat) — need slope, within window, prior bar available
        decision: Optional[Decision] = None
        entry_start = (
            datetime.combine(today, MARKET_OPEN)
            + timedelta(minutes=self.p.entry_start_min)
        ).time()

        if slope is not None and t >= entry_start and prev_vwap is not None:
            if slope > self.p.slope_eps:
                # Uptrend bias — look for long pullback that holds/reclaims.
                # Clear any stale short zone from a previous downtrend phase.
                if self._zone == "PULLBACK_SHORT":
                    self._zone = "NONE"

                # Arm: prior close was above prior VWAP AND this bar's low dipped to VWAP
                if (
                    prev_close is not None
                    and prev_close > prev_vwap
                    and lo <= vwap * (1 + self.p.touch_band_pct)
                ):
                    self._zone = "PULLBACK_LONG"

                if self._zone == "PULLBACK_LONG":
                    if price < vwap * (1 - self.p.invalidate_band_pct):
                        # Pullback failed — thesis voided
                        self._zone = "NONE"
                    elif price >= vwap * (1 + self.p.reclaim_band_pct):
                        stop = vwap * (1 - self.p.sl_buffer_pct)
                        target = price * (1 + self.p.far_target_mult)
                        self._stop_level = stop
                        decision = Decision(
                            action="ENTER",
                            side="BUY",
                            stop=stop,
                            target=target,
                            reason=(
                                f"VWAP-trend long pullback"
                                f" vwap={vwap:.2f} slope={slope:+.4f}"
                            ),
                        )
                        self._zone = "NONE"

            elif slope < -self.p.slope_eps:
                # Downtrend bias — look for short rejection from VWAP.
                # Clear any stale long zone from a previous uptrend phase.
                if self._zone == "PULLBACK_LONG":
                    self._zone = "NONE"

                # Arm: prior close was below prior VWAP AND this bar's high reached VWAP
                if (
                    prev_close is not None
                    and prev_close < prev_vwap
                    and hi >= vwap * (1 - self.p.touch_band_pct)
                ):
                    self._zone = "PULLBACK_SHORT"

                if self._zone == "PULLBACK_SHORT":
                    if price > vwap * (1 + self.p.invalidate_band_pct):
                        # Rally through VWAP — downtrend thesis voided
                        self._zone = "NONE"
                    elif price <= vwap * (1 - self.p.reclaim_band_pct):
                        stop = vwap * (1 + self.p.sl_buffer_pct)
                        target = max(price * (1 - self.p.far_target_mult), 0.05)
                        self._stop_level = stop
                        decision = Decision(
                            action="ENTER",
                            side="SELL",
                            stop=stop,
                            target=target,
                            reason=(
                                f"VWAP-trend short rejection"
                                f" vwap={vwap:.2f} slope={slope:+.4f}"
                            ),
                        )
                        self._zone = "NONE"

            else:
                # Flat VWAP — no new entries, clear any armed zone
                self._zone = "NONE"

        # 6. Advance prior-bar state and return
        self._prev_close = price
        self._prev_vwap = vwap
        return decision

    # ── Session management ────────────────────────────────────────────────────

    def _reset_session(self, today: Optional[date]) -> None:
        """Reset ALL intraday state. Position state is not touched (owned by notify_*)."""
        self._session_date = today
        # VWAP accumulators
        self._cum_pv: float = 0.0
        self._cum_vol: float = 0.0
        # Slope history: deque of the last (slope_lag + 1) VWAP values
        self._vwap_hist: collections.deque = collections.deque(
            maxlen=self.p.slope_lag + 1
        )
        # Prior-bar snapshot (used by zone machine arming condition)
        self._prev_close: Optional[float] = None
        self._prev_vwap: Optional[float] = None
        # Zone machine
        self._zone: str = "NONE"
        # Stop level (set at ENTER, read while position is open)
        self._stop_level: float = 0.0
        logger.debug("[VwapTrend %s] session reset %s", self.security_id, today)
