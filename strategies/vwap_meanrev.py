"""
VWAP Mean-Reversion — pure signal logic.

Fades price stretches beyond a VWAP dispersion band, targeting reversion
back to the session VWAP.  Opposite posture to ORB (breakout); run on
the same engine/costs/universe for direct comparison.

Decision protocol — on_tick() returns None or a Decision (reused from orb.py):
    Decision(action="ENTER", side="BUY"|"SELL", stop=…, target=…, reason=…)
    Decision(action="EXIT",  reason=…)

Position state is fed back via notify_fill()/notify_flat(); the strategy
never assumes an order it requested was actually executed.

Spec: research/backtest/strategy_specs/vwap_meanrev.md
"""
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from core.sessions import EQUITY, MarketSession

# Reuse the shared market-session constants from ORB so they never drift.
from strategies.orb import (  # noqa: F401 — MARKET_OPEN/MARKET_CLOSE re-exported for parity
    IST,
    MARKET_CLOSE,
    MARKET_OPEN,
    MAX_FUTURE_SKEW,
    Decision,
)

logger = logging.getLogger("dhan.strategy.vwap_mr")


@dataclass
class VwapMeanRevParams:
    band_method: str = "resid_std"          # "resid_std" | "typical_std"
    entry_band_mult: float = 2.0            # k — entry trigger = vwap ± k*band
    stop_band_mult: float = 1.0             # protective stop = trigger ± this*band beyond band
    warmup_bars: int = 20                   # bars required before any signal
    min_band_pct: float = 0.0015            # skip if band < this fraction of price
    max_entries_per_session: int = 3        # re-entry cap after completed round trips
    squareoff_before_close_min: int = 15
    no_entry_before_close_min: int = 30
    ewma_alpha: float = 0.0                 # 0 => plain cumulative; >0 => EWMA band


class VwapMeanReversion:
    def __init__(self, security_id: str, params: Optional[VwapMeanRevParams] = None,
                 session: MarketSession = EQUITY):
        """``session`` binds the EOD square-off / no-entry window to a MarketSession
        profile (defaults to EQUITY → byte-identical legacy 15:30 behaviour). Pass
        session=MCX for the evening commodity session (DST-aware close)."""
        self.security_id = security_id
        self.p = params or VwapMeanRevParams()
        self.session = session

        # Position view — updated ONLY via notify_fill / notify_flat
        self.position: int = 0
        self.entry_price: float = 0.0

        # Session state (reset on date change)
        self._session_date: Optional[date] = None
        self._cum_pv: float = 0.0           # Σ (tp · volume)
        self._cum_vol: float = 0.0          # Σ volume
        self._n: int = 0                    # Welford count
        self._mean: float = 0.0             # Welford running mean of fed value
        self._m2: float = 0.0              # Welford sum of squared deltas
        self._ew_mean: float = 0.0          # EWMA mean
        self._ew_var: float = 0.0           # EWMA variance
        self._ew_init: bool = False         # EWMA initialised flag
        self._entries_this_session: int = 0

        # Position-level stop/target snapshots (cleared on notify_flat)
        self._stop: float = 0.0
        self._target: float = 0.0

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
        self._stop = 0.0
        self._target = 0.0

    # ── Core logic ────────────────────────────────────────────────────────────

    def on_tick(self, now: datetime, price: float,
                high: Optional[float] = None,
                low: Optional[float] = None,
                volume: Optional[float] = None) -> Optional[Decision]:
        """now must be IST.  price = bar close; high/low = bar extremes; volume = bar volume."""
        if price <= 0:
            return None

        # Future-skew guard: ignore implausibly future-stamped ticks
        wall = datetime.now(IST)
        ref = wall if now.tzinfo is not None else wall.replace(tzinfo=None)
        if now - ref > MAX_FUTURE_SKEW:
            logger.warning(
                "[VWAP-MR %s] ignoring future-stamped tick %s (wall %s) — skew > %s",
                self.security_id, now, ref, MAX_FUTURE_SKEW,
            )
            return None

        today = now.date()
        t = now.time()

        if self._session_date != today:
            self._reset_session(today)

        squareoff_time = (
            datetime.combine(today, self.session.close_time_for(today))
            - timedelta(minutes=self.p.squareoff_before_close_min)
        ).time()

        # 1. EOD square-off — UNCONDITIONAL, above everything (incl. warm-up)
        if t >= squareoff_time:
            if self.position != 0:
                return Decision(action="EXIT", reason="EOD square-off")
            return None

        # 2. Update VWAP / band state for this bar (before any signal reads them)
        vwap, band = self._update_indicators(price, high, low, volume)

        # 3. Exit logic for open positions (sits above warm-up gate)
        if self.position != 0:
            # 3a. Protective stop (close-based belt-and-braces; engine owns intrabar wick)
            if self.position > 0 and self._stop > 0.0 and price <= self._stop:
                return Decision(action="EXIT", reason="VWAP-MR stop")
            if self.position < 0 and self._stop > 0.0 and price >= self._stop:
                return Decision(action="EXIT", reason="VWAP-MR stop")
            # 3b. VWAP reversion target (primary profit exit — dynamic VWAP)
            if self.position > 0 and price >= vwap:
                return Decision(action="EXIT", reason="VWAP target")
            if self.position < 0 and price <= vwap:
                return Decision(action="EXIT", reason="VWAP target")
            return None

        # 4. Warm-up gate (entry only — exits/EOD are not gated)
        if not self._ready(band):
            return None

        # 5. No-entry near close
        no_entry_time = (
            datetime.combine(today, self.session.close_time_for(today))
            - timedelta(minutes=self.p.no_entry_before_close_min)
        ).time()
        if t >= no_entry_time:
            return None

        # 6. Entry cap
        if self._entries_this_session >= self.p.max_entries_per_session:
            return None

        # 7. Minimum-band filter (skip dead-flat sessions)
        if band < price * self.p.min_band_pct:
            return None

        # 8. Entry signals
        upper = vwap + self.p.entry_band_mult * band
        lower = vwap - self.p.entry_band_mult * band

        # SHORT — fade the stretch up
        if price >= upper:
            stop = upper + self.p.stop_band_mult * band
            target = vwap
            self._stop = stop
            self._target = target
            self._entries_this_session += 1
            logger.info(
                "[VWAP-MR %s] SHORT signal  px=%.2f vwap=%.2f band=%.4f stop=%.2f target=%.2f",
                self.security_id, price, vwap, band, stop, target,
            )
            return Decision(
                action="ENTER", side="SELL", stop=stop, target=target,
                reason=f"VWAP-MR short  px={price:.2f} vwap={vwap:.2f} band={band:.2f}",
            )

        # LONG — fade the stretch down
        if price <= lower:
            stop = lower - self.p.stop_band_mult * band
            target = vwap
            self._stop = stop
            self._target = target
            self._entries_this_session += 1
            logger.info(
                "[VWAP-MR %s] LONG signal  px=%.2f vwap=%.2f band=%.4f stop=%.2f target=%.2f",
                self.security_id, price, vwap, band, stop, target,
            )
            return Decision(
                action="ENTER", side="BUY", stop=stop, target=target,
                reason=f"VWAP-MR long  px={price:.2f} vwap={vwap:.2f} band={band:.2f}",
            )

        return None

    # ── Indicator math ────────────────────────────────────────────────────────

    def _update_indicators(
        self,
        close: float,
        high: Optional[float],
        low: Optional[float],
        volume: Optional[float],
    ) -> tuple[float, float]:
        """Update cumulative VWAP and band; return (vwap, band)."""
        h = high if high is not None else close
        lo = low if low is not None else close
        tp = (h + lo + close) / 3.0

        # Volume handling: None or negative → treat as 0 (bar contributes nothing)
        v = float(volume) if volume is not None and volume >= 0 else 0.0

        # VWAP (exact cumulative)
        self._cum_pv += tp * v
        self._cum_vol += v
        vwap = self._cum_pv / self._cum_vol if self._cum_vol > 0 else tp

        # Band — feed resid or tp depending on band_method
        if self.p.band_method == "typical_std":
            fed = tp
        else:
            # default: resid_std
            fed = tp - vwap

        band = self._welford_update(fed)
        return vwap, band

    def _welford_update(self, value: float) -> float:
        """Update Welford (or EWMA if alpha>0) with `value`; return current std (band)."""
        if self.p.ewma_alpha > 0.0:
            a = self.p.ewma_alpha
            if not self._ew_init:
                self._ew_mean = value
                self._ew_var = 0.0
                self._ew_init = True
            else:
                diff = value - self._ew_mean
                incr = a * diff
                self._ew_mean += incr
                self._ew_var = (1 - a) * (self._ew_var + diff * incr)
            return math.sqrt(max(self._ew_var, 0.0))
        else:
            # Plain Welford
            self._n += 1
            delta = value - self._mean
            self._mean += delta / self._n
            delta2 = value - self._mean
            self._m2 += delta * delta2
            var = self._m2 / (self._n - 1) if self._n >= 2 else 0.0
            return math.sqrt(max(var, 0.0))

    def _ready(self, band: float) -> bool:
        return (
            self._n >= self.p.warmup_bars
            and self._cum_vol > 0
            and band > 0
        )

    # ── Session reset ─────────────────────────────────────────────────────────

    def _reset_session(self, today: date):
        self._session_date = today
        self._cum_pv = 0.0
        self._cum_vol = 0.0
        self._n = 0
        self._mean = 0.0
        self._m2 = 0.0
        self._ew_mean = 0.0
        self._ew_var = 0.0
        self._ew_init = False
        self._entries_this_session = 0
        self._stop = 0.0
        self._target = 0.0
        logger.info("[VWAP-MR %s] New session %s — reset", self.security_id, today)

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        # Reconstruct vwap/band from current state without mutating it
        vwap = self._cum_pv / self._cum_vol if self._cum_vol > 0 else 0.0
        # Reconstruct band from whichever variance model is active. In EWMA mode
        # the Welford fields (_m2/_n) are never updated, so read _ew_var instead;
        # otherwise (plain cumulative) read the Welford variance.
        if self.p.ewma_alpha > 0.0:
            band = math.sqrt(max(self._ew_var, 0.0)) if self._ew_init else 0.0
        elif self._n >= 2:
            var = self._m2 / (self._n - 1)
            band = math.sqrt(max(var, 0.0))
        else:
            band = 0.0
        upper = vwap + self.p.entry_band_mult * band
        lower = vwap - self.p.entry_band_mult * band
        return {
            "security_id": self.security_id,
            "vwap": round(vwap, 4),
            "band": round(band, 4),
            "upper": round(upper, 4),
            "lower": round(lower, 4),
            "position": self.position,
            "entry_price": self.entry_price,
            "ready": self._n >= self.p.warmup_bars and self._cum_vol > 0 and band > 0,
            "entries_this_session": self._entries_this_session,
        }
