"""
Intraday Options Scalper (Ladder) — pure signal/ladder engine for NIFTY long options.

This class is **deliberately synchronous and IO-free**: underlying ticks in, ScalpDecisions
out.  It mirrors the structure and hard rules of ``strategies/orb.py`` (session reset,
unconditional EOD square-off, future-skew guard, position via fills only).

DATA-FEASIBILITY NOTICE (read before attempting a backtest)
-----------------------------------------------------------
The current DB has **NO intraday option-premium series** and (today) **NO intraday NIFTY
1-min bars**.  A faithful historical backtest of this intraday scalper is *blocked* on
data that does not exist yet.

Recommended path: **forward paper-trade on a live intraday feed** — underlying 1-min (or
tick) for NIFTY spot for the signal, plus the live option LTP (ATM CE/PE) for TP/stop.
That LTP feed does not exist in ``core/fno_collector.py`` today (it is EOD-only); building
an intraday ATM subscription is a prerequisite and is a separate PR.

Optional Black-76 intraday backtest: *only if* a real intraday NIFTY index path becomes
available (e.g. ``index_bars`` at ``timeframe="1min"``).  **Do NOT synthesise an intraday
path from daily OHLCV** — interpolating or Brownian-bridging daily bars would fabricate the
very premium moves this strategy trades.  If intraday data is absent, the Black-76 path is
not available.

The class is fully unit-testable with synthetic on_tick streams regardless of data
availability — §7 of the spec drives tests by feeding hand-crafted price sequences.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("dhan.strategy.options_scalper")

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

# Mirror ORB: a tick stamped further than this ahead of wall clock is ignored.
MAX_FUTURE_SKEW = timedelta(minutes=2)


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------


@dataclass
class ScalperParams:
    # ── direction signal (§1) ──────────────────────────────────────────────
    signal: str = "vwap_mom"      # "vwap_mom" | "ema" | "orb" | "momentum"
    vwap_band: float = 0.0005     # deadband around VWAP (5 bps) for LONG/SHORT/FLAT
    mom_k: int = 5                # trailing minutes for the momentum trigger
    mom_thresh: float = 0.0008    # min |k-min return| to trigger (8 bps)
    atr_window: int = 14          # ATR-style activity filter window (min)
    min_atr_pts: float = 6.0      # min avg true range (NIFTY pts) to allow entries
    ema_fast: int = 5             # used when signal == "ema"
    ema_slow: int = 20
    orb_minutes: int = 15         # used when signal == "orb"
    warmup_minutes: int = 15      # no entries before OPEN + this

    # ── strike (§1d) ───────────────────────────────────────────────────────
    strike_offset: int = 0        # grid steps; - = ITM for the side, + = OTM
    step: int = 50                # NIFTY strike grid

    # ── ladder entries (§2a) ───────────────────────────────────────────────
    ladder_mode: str = "pyramid"          # "pyramid" | "scale_in_dips"
    rung_spacing_pts: float = 10.0        # underlying-pt spacing between adds
    tranche_lots: int = 1                 # lots per rung (×65 units)
    max_rungs: int = 3                    # max tranches per ladder
    ladder_size_mode: str = "flat"        # "flat" | "decreasing"

    # ── ladder exits / scalp (§2b, §3) ─────────────────────────────────────
    tp_ladder_pct: list = field(default_factory=lambda: [0.10, 0.20, 0.35])
    trail_pct: float = 0.12               # trail remainder off premium high-water
    stop_pct: float = 0.20                # hard stop per tranche (% of fill premium)
    time_stop_min: int = 12               # theta time-stop per tranche
    cooldown_min: int = 3                 # after a full flatten, before new ladder

    # ── risk / session limits (§3) ─────────────────────────────────────────
    max_trades: int = 8                   # ladders started per day
    daily_loss_cap: float = 8000.0        # ₹ realized-loss stand-down
    no_trade_open_min: int = 15
    no_trade_close_min: int = 20
    squareoff_before_close_min: int = 5

    # ── contract ───────────────────────────────────────────────────────────
    lot: int = 65                          # NIFTY_LOT


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScalpDecision:
    action: str         # "ENTER" | "EXIT"
    side: str = ""      # ENTER: always "BUY" (long-only). EXIT: "SELL".
    option_type: str = ""   # "CE" | "PE"
    strike: int = 0
    lots: int = 0           # tranche size this decision acts on
    reason: str = ""


# ---------------------------------------------------------------------------
# Internal tranche
# ---------------------------------------------------------------------------


@dataclass
class _Tranche:
    lots: int
    entry_premium: float
    entry_underlying: float
    fill_time: datetime
    tp_index: int = 0          # which tp_ladder_pct level is next for this tranche
    trailing: bool = False     # True once top TP hit; switches to trail
    hi_water_premium: float = 0.0


# ---------------------------------------------------------------------------
# Pure signal helper
# ---------------------------------------------------------------------------


def direction_signal(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    params: ScalperParams,
    vwap: float,
    orb_high: float = 0.0,
    orb_low: float = 0.0,
) -> str:
    """Compute direction signal from a trailing window of underlying prices.

    Parameters
    ----------
    closes:
        1-min close prices (underlying), newest last.  Must have >= warmup bars.
    highs, lows:
        Corresponding 1-min high/low for ATR filter; same length as ``closes``.
    params:
        ScalperParams.
    vwap:
        Running intraday VWAP of the underlying (maintained by caller).
    orb_high, orb_low:
        Opening-range high/low (only used when ``params.signal == "orb"``).

    Returns
    -------
    "LONG" | "SHORT" | "FLAT"
    """
    if not closes:
        return "FLAT"

    price = closes[-1]
    sig = params.signal

    if sig == "vwap_mom":
        # 1. VWAP regime anchor with deadband
        upper = vwap * (1 + params.vwap_band)
        lower = vwap * (1 - params.vwap_band)
        if price > upper:
            anchor = "LONG"
        elif price < lower:
            anchor = "SHORT"
        else:
            return "FLAT"

        # 2. Signed k-min momentum trigger
        k = params.mom_k
        if len(closes) < k + 1:
            return "FLAT"
        ret_k = (closes[-1] - closes[-1 - k]) / closes[-1 - k]
        if anchor == "LONG" and ret_k < params.mom_thresh:
            return "FLAT"
        if anchor == "SHORT" and ret_k > -params.mom_thresh:
            return "FLAT"

        # 3. ATR activity filter
        w = params.atr_window
        if len(highs) < w or len(lows) < w:
            return "FLAT"
        trs = [highs[-i] - lows[-i] for i in range(1, w + 1)]
        avg_tr = sum(trs) / len(trs)
        if avg_tr < params.min_atr_pts:
            return "FLAT"

        return anchor

    elif sig == "ema":
        fast = params.ema_fast
        slow = params.ema_slow
        if len(closes) < slow:
            return "FLAT"

        def _ema(prices: list[float], period: int) -> float:
            k_mult = 2 / (period + 1)
            ema = prices[0]
            for p in prices[1:]:
                ema = p * k_mult + ema * (1 - k_mult)
            return ema

        ema_f = _ema(closes[-slow:], fast)
        ema_s = _ema(closes[-slow:], slow)
        if ema_f > ema_s:
            return "LONG"
        elif ema_f < ema_s:
            return "SHORT"
        return "FLAT"

    elif sig == "orb":
        if orb_high <= 0 or orb_low <= 0:
            return "FLAT"
        if price > orb_high:
            return "LONG"
        elif price < orb_low:
            return "SHORT"
        return "FLAT"

    elif sig == "momentum":
        k = params.mom_k
        if len(closes) < k + 1:
            return "FLAT"
        if closes[-1 - k] == 0:
            return "FLAT"
        ret_k = (closes[-1] - closes[-1 - k]) / closes[-1 - k]
        if ret_k > params.mom_thresh:
            return "LONG"
        elif ret_k < -params.mom_thresh:
            return "SHORT"
        return "FLAT"

    return "FLAT"


# ---------------------------------------------------------------------------
# OptionsScalper
# ---------------------------------------------------------------------------


def _round_to_step(value: float, step: int) -> int:
    """Round value to nearest strike grid step."""
    return int(round(value / step) * step)


class OptionsScalper:
    """Intraday long-option ladder scalper for NIFTY.

    API mirrors ``strategies/orb.py``:
    - ``on_tick(now, underlying_price, option_premium, high, low)``
      returns ``ScalpDecision | None``.
    - ``notify_fill(side, lots, premium, now)`` — update tranche book.
    - ``notify_flat()`` — clear tranche book (e.g. exchange confirms flat).

    Hard rules (verbatim from spec §6):
    1. Session reset on date change.
    2. Unconditional EOD square-off — above all other gates.
    3. Future-skew guard (MAX_FUTURE_SKEW = 2 min).
    4. Position via fills only — ``on_tick`` never assumes an order filled.
    5. Long-only — every ENTER is BUY; only SELLs are exits.
    6. Fail-safe on missing premium — no TP/stop/trail fabricated if None.
    7. PAPER only — no live order path from this module.
    """

    def __init__(self, security_id: str, params: Optional[ScalperParams] = None):
        self.security_id = security_id
        self.p = params or ScalperParams()

        # ── session state ─────────────────────────────────────────────────
        self._session_date: Optional[date] = None

        # VWAP accumulator
        self._vwap_cum_tp: float = 0.0   # sum of typical_price * volume (proxy: price)
        self._vwap_cum_v: float = 0.0    # sum of volume (proxy: 1 per bar)
        self.vwap: float = 0.0

        # ORB state (signal == "orb")
        self._orb_high: float = 0.0
        self._orb_low: float = float("inf")
        self._orb_locked: bool = False

        # Trailing window for momentum / ATR (1-min closes, highs, lows)
        _window = max(self.p.ema_slow, self.p.mom_k + 1, self.p.atr_window) + 2
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._max_window: int = _window

        # Warmup bar count
        self._bars_seen: int = 0

        # Current direction anchor (updated each tick)
        self._current_direction: str = "FLAT"   # "LONG" | "SHORT" | "FLAT"

        # Test seam: when set, on_tick uses this direction instead of computing
        # it from the bar window.  Never set in production code.
        self._signal_override: Optional[str] = None

        # ── ladder / tranche book ─────────────────────────────────────────
        self._tranches: list[_Tranche] = []

        # Active ladder context (fixed at first fill of a ladder)
        self._ladder_direction: str = ""   # "LONG" | "SHORT"
        self._ladder_option_type: str = "" # "CE" | "PE"
        self._ladder_strike: int = 0
        self._ladder_anchor_underlying: float = 0.0
        self._last_rung_underlying: float = 0.0
        self._rungs_requested: int = 0

        # Class-level TP ladder index: advances each time a partial TP exit fires,
        # so successive exits hit TP[0] then TP[1] then TP[2] in order.
        self._tp_ladder_index: int = 0

        # Whether we're in the trailing phase for the last open tranche
        self._trailing_active: bool = False
        self._trailing_hi_water: float = 0.0

        # ── session risk / counters ───────────────────────────────────────
        self._trades_today: int = 0          # ladders started today
        self._daily_realized_pnl: float = 0.0
        self._standing_down: bool = False    # daily-loss kill
        self._cooldown_until: Optional[datetime] = None

        # Track last full-flatten time to compute cooldown
        self._last_flatten_time: Optional[datetime] = None

        # In-flight ENTER decision guard: once we emit ENTER for a rung,
        # block further rungs until notify_fill confirms it.
        self._pending_entry: bool = False

        # Track whether an EOD EXIT was already emitted this session.
        self._eod_exit_emitted: bool = False

        # Test seam: bypass clock-based warm-up and trade-window checks.
        # When True: _is_warm() returns True (bar-count still checked against 0),
        # trade-window and squareoff are still enforced via tick timestamp.
        # Never set in production code.
        self._bypass_time_guards: bool = False

    # ── Caller feedback ───────────────────────────────────────────────────

    def notify_fill(self, side: str, lots: int, premium: float,
                    now: Optional[datetime] = None) -> None:
        """Update the tranche book.  Always call after on_tick returns ENTER/EXIT.

        Parameters
        ----------
        side : "BUY" | "SELL"
        lots : number of lots
        premium : fill premium per unit (₹)
        now : fill timestamp (IST); required for time-stop tracking
        """
        self._pending_entry = False

        if side == "BUY":
            if premium <= 0:
                logger.warning(
                    "[OptionsScalper %s] notify_fill BUY with premium=%.4f ≤ 0 — ignoring "
                    "(zero entry_premium would make stop/TP fire instantly)",
                    self.security_id, premium,
                )
                return
            t = _Tranche(
                lots=lots,
                entry_premium=premium,
                entry_underlying=self._last_rung_underlying,
                fill_time=now or datetime.now(IST),
                hi_water_premium=premium,
            )
            self._tranches.append(t)
            logger.info(
                "[OptionsScalper %s] FILL BUY %d lots @ ₹%.2f (underlying %.2f)",
                self.security_id, lots, premium, t.entry_underlying,
            )
        elif side == "SELL":
            remaining = lots
            while remaining > 0 and self._tranches:
                t = self._tranches[0]
                if t.lots <= remaining:
                    lots_closed = t.lots
                    remaining -= t.lots
                    self._update_realized_pnl(lots_closed, t.entry_premium, premium)
                    self._tranches.pop(0)
                else:
                    lots_closed = remaining
                    self._update_realized_pnl(lots_closed, t.entry_premium, premium)
                    t.lots -= remaining
                    remaining = 0
            logger.info(
                "[OptionsScalper %s] FILL SELL %d lots @ ₹%.2f (book: %d tranches)",
                self.security_id, lots, premium, len(self._tranches),
            )
            if not self._tranches:
                self._on_fully_flat(now)

    def notify_flat(self) -> None:
        """External flatten (e.g. RiskEngine kill-switch confirmed)."""
        self._tranches.clear()
        self._on_fully_flat(datetime.now(IST))

    # ── Core tick ────────────────────────────────────────────────────────

    def on_tick(
        self,
        now: datetime,
        underlying_price: float,
        option_premium: Optional[float] = None,
        high: Optional[float] = None,
        low: Optional[float] = None,
    ) -> Optional[ScalpDecision]:
        """Process one underlying tick.

        Parameters
        ----------
        now : IST timestamp
        underlying_price : current NIFTY index price
        option_premium : LTP of the currently held ATM CE/PE (₹/unit);
            None if no live feed or no position.  If a position is open
            and this is None, premium-dependent exits are suppressed (fail-safe).
        high, low : intrabar extremes for ATR filter (optional; price used if None)
        """
        if underlying_price <= 0:
            return None

        # ── Future-skew guard (§6.3) ──────────────────────────────────────
        wall = datetime.now(IST)
        ref = wall if now.tzinfo is not None else wall.replace(tzinfo=None)
        if now - ref > MAX_FUTURE_SKEW:
            logger.warning(
                "[OptionsScalper %s] ignoring future-stamped tick %s (wall %s) — skew > %s",
                self.security_id, now, ref, MAX_FUTURE_SKEW,
            )
            return None

        today = now.date()
        t = now.time()

        # ── Session reset ─────────────────────────────────────────────────
        if self._session_date != today:
            self._reset_session(today)

        # ── Unconditional EOD square-off (§3.10, §6.2) ───────────────────
        squareoff = (
            datetime.combine(today, MARKET_CLOSE)
            - timedelta(minutes=self.p.squareoff_before_close_min)
        ).time()
        if t >= squareoff:
            if self._position_lots() > 0 and not self._eod_exit_emitted:
                self._eod_exit_emitted = True
                return ScalpDecision(
                    action="EXIT",
                    side="SELL",
                    option_type=self._ladder_option_type,
                    strike=self._ladder_strike,
                    lots=self._position_lots(),
                    reason="EOD square-off (unconditional)",
                )
            return None

        # ── Intrabar price book update (for signal next tick) ─────────────
        h = high if high is not None else underlying_price
        l = low if low is not None else underlying_price
        self._ingest_bar(underlying_price, h, l)

        # ── Compute current direction signal ──────────────────────────────
        warmup_ok = self._is_warm(now)
        if self._signal_override is not None:
            # Test seam: use the pinned direction (never set in production).
            self._current_direction = self._signal_override
        else:
            self._current_direction = (
                direction_signal(
                    self._closes, self._highs, self._lows,
                    self.p, self.vwap,
                    self._orb_high, self._orb_low,
                )
                if warmup_ok
                else "FLAT"
            )

        # ── Exits on open tranches (before entry logic) ───────────────────
        if self._tranches:
            exit_dec = self._evaluate_exits(now, underlying_price, option_premium)
            if exit_dec is not None:
                return exit_dec

        # ── Entry logic ───────────────────────────────────────────────────
        if not self._pending_entry:
            enter_dec = self._evaluate_entry(now, underlying_price, t)
            if enter_dec is not None:
                self._pending_entry = True
                return enter_dec

        return None

    # ── Exit evaluation ───────────────────────────────────────────────────

    def _evaluate_exits(
        self,
        now: datetime,
        underlying_price: float,
        option_premium: Optional[float],
    ) -> Optional[ScalpDecision]:
        """Check all exit conditions in priority order."""

        # 5. Signal-flip — flatten entire ladder (§3.5)
        if self._ladder_direction == "LONG" and self._current_direction == "SHORT":
            return self._flatten_all("Signal-flip LONG→SHORT")
        if self._ladder_direction == "SHORT" and self._current_direction == "LONG":
            return self._flatten_all("Signal-flip SHORT→LONG")

        # 8. Daily-loss kill (already standing down, but a position somehow open)
        if self._standing_down and self._position_lots() > 0:
            return self._flatten_all("Daily-loss stand-down — flatten residual")

        if option_premium is None:
            # Fail-safe: no premium-dependent exit fabrication (§6.6)
            logger.warning(
                "[OptionsScalper %s] position open but option_premium=None — "
                "suppressing premium-based exits (fail-safe)",
                self.security_id,
            )
            return None

        # Update high-water marks on all tranches
        for tr in self._tranches:
            if option_premium > tr.hi_water_premium:
                tr.hi_water_premium = option_premium

        # 3. Hard stop — check each tranche (§3.3)
        stop_lots = 0
        for tr in self._tranches:
            stop_price = tr.entry_premium * (1 - self.p.stop_pct)
            if option_premium <= stop_price:
                stop_lots += tr.lots

        if stop_lots > 0:
            # All breach → flatten all for efficiency
            total = self._position_lots()
            if stop_lots >= total:
                return self._flatten_all(
                    f"Hard stop hit (premium ₹{option_premium:.2f})"
                )
            # Partial stop (some tranches breached)
            return ScalpDecision(
                action="EXIT",
                side="SELL",
                option_type=self._ladder_option_type,
                strike=self._ladder_strike,
                lots=stop_lots,
                reason=f"Hard stop partial (premium ₹{option_premium:.2f})",
            )

        # 4. Time-stop — check each tranche (§3.4)
        time_stop_lots = 0
        for tr in self._tranches:
            if tr.tp_index == 0:  # hasn't hit first TP yet
                age = (now - tr.fill_time).total_seconds() / 60
                if age >= self.p.time_stop_min:
                    time_stop_lots += tr.lots

        if time_stop_lots > 0:
            if time_stop_lots >= self._position_lots():
                return self._flatten_all(
                    f"Time-stop (theta guard ≥{self.p.time_stop_min}min)"
                )
            return ScalpDecision(
                action="EXIT",
                side="SELL",
                option_type=self._ladder_option_type,
                strike=self._ladder_strike,
                lots=time_stop_lots,
                reason=f"Time-stop partial (≥{self.p.time_stop_min}min)",
            )

        # 1+2. TP ladder + trailing remainder (§2b, §3.1, §3.2)
        #
        # Uses a SINGLE class-level _tp_ladder_index so successive partial exits
        # hit TP[0], then TP[1], then TP[2] in order, regardless of which tranche
        # is at the front of the book.  Once the last TP level is consumed the
        # remaining tranche converts to trailing.
        if self._tranches:
            tr = self._tranches[0]

            # Already in trailing phase for the last remaining tranche?
            if tr.trailing:
                # Update high-water mark on this tick (premium already updated above)
                if option_premium > tr.hi_water_premium:
                    tr.hi_water_premium = option_premium
                trail_floor = tr.hi_water_premium * (1 - self.p.trail_pct)
                if option_premium <= trail_floor:
                    return ScalpDecision(
                        action="EXIT",
                        side="SELL",
                        option_type=self._ladder_option_type,
                        strike=self._ladder_strike,
                        lots=tr.lots,
                        reason=(
                            f"Trail hit ₹{option_premium:.2f} "
                            f"(high-water ₹{tr.hi_water_premium:.2f}, "
                            f"floor ₹{trail_floor:.2f})"
                        ),
                    )
            else:
                # TP ladder — use the class-level index to advance through levels
                tp_levels = self.p.tp_ladder_pct
                if self._tp_ladder_index < len(tp_levels):
                    # Target is measured against THIS tranche's entry premium
                    tp_pct = tp_levels[self._tp_ladder_index]
                    tp_target = tr.entry_premium * (1 + tp_pct)
                    if option_premium >= tp_target:
                        tp_idx = self._tp_ladder_index
                        lots_to_exit = tr.lots  # one tranche per TP level
                        is_last_tp = (tp_idx == len(tp_levels) - 1)

                        # Advance the class-level index so the next partial exit
                        # hits the next TP level.
                        self._tp_ladder_index += 1

                        if is_last_tp:
                            # The final TP level was reached.  After the caller
                            # calls notify_fill to remove this tranche, any
                            # remaining tranche becomes the trailing remainder.
                            # Pre-mark the next tranche now so it trails
                            # immediately on the next tick.
                            if len(self._tranches) > 1:
                                self._tranches[1].trailing = True
                                self._tranches[1].hi_water_premium = max(
                                    self._tranches[1].hi_water_premium,
                                    option_premium,
                                )

                        return ScalpDecision(
                            action="EXIT",
                            side="SELL",
                            option_type=self._ladder_option_type,
                            strike=self._ladder_strike,
                            lots=lots_to_exit,
                            reason=(
                                f"TP[{tp_idx}] +{tp_pct*100:.0f}% "
                                f"(premium ₹{option_premium:.2f})"
                            ),
                        )

        return None

    # ── Entry evaluation ──────────────────────────────────────────────────

    def _evaluate_entry(
        self,
        now: datetime,
        underlying_price: float,
        t: dtime,
    ) -> Optional[ScalpDecision]:
        """Decide whether to open a new ladder rung."""

        # Warm-up gate (§1e, §3.9)
        if not self._is_warm(now):
            return None

        # Trade window
        if not self._bypass_time_guards:
            trade_open_dt = (
                datetime.combine(now.date(), MARKET_OPEN)
                + timedelta(minutes=self.p.no_trade_open_min)
            ).time()
            trade_close_dt = (
                datetime.combine(now.date(), MARKET_CLOSE)
                - timedelta(minutes=self.p.no_trade_close_min)
            ).time()
            if t < trade_open_dt or t >= trade_close_dt:
                return None

        # Standing-down (daily-loss kill)
        if self._standing_down:
            return None

        # Check if we're in an active ladder
        if self._tranches:
            return self._evaluate_ladder_add(now, underlying_price)
        else:
            return self._evaluate_new_ladder(now, underlying_price)

    def _evaluate_new_ladder(
        self, now: datetime, underlying_price: float
    ) -> Optional[ScalpDecision]:
        """Try to open the first rung of a brand-new ladder."""

        # Cooldown check (§3.6)
        if self._cooldown_until is not None:
            cmp_now = now if now.tzinfo is not None else now.replace(tzinfo=IST)
            cmp_cd = (
                self._cooldown_until
                if self._cooldown_until.tzinfo is not None
                else self._cooldown_until.replace(tzinfo=IST)
            )
            if cmp_now < cmp_cd:
                return None

        # Max trades cap (§3.7)
        if self._trades_today >= self.p.max_trades:
            return None

        # Signal must not be FLAT
        direction = self._current_direction
        if direction == "FLAT":
            return None

        # Compute strike
        atm = _round_to_step(underlying_price, self.p.step)
        if direction == "LONG":
            option_type = "CE"
            # strike_offset: 0 = ATM, -1 = ITM (ATM-step), +1 = OTM (ATM+step)
            strike = atm + self.p.strike_offset * self.p.step
        else:  # SHORT
            option_type = "PE"
            strike = atm - self.p.strike_offset * self.p.step

        lots = self._tranche_lots_for_rung(1)

        # Record ladder context (fixed for the life of this ladder)
        self._ladder_direction = direction
        self._ladder_option_type = option_type
        self._ladder_strike = strike
        self._ladder_anchor_underlying = underlying_price
        self._last_rung_underlying = underlying_price
        self._rungs_requested = 1

        self._trades_today += 1
        logger.info(
            "[OptionsScalper %s] NEW LADDER #%d: %s %s%d, underlying=%.2f",
            self.security_id,
            self._trades_today,
            direction,
            option_type,
            strike,
            underlying_price,
        )

        return ScalpDecision(
            action="ENTER",
            side="BUY",
            option_type=option_type,
            strike=strike,
            lots=lots,
            reason=(
                f"Ladder open [{direction}] signal={self.p.signal} "
                f"underlying={underlying_price:.2f}"
            ),
        )

    def _evaluate_ladder_add(
        self, now: datetime, underlying_price: float
    ) -> Optional[ScalpDecision]:
        """Try to add a rung to the existing ladder (pyramiding)."""

        # Max rungs cap
        if self._rungs_requested >= self.p.max_rungs:
            return None

        # Anchor bias must still match current direction (no add on flip or flat)
        if self._current_direction != self._ladder_direction:
            return None

        move = underlying_price - self._last_rung_underlying
        if self._ladder_direction == "LONG":
            favorable_move = move
        else:
            favorable_move = -move

        if self.p.ladder_mode == "pyramid":
            # Add when underlying moved +rung_spacing_pts further in favor
            if favorable_move < self.p.rung_spacing_pts:
                return None
        elif self.p.ladder_mode == "scale_in_dips":
            # Add when underlying retraced rung_spacing_pts against the trade
            if favorable_move > -self.p.rung_spacing_pts:
                return None

        self._rungs_requested += 1
        self._last_rung_underlying = underlying_price
        lots = self._tranche_lots_for_rung(self._rungs_requested)

        logger.info(
            "[OptionsScalper %s] LADDER ADD rung %d, underlying=%.2f",
            self.security_id, self._rungs_requested, underlying_price,
        )

        return ScalpDecision(
            action="ENTER",
            side="BUY",
            option_type=self._ladder_option_type,
            strike=self._ladder_strike,
            lots=lots,
            reason=(
                f"Ladder add rung {self._rungs_requested} "
                f"(+{favorable_move:.1f} pts in favor)"
            ),
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _flatten_all(self, reason: str) -> ScalpDecision:
        """Emit an EXIT for all open lots (does not mutate state — caller calls notify_fill/flat)."""
        total = self._position_lots()
        logger.info(
            "[OptionsScalper %s] FLATTEN ALL %d lots — %s",
            self.security_id, total, reason,
        )
        return ScalpDecision(
            action="EXIT",
            side="SELL",
            option_type=self._ladder_option_type,
            strike=self._ladder_strike,
            lots=total,
            reason=reason,
        )

    def _on_fully_flat(self, now: Optional[datetime]) -> None:
        """Called when tranches drop to zero (full flatten confirmed via fills)."""
        if now is not None:
            self._last_flatten_time = now
            self._cooldown_until = now + timedelta(minutes=self.p.cooldown_min)

        # Reset ladder context
        self._ladder_direction = ""
        self._ladder_option_type = ""
        self._ladder_strike = 0
        self._ladder_anchor_underlying = 0.0
        self._last_rung_underlying = 0.0
        self._rungs_requested = 0
        self._tp_ladder_index = 0
        self._trailing_active = False
        self._trailing_hi_water = 0.0
        self._pending_entry = False

    def _position_lots(self) -> int:
        return sum(t.lots for t in self._tranches)

    def _tranche_lots_for_rung(self, rung_number: int) -> int:
        """Lots for a given rung (1-indexed) based on ladder_size_mode."""
        if self.p.ladder_size_mode == "flat":
            return self.p.tranche_lots
        elif self.p.ladder_size_mode == "decreasing":
            # 1, then halve, floor 1
            lots = self.p.tranche_lots
            for _ in range(rung_number - 1):
                lots = max(1, lots // 2)
            return lots
        return self.p.tranche_lots

    def _is_warm(self, now: datetime) -> bool:
        """True if we've seen enough bars and passed the warmup window."""
        if not self._bypass_time_guards:
            warmup_dt = (
                datetime.combine(now.date(), MARKET_OPEN)
                + timedelta(minutes=self.p.warmup_minutes)
            )
            now_naive = now.replace(tzinfo=None) if now.tzinfo is not None else now
            warmup_naive = (
                warmup_dt.replace(tzinfo=None)
                if warmup_dt.tzinfo is not None
                else warmup_dt
            )
            if now_naive < warmup_naive:
                return False
        required = max(self.p.ema_slow, self.p.mom_k + 1, self.p.atr_window)
        return self._bars_seen >= required

    def _ingest_bar(self, close: float, high: float, low: float) -> None:
        """Update rolling window and VWAP accumulator."""
        self._bars_seen += 1

        # VWAP (typical price proxy, unit volume)
        tp = (high + low + close) / 3
        self._vwap_cum_tp += tp
        self._vwap_cum_v += 1
        self.vwap = self._vwap_cum_tp / self._vwap_cum_v

        # Rolling window (bounded)
        self._closes.append(close)
        self._highs.append(high)
        self._lows.append(low)
        if len(self._closes) > self._max_window:
            self._closes.pop(0)
            self._highs.pop(0)
            self._lows.pop(0)

        # ORB update (signal == "orb")
        if self.p.signal == "orb" and not self._orb_locked:
            orb_end_bars = self.p.orb_minutes
            if self._bars_seen <= orb_end_bars:
                self._orb_high = max(self._orb_high, high)
                self._orb_low = min(self._orb_low, low)
            else:
                self._orb_locked = True

    def _update_realized_pnl(self, lots: int, entry_premium: float,
                              exit_premium: float) -> None:
        """Accumulate realized P&L (net, in ₹)."""
        pnl = (exit_premium - entry_premium) * lots * self.p.lot
        self._daily_realized_pnl += pnl
        if self._daily_realized_pnl <= -self.p.daily_loss_cap:
            logger.warning(
                "[OptionsScalper %s] Daily-loss cap hit ₹%.0f — standing down",
                self.security_id, self._daily_realized_pnl,
            )
            self._standing_down = True

    def _reset_session(self, today: date) -> None:
        self._session_date = today
        self._vwap_cum_tp = 0.0
        self._vwap_cum_v = 0.0
        self.vwap = 0.0
        self._orb_high = 0.0
        self._orb_low = float("inf")
        self._orb_locked = False
        self._closes.clear()
        self._highs.clear()
        self._lows.clear()
        self._bars_seen = 0
        self._current_direction = "FLAT"
        self._signal_override = None
        self._bypass_time_guards = False
        self._tranches.clear()
        self._ladder_direction = ""
        self._ladder_option_type = ""
        self._ladder_strike = 0
        self._ladder_anchor_underlying = 0.0
        self._last_rung_underlying = 0.0
        self._rungs_requested = 0
        self._tp_ladder_index = 0
        self._trailing_active = False
        self._trailing_hi_water = 0.0
        self._trades_today = 0
        self._daily_realized_pnl = 0.0
        self._standing_down = False
        self._cooldown_until = None
        self._last_flatten_time = None
        self._pending_entry = False
        self._eod_exit_emitted = False
        logger.info("[OptionsScalper %s] New session %s — reset", self.security_id, today)

    def status(self) -> dict:
        return {
            "security_id": self.security_id,
            "session_date": str(self._session_date),
            "vwap": round(self.vwap, 2),
            "direction": self._current_direction,
            "tranches": len(self._tranches),
            "position_lots": self._position_lots(),
            "ladder_direction": self._ladder_direction,
            "ladder_strike": self._ladder_strike,
            "ladder_option_type": self._ladder_option_type,
            "rungs_requested": self._rungs_requested,
            "trades_today": self._trades_today,
            "daily_realized_pnl": round(self._daily_realized_pnl, 2),
            "standing_down": self._standing_down,
            "bars_seen": self._bars_seen,
            "warm": self._is_warm(datetime.now(IST)),
        }
