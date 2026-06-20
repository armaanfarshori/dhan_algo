"""
Minute-data backtest harness for the long-option intraday SCALPER.

This is the **honest** counterpart to the scalper spec set
(``strategies/scalper_specs/*``): instead of forward-paper-only validation it
REPLAYS real per-minute option bars (from ``option_chain_snapshot``, ingested by
``core/dhan_option_history`` via the Dhan rollingoption endpoint) against the
underlying's per-minute path, and prices every entry/exit through an explicit
FILL MODEL. The single biggest dishonesty in any options-scalp backtest is
hand-waving the fill — a +10% target on a ₹120 premium is ₹12, and the bid/ask
spread + slippage can be a large fraction of that. So the spread and slippage are
MODELLED EXPLICITLY and SWEPT as falsifiers; the report leads with **net
expectancy per scalp** and the **break-even win-rate**, not gross hit-rate.

What it does (per index, per day)
---------------------------------
FLAT → compute the ``vwap_mom`` direction signal on the UNDERLYING 1-min path
(reusing ``strategies.options_scalper.direction_signal`` + ``ScalperParams`` —
this harness never re-implements the signal), screen the ATM contract (snapshot
``strike_offset == 0`` — see the PSEUDO-strike note below), and ENTER via the
fill model.
OPEN → manage bar-by-bar with ADVERSE intra-bar sequencing (the stop is checked
on the bar LOW before the target on the bar HIGH; the trail ratchets on the bar
CLOSE), a 5-minute time-stop, a signal-flip exit, and an UNCONDITIONAL EOD exit
at 15:15 IST. This is a deliberately SIMPLER, single-position model than the live
``OptionsScalper`` ladder — one lot, one contract per day-trade — because the
backtest's job is to measure whether the *core* long-option scalp clears costs,
not to reproduce the full pyramiding book (which is forward-paper territory).

The honest gaps, stated up front (see ``CAVEATS``)
--------------------------------------------------
  * PSEUDO-strike offset, NOT a real strike. The rollingoption ingest stores the
    ATM±k OFFSET (0, ±1, …) in ``option_chain_snapshot.strike`` — the true
    numeric strike is resolved server-side and is NOT recorded. So we select the
    ATM leg by ``strike == 0`` and trust the ingest's ATM tracking; we never map
    to a real strike.
  * bid/ask = None in the snapshot → the spread is MODELLED (default ₹1.0;
    swept 0.5/1.0/2.0), never read. The premium PATH itself is REAL (replayed
    bars); only the crossing cost is synthetic. We NEVER synthesize the premium
    path.
  * Underlying 1-min path: read from ``index_bars`` (timeframe 1min) when present;
    otherwise we FLAG the STRADDLE-PROXY fallback (reconstruct an underlying-move
    proxy from the CE−PE premium path) and run reduced-confidence.
  * PRELIMINARY · PAPER / research only. Forward-paper with a live ATM CE/PE LTP
    stream is the real go/no-go — a modelled spread can flatter or punish the EV
    and only a live fill log settles it.

Lane discipline: this module REUSES the signal (``options_scalper``), the cost
stack (``research.backtest.fno_costs``) and the drawdown helper
(``research.backtest.fno_strategies._max_drawdown``). It owns only the replay
loop, the fill model, and the metrics/sweep/report — it never re-implements the
signal, the charges, or the drawdown math.

Determinism: a fixed RNG seed (``--seed``, default 7) governs any stochastic
choice (currently none beyond reproducible ordering; the seed is wired so future
jitter stays reproducible). Replay is otherwise fully deterministic.

CLI
---
    python -m research.backtest.scalper_backtest \
        --underlying NIFTY --from 2026-01-01 --to 2026-06-18 \
        --spread 1.0 --slip-pct 0.01 [--sweep] [--upload-s3]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

# ── reused surface — import, never re-implement (lane discipline) ──────────────
from research.backtest.fno_costs import NIFTY_LOT, condor_costs
from research.backtest.fno_strategies import _max_drawdown
from strategies.options_scalper import ScalperParams, direction_signal

logger = logging.getLogger("dhan.backtest.scalper")

# IST tz (mirror core/fno_backfill._IST — DB timestamps are tz-aware UTC; we bucket
# by IST trading day and gate the EOD exit on the IST wall clock).
_IST = timezone(timedelta(hours=5, minutes=30))

# Trading-session anchors (IST). Entries stop near the close; the EOD exit is
# unconditional at 15:15 (matches the harness spec — earlier than the live
# engine's 15:25 square-off, deliberately conservative for a backtest).
MARKET_OPEN = time(9, 15)
EOD_EXIT_TIME = time(15, 15)
NO_NEW_ENTRY_AFTER = time(15, 5)  # do not OPEN a fresh scalp this late

# Lot sizes per index (units/lot). Reused from the cost stack for NIFTY; BANKNIFTY
# is 30 (core/instruments.INDEX_CONFIGS). The backtest trades exactly one lot.
LOT_BY_UNDERLYING: dict[str, int] = {"NIFTY": NIFTY_LOT, "BANKNIFTY": 30}

CAVEATS = (
    "MODELLED SPREAD (bid/ask=None in snapshot — spread synthetic, swept 0.5/1/2) "
    "· PSEUDO-strike offset (ATM=0; true strike server-side, not recorded) "
    "· slippage modelled as % of premium (swept 0.5/1/2%) "
    "· single-position single-lot (NOT the full live ladder — no pyramiding P&L) "
    "· NO daily-loss stand-down / NO inter-trade cooldown / NO max-trades cap "
    "(the live engine has all three; the backtest can over-trade a bad day) "
    "· time-stop 5min vs live 12min (cuts stagnant scalps harder — conservative on "
    "those, but raises trade count) "
    "· underlying path from index_bars 1min, else STRADDLE-PROXY fallback (flagged) "
    "· premium PATH is REAL (never synthesized); only spread/slip are modelled "
    "· PRELIMINARY · PAPER/research only — forward-paper with a live LTP stream is "
    "the real go/no-go"
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Bar dataclasses
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Bar:
    """One minute OHLC bar (option premium or underlying price)."""

    t: datetime              # tz-aware (UTC in DB; bucketed by IST elsewhere)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    iv: Optional[float] = None  # raw percent (snapshot convention), CE+PE mean for ATM


# ─────────────────────────────────────────────────────────────────────────────
# 2. Fill model — the honest gap
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FillModel:
    """Models the crossing cost on a single option leg.

    fill = next-bar-OPEN ± spread/2 ± ref·slip_pct

    The premium PATH is real (replayed bars); only the spread (bid/ask absent in
    the snapshot) and slippage are modelled. A BUY pays UP (open + spread/2 +
    slip), a SELL receives DOWN (open − spread/2 − slip) — always adverse.

    ``spread`` is in ₹/unit (default 1.0; swept 0.5/1.0/2.0). ``slip_pct`` is a
    fraction of the reference premium (default 0.01; swept 0.005/0.01/0.02).
    ``iv_scale_spread`` optionally widens the spread when IV is high (a crude but
    honest "spreads blow out in vol" knob); off by default.
    """

    spread: float = 1.0
    slip_pct: float = 0.01
    iv_scale_spread: bool = False
    iv_scale_ref: float = 12.0   # IV (percent) at which the modelled spread is 1×

    def _effective_spread(self, iv: Optional[float]) -> float:
        if not self.iv_scale_spread or iv is None or self.iv_scale_ref <= 0:
            return self.spread
        # Linear scale: spread × (iv / iv_scale_ref), floored at the base spread.
        return max(self.spread, self.spread * (iv / self.iv_scale_ref))

    def buy_fill(self, ref_open: float, *, iv: Optional[float] = None) -> float:
        """Adverse BUY fill (pay up). ref_open is the next bar's open."""
        sp = self._effective_spread(iv)
        return ref_open + sp / 2.0 + ref_open * self.slip_pct

    def sell_fill(self, ref_open: float, *, iv: Optional[float] = None) -> float:
        """Adverse SELL fill (receive down). Floored at 0.05 (a premium can't go
        negative; a deep adverse model must not produce a <0 fill)."""
        sp = self._effective_spread(iv)
        return max(0.05, ref_open - sp / 2.0 - ref_open * self.slip_pct)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Cost helper — round-trip charges for a 1-lot scalp (BUY open + SELL close)
# ─────────────────────────────────────────────────────────────────────────────
def scalp_costs(entry_premium: float, exit_premium: float, qty: int) -> float:
    """Full statutory + brokerage cost for one round-trip scalp (2 legs).

    Reuses the options cost stack (``condor_costs``): one BUY leg (open) at
    ``entry_premium`` and one SELL leg (close) at ``exit_premium``, ``qty`` units
    each. No exercise (scalps are always closed in the market intraday). Returns
    the total ₹ cost. The fill-model slippage is applied SEPARATELY on the fill
    prices — this is the brokerage/STT/exchange/SEBI/stamp/GST stack only, so
    slippage is never double-counted.
    """
    legs = [
        (entry_premium, qty, "BUY"),
        (exit_premium, qty, "SELL"),
    ]
    return condor_costs(legs, exercise_intrinsic=0.0).total


# ─────────────────────────────────────────────────────────────────────────────
# 4. Trade record + exit reasons
# ─────────────────────────────────────────────────────────────────────────────
EXIT_HARD_STOP = "hard_stop"
EXIT_TARGET = "target"
EXIT_TRAIL = "trail"
EXIT_SIGNAL_FLIP = "signal_flip"
EXIT_TIME_STOP = "time_stop"
EXIT_EOD = "eod"


@dataclass(frozen=True)
class ScalpTrade:
    """One completed single-lot scalp."""

    day: date
    underlying: str
    option_type: str          # CE | PE
    direction: str            # LONG | SHORT (underlying bias)
    entry_time: datetime
    exit_time: datetime
    entry_premium: float      # fill price (incl. spread/slip)
    exit_premium: float       # fill price (incl. spread/slip)
    qty: int                  # units (one lot)
    exit_reason: str
    gross_pnl: float          # (exit − entry) × qty, fill-priced
    costs: float              # brokerage/STT/etc. round-trip
    net_pnl: float            # gross − costs
    dte: Optional[int] = None
    entry_iv: Optional[float] = None
    used_straddle_proxy: bool = False

    @property
    def hold_minutes(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds() / 60.0


# ─────────────────────────────────────────────────────────────────────────────
# 5. Replay parameters
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ReplayParams:
    """Per-trade exit discipline for the backtest (a SIMPLE single-lot subset of
    the live ladder spec). Defaults mirror ``strategies/scalper_specs/03_*``:
    hard stop −20%, first target +10% then trail 12% off the high-water, 5-min
    time-stop, signal-flip flatten, unconditional EOD exit."""

    stop_pct: float = 0.20            # hard stop, fraction of entry premium
    target_pct: float = 0.10          # take-profit / trail-arm threshold
    trail_pct: float = 0.12           # give-back off premium high-water
    use_trail: bool = True            # FALSIFIER knob: removing trail should
                                      #   collapse EV if the edge is real & thin
    time_stop_min: int = 5            # minutes before a flat scalp is cut
    flip_exit: bool = True            # exit on a signal flip against the position


# ─────────────────────────────────────────────────────────────────────────────
# 6. Per-day replay loop
# ─────────────────────────────────────────────────────────────────────────────
def _ist_time(b: Bar) -> time:
    """The bar's IST wall-clock time-of-day (tz-stripped) for session gating.
    DB bars are tz-aware UTC; converting to IST first keeps the EOD/entry gates
    correct regardless of how the timestamp is stored (TZ-safe)."""
    return b.t.astimezone(_IST).timetz().replace(tzinfo=None)


def replay_day(
    day: date,
    underlying: str,
    underlying_bars: list[Bar],
    ce_bars: list[Bar],
    pe_bars: list[Bar],
    *,
    params: Optional[ScalperParams] = None,
    replay: Optional[ReplayParams] = None,
    fill: Optional[FillModel] = None,
    qty: Optional[int] = None,
    dte: Optional[int] = None,
    used_straddle_proxy: bool = False,
    rng: Optional[random.Random] = None,
) -> list[ScalpTrade]:
    """Replay one trading day for one index and return the completed scalps.

    Parameters
    ----------
    underlying_bars : the underlying 1-min path (index_bars). If empty AND a
        straddle proxy is requested, ``used_straddle_proxy`` should be True and a
        proxy path is reconstructed from CE+PE (see ``straddle_proxy_underlying``).
    ce_bars / pe_bars : ATM CE / PE 1-min premium bars (snapshot offset 0).
    params : signal params (``direction_signal``). Defaults to ScalperParams().
    replay : exit discipline. Defaults to ReplayParams().
    fill   : the fill model. Defaults to FillModel().

    The loop is single-position: while flat, the FIRST minute the signal fires a
    LONG/SHORT (after warm-up + before NO_NEW_ENTRY_AFTER) opens an ATM CE/PE at
    the NEXT bar's open (fill-modelled). While open, each subsequent bar is
    managed with adverse intra-bar sequencing. After an exit, the scalp may
    re-arm the SAME day (a new signal opens a fresh position) — matching a scalper
    that trades repeatedly intraday. EOD exit at 15:15 is unconditional.
    """
    params = params or ScalperParams()
    replay = replay or ReplayParams()
    fill = fill or FillModel()
    rng = rng or random.Random(0)
    qty = qty or LOT_BY_UNDERLYING.get(underlying, NIFTY_LOT)

    # Index option bars by minute key for O(1) lookup against the underlying clock.
    ce_by_t = {b.t: b for b in ce_bars}
    pe_by_t = {b.t: b for b in pe_bars}

    # The driving clock is the underlying path (or the proxy). Sort defensively.
    ub = sorted(underlying_bars, key=lambda b: b.t)
    trades: list[ScalpTrade] = []
    if len(ub) < 2:
        return trades

    # Rolling underlying windows for the signal + a running VWAP (typical-price
    # proxy, unit volume — mirrors the live engine's VWAP accumulator).
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    vwap_cum_tp = 0.0
    vwap_cum_v = 0.0
    warmup_bars = max(params.ema_slow, params.mom_k + 1, params.atr_window)

    # Open-position state (single lot).
    open_pos: Optional[dict[str, Any]] = None

    for i, bar in enumerate(ub):
        # Ingest THIS bar into the signal window BEFORE evaluating (no look-ahead:
        # the trigger reads only closed bars; entries fill on the NEXT bar's open).
        closes.append(bar.close)
        highs.append(bar.high)
        lows.append(bar.low)
        tp = (bar.high + bar.low + bar.close) / 3.0
        vwap_cum_tp += tp
        vwap_cum_v += 1.0
        vwap = vwap_cum_tp / vwap_cum_v if vwap_cum_v else bar.close

        now_ist = _ist_time(bar)
        warm = (i + 1) >= warmup_bars and now_ist >= MARKET_OPEN
        direction = "FLAT"
        if warm:
            direction = direction_signal(closes, highs, lows, params, vwap)

        # ── manage an open position on THIS bar (using its own option OHLC) ──────
        if open_pos is not None:
            ot = open_pos["option_type"]
            opt_bar = (ce_by_t if ot == "CE" else pe_by_t).get(bar.t)
            # Track the last KNOWN option close so clock-driven exits on a
            # premium-missing minute use an honest reference, not the stale entry.
            if opt_bar is not None:
                open_pos["last_close"] = opt_bar.close
            # _manage_open mutates open_pos in place (armed_trail, hi_water).
            exit_now, reason, exit_ref = _manage_open(
                open_pos, opt_bar, now_ist, direction, replay
            )
            if exit_now:
                exit_px = fill.sell_fill(exit_ref, iv=open_pos.get("entry_iv"))
                trades.append(
                    _close_trade(open_pos, bar, exit_px, reason, day, underlying,
                                 qty, dte, used_straddle_proxy)
                )
                open_pos = None
                # do NOT re-enter on the same bar (avoids ping-pong / look-ahead)
                continue

        # ── consider a NEW entry when flat ──────────────────────────────────────
        if open_pos is None and direction in ("LONG", "SHORT"):
            if now_ist >= NO_NEW_ENTRY_AFTER or now_ist >= EOD_EXIT_TIME:
                continue
            if i + 1 >= len(ub):
                continue  # no next bar to fill against
            nxt = ub[i + 1]
            ot = "CE" if direction == "LONG" else "PE"
            entry_opt = (ce_by_t if ot == "CE" else pe_by_t).get(nxt.t)
            if entry_opt is None or entry_opt.open is None or entry_opt.open <= 0:
                continue  # no tradeable option bar at the fill instant
            entry_px = fill.buy_fill(entry_opt.open, iv=entry_opt.iv)
            open_pos = {
                "option_type": ot,
                "direction": direction,
                "entry_time": nxt.t,
                "entry_premium": entry_px,
                "entry_iv": entry_opt.iv,
                "hi_water": entry_px,
                "armed_trail": False,
                "last_close": entry_px,  # seeded; updated each managed bar
            }

    # ── unconditional EOD flatten of any residual position ──────────────────────
    if open_pos is not None and ub:
        last = ub[-1]
        ot = open_pos["option_type"]
        opt_bar = (ce_by_t if ot == "CE" else pe_by_t).get(last.t)
        # Honest fallback: the last KNOWN option close, not the stale entry premium.
        exit_ref = opt_bar.close if opt_bar is not None else open_pos.get("last_close", open_pos["entry_premium"])
        exit_px = fill.sell_fill(exit_ref, iv=open_pos.get("entry_iv"))
        trades.append(
            _close_trade(open_pos, last, exit_px, EXIT_EOD, day, underlying,
                         qty, dte, used_straddle_proxy)
        )

    return trades


def _manage_open(
    pos: dict[str, Any],
    opt_bar: Optional[Bar],
    now_ist: time,
    direction: str,
    replay: ReplayParams,
) -> tuple[bool, str, float]:
    """Decide whether to exit the open position on this bar.

    Returns (exit_now, reason, exit_reference_premium). The exit reference is the
    premium the SELL fill is computed from — set to the level that triggered the
    exit (stop level, target level, trail floor, or bar close) so the fill model
    applies its adverse spread/slip on top.

    ADVERSE INTRA-BAR SEQUENCING (the honesty rule): within a single bar we assume
    the WORST plausible ordering — check the hard STOP against the bar LOW first,
    THEN the TARGET against the bar HIGH. The trail ratchets on the bar CLOSE only
    (a high-water set mid-bar that we never actually saw must not arm a tighter
    trail than the close supports).

    EXIT PRIORITY: EOD > hard-stop > signal-flip > target/trail > time-stop. The
    hard stop precedes the flip on purpose (honesty): when a bar both breaches the
    stop on its low AND the signal flips, the stop is the harder, earlier intra-bar
    event and gives the WORSE (truer) fill (``stop_level`` < ``close``); letting the
    flip win there would book the more optimistic close-based fill.

    NOTE: this function MUTATES ``pos`` in place — ``armed_trail`` and ``hi_water``
    persist back to the caller's ``open_pos`` dict via the shared reference.

    ``last`` is the last KNOWN option close seen while the position was open (passed
    by the caller via ``pos['last_close']``); it is the honest exit reference for
    clock-driven exits on a minute with NO option bar — far better than the stale
    entry premium (which would book a phantom zero-gross trade)."""
    entry = pos["entry_premium"]
    last = pos.get("last_close", entry)  # last known option close (honest fallback)

    # 1. EOD — unconditional (highest priority).
    if now_ist >= EOD_EXIT_TIME:
        ref = opt_bar.close if opt_bar is not None else last
        return True, EXIT_EOD, ref

    # 2. HARD STOP — before the flip (see EXIT PRIORITY note). Checked on the bar
    #    LOW (adverse: assume it dipped first within the minute).
    if opt_bar is not None:
        stop_level = entry * (1.0 - replay.stop_pct)
        if opt_bar.low <= stop_level:
            return True, EXIT_HARD_STOP, stop_level

    # 3. Signal flip — thesis gone, flatten whole (premium-independent).
    if replay.flip_exit and direction in ("LONG", "SHORT") and direction != pos["direction"]:
        ref = opt_bar.close if opt_bar is not None else last
        return True, EXIT_SIGNAL_FLIP, ref

    # Premium-dependent rules need a real option bar this minute.
    if opt_bar is None:
        # No premium this minute → only the time-stop (clock) can still fire; it
        # exits at the last KNOWN option close, not the stale entry premium.
        age = _combine_age(pos, now_ist)
        if age >= replay.time_stop_min and not pos["armed_trail"]:
            return True, EXIT_TIME_STOP, last
        return False, "", last

    target_level = entry * (1.0 + replay.target_pct)

    # 4. TARGET / TRAIL — checked on the bar HIGH after the stop.
    if not pos["armed_trail"]:
        if opt_bar.high >= target_level:
            if replay.use_trail:
                # Arm the trail; do NOT exit yet (let the runner trail from here).
                pos["armed_trail"] = True
                pos["hi_water"] = max(pos["hi_water"], opt_bar.close)
                # fall through to the trail check below this bar
            else:
                return True, EXIT_TARGET, target_level
    if pos["armed_trail"]:
        # Ratchet the high-water on the CLOSE (not the intra-bar high we never
        # confirmed filling at), then exit if the close gave back trail_pct.
        pos["hi_water"] = max(pos["hi_water"], opt_bar.close)
        trail_floor = pos["hi_water"] * (1.0 - replay.trail_pct)
        if opt_bar.close <= trail_floor:
            return True, EXIT_TRAIL, trail_floor

    # 5. Time-stop — a scalp that hasn't armed its target in time_stop_min is cut.
    age = _combine_age(pos, now_ist)
    if age >= replay.time_stop_min and not pos["armed_trail"]:
        return True, EXIT_TIME_STOP, opt_bar.close

    return False, "", opt_bar.close


def _combine_age(pos: dict[str, Any], now_ist: time) -> float:
    """Minutes since entry, computed on the IST wall clock (tz-safe — both ends
    are IST time-of-day on the same trading day)."""
    et = pos["entry_time"].astimezone(_IST).timetz().replace(tzinfo=None)
    e_min = et.hour * 60 + et.minute + et.second / 60.0
    n_min = now_ist.hour * 60 + now_ist.minute + now_ist.second / 60.0
    return n_min - e_min


def _close_trade(
    pos: dict[str, Any],
    bar: Bar,
    exit_px: float,
    reason: str,
    day: date,
    underlying: str,
    qty: int,
    dte: Optional[int],
    used_straddle_proxy: bool,
) -> ScalpTrade:
    entry = pos["entry_premium"]
    gross = (exit_px - entry) * qty
    costs = scalp_costs(entry, exit_px, qty)
    return ScalpTrade(
        day=day,
        underlying=underlying,
        option_type=pos["option_type"],
        direction=pos["direction"],
        entry_time=pos["entry_time"],
        exit_time=bar.t,
        entry_premium=round(entry, 4),
        exit_premium=round(exit_px, 4),
        qty=qty,
        exit_reason=reason,
        gross_pnl=round(gross, 2),
        costs=round(costs, 2),
        net_pnl=round(gross - costs, 2),
        dte=dte,
        entry_iv=pos.get("entry_iv"),
        used_straddle_proxy=used_straddle_proxy,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Straddle-proxy underlying (fallback when index_bars 1min is absent)
# ─────────────────────────────────────────────────────────────────────────────
def straddle_proxy_underlying(ce_bars: list[Bar], pe_bars: list[Bar]) -> list[Bar]:
    """Reconstruct a crude underlying-MOVE proxy from the CE−PE premium path when
    no real 1-min underlying bar is available.

    The signal only needs a directional path with relative highs/lows, not an
    absolute price: ``proxy = call_premium − put_premium`` tracks the underlying
    monotonically near ATM (call rises / put falls as spot rises). We re-base it
    to a positive synthetic level (+1000) so VWAP/momentum ratios stay finite.
    This is FLAGGED (``used_straddle_proxy=True``) and is reduced-confidence — the
    report states it explicitly. Returns [] if the legs don't overlap in time.
    """
    pe_by_t = {b.t: b for b in pe_bars}
    out: list[Bar] = []
    base = 1000.0
    for c in sorted(ce_bars, key=lambda b: b.t):
        p = pe_by_t.get(c.t)
        if p is None:
            continue
        o = base + (c.open - p.open)
        h = base + (c.high - p.low)   # widest up-move within the bar
        lo = base + (c.low - p.high)  # widest down-move within the bar
        cl = base + (c.close - p.close)
        out.append(Bar(t=c.t, open=o, high=max(o, h, cl), low=min(o, lo, cl),
                       close=cl, volume=c.volume))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 8. Metrics
# ─────────────────────────────────────────────────────────────────────────────
def _break_even_win_rate(avg_win: float, avg_loss: float) -> float:
    """Win-rate at which net expectancy is zero, given the realised average win
    and average loss (avg_loss is a POSITIVE magnitude). BE_WR = L / (W + L).
    Returns 1.0 when there are no wins (can never break even) and 0.0 when there
    are no losses."""
    if avg_win <= 0 and avg_loss <= 0:
        return 0.0
    if avg_win <= 0:
        return 1.0
    if avg_loss <= 0:
        return 0.0
    return avg_loss / (avg_win + avg_loss)


def compute_metrics(trades: list[ScalpTrade]) -> dict[str, Any]:
    """Compute the headline + diagnostic metrics for a set of scalps.

    Headline = NET expectancy per scalp. Also: win-rate, break-even win-rate,
    profit factor, gross-vs-net, fat-tail concentration (% of net profit from the
    top-decile winners), daily P&L / max drawdown / longest losing streak, and the
    by-cell (DTE × time-of-day × IV regime) and by-exit-reason breakdowns.
    """
    n = len(trades)
    if n == 0:
        return {"n_trades": 0, "note": "no trades"}

    nets = [t.net_pnl for t in trades]
    grosses = [t.gross_pnl for t in trades]
    costs = [t.costs for t in trades]
    wins = [p for p in nets if p > 0]
    losses = [p for p in nets if p < 0]

    gross_profit = sum(p for p in nets if p > 0)
    gross_loss = -sum(p for p in nets if p < 0)  # positive magnitude
    # profit_factor stays a real float (math.inf for zero-loss) — NEVER a string —
    # so sweep cells / downstream code can compare it numerically. The report
    # stringifies it (with a "no losses in sample" qualifier) at the print boundary.
    profit_factor = (gross_profit / gross_loss) if gross_loss > 1e-9 else math.inf

    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (-sum(losses) / len(losses)) if losses else 0.0  # positive

    total_gross = sum(grosses)
    total_net = sum(nets)

    # Fat-tail: share of total NET PROFIT from the top-decile winners. Only
    # meaningful when the system is a NET WINNER — a "share of profit" is undefined
    # when total_net <= 0, so we return None there rather than a misleading
    # negative/blown-up percentage (agent QA: a losing system used to print e.g.
    # -250%, which reads as nonsense).
    sorted_nets = sorted(nets, reverse=True)
    decile = max(1, n // 10)
    top_decile_sum = sum(sorted_nets[:decile])
    fat_tail_pct = (top_decile_sum / total_net * 100.0) if total_net > 1e-9 else None

    # Daily P&L / max drawdown / longest loss streak (chronological by exit time).
    chrono = sorted(trades, key=lambda t: t.exit_time)
    daily: dict[date, float] = {}
    for t in chrono:
        daily[t.day] = daily.get(t.day, 0.0) + t.net_pnl
    daily_pnls = [daily[d] for d in sorted(daily)]
    max_dd = _max_drawdown(daily_pnls)

    longest_loss_streak = _longest_loss_streak([t.net_pnl for t in chrono])

    return {
        "n_trades": n,
        "net_expectancy": round(total_net / n, 2),       # HEADLINE
        "total_net_pnl": round(total_net, 2),
        "total_gross_pnl": round(total_gross, 2),
        "total_costs": round(sum(costs), 2),
        "win_rate": round(len(wins) / n, 4),
        "break_even_win_rate": round(_break_even_win_rate(avg_win, avg_loss), 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        # Real float (math.inf when no losses). zero_loss_sample flags the inf case
        # so the report can qualify it ("no losses in sample — not a real PF").
        "profit_factor": (round(profit_factor, 3) if math.isfinite(profit_factor)
                          else profit_factor),
        "zero_loss_sample": gross_loss <= 1e-9,
        # Cost drag is only well-defined when GROSS is positive (a negative gross
        # flips the ratio's sign); None otherwise rather than a sign-inverted value.
        "gross_vs_net_drag_pct": (round((1 - total_net / total_gross) * 100.0, 2)
                                  if total_gross > 1e-9 else None),
        "fat_tail_top_decile_pct": (round(fat_tail_pct, 2) if fat_tail_pct is not None
                                    else None),
        "n_days": len(daily),
        "avg_daily_pnl": round(sum(daily_pnls) / len(daily_pnls), 2) if daily_pnls else 0.0,
        "max_daily_pnl": round(max(daily_pnls), 2) if daily_pnls else 0.0,
        "min_daily_pnl": round(min(daily_pnls), 2) if daily_pnls else 0.0,
        "max_drawdown": round(max_dd, 2),
        "longest_loss_streak": longest_loss_streak,
        "by_exit_reason": _by_exit_reason(trades),
        "by_cell": _by_cell(trades),
        "used_straddle_proxy": any(t.used_straddle_proxy for t in trades),
    }


def _longest_loss_streak(pnls: list[float]) -> int:
    longest = 0
    cur = 0
    for p in pnls:
        if p < 0:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    return longest


def _by_exit_reason(trades: list[ScalpTrade]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for t in trades:
        d = out.setdefault(t.exit_reason, {"n": 0, "net": 0.0})
        d["n"] += 1
        d["net"] += t.net_pnl
    for r, d in out.items():
        d["net"] = round(d["net"], 2)
        d["avg_net"] = round(d["net"] / d["n"], 2) if d["n"] else 0.0
    return out


def _time_bucket(t: ScalpTrade) -> str:
    h = t.entry_time.astimezone(_IST).hour
    if h < 11:
        return "open(9-11)"
    if h < 13:
        return "mid(11-13)"
    return "close(13-15)"


def _iv_bucket(t: ScalpTrade) -> str:
    if t.entry_iv is None:
        return "iv_na"
    if t.entry_iv < 11.0:
        return "iv_low(<11)"
    if t.entry_iv < 15.0:
        return "iv_mid(11-15)"
    return "iv_high(>=15)"


def _dte_bucket(t: ScalpTrade) -> str:
    if t.dte is None:
        return "dte_na"
    if t.dte <= 0:
        return "dte_0(expiry)"
    if t.dte == 1:
        return "dte_1"
    return "dte_2+"


def _by_cell(trades: list[ScalpTrade]) -> dict[str, dict[str, Any]]:
    """By-cell = DTE × time-of-day × IV-regime expectancy table."""
    out: dict[str, dict[str, Any]] = {}
    for t in trades:
        key = f"{_dte_bucket(t)}|{_time_bucket(t)}|{_iv_bucket(t)}"
        d = out.setdefault(key, {"n": 0, "net": 0.0})
        d["n"] += 1
        d["net"] += t.net_pnl
    for k, d in out.items():
        d["net"] = round(d["net"], 2)
        d["expectancy"] = round(d["net"] / d["n"], 2) if d["n"] else 0.0
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 9. Falsifier sweeps
# ─────────────────────────────────────────────────────────────────────────────
SWEEP_SPREADS = (0.5, 1.0, 2.0)
SWEEP_SLIP_PCTS = (0.005, 0.01, 0.02)
SWEEP_TRAIL = (True, False)


def run_sweep(
    days: list[dict[str, Any]],
    underlying: str,
    *,
    params: Optional[ScalperParams] = None,
    base_replay: Optional[ReplayParams] = None,
    seed: int = 7,
    spreads: tuple[float, ...] = SWEEP_SPREADS,
    slip_pcts: tuple[float, ...] = SWEEP_SLIP_PCTS,
    trail_opts: tuple[bool, ...] = SWEEP_TRAIL,
) -> dict[str, Any]:
    """Run the MANDATORY falsifier sweeps: slippage 0.5/1/2%, spread 0.5/1/2,
    trail on/off. ``days`` is a list of per-day replay inputs (see
    ``run_backtest``). Returns a dict with the base config metrics + every sweep
    cell's headline metrics. Removing the trail SHOULD collapse EV if the edge is
    real and thin — that drop is the key falsifier readout.
    """
    params = params or ScalperParams()
    base_replay = base_replay or ReplayParams()

    cells: list[dict[str, Any]] = []
    for spread in spreads:
        for slip in slip_pcts:
            for use_trail in trail_opts:
                rng = random.Random(seed)
                fill = FillModel(spread=spread, slip_pct=slip)
                rp = ReplayParams(
                    stop_pct=base_replay.stop_pct, target_pct=base_replay.target_pct,
                    trail_pct=base_replay.trail_pct, use_trail=use_trail,
                    time_stop_min=base_replay.time_stop_min, flip_exit=base_replay.flip_exit,
                )
                trades: list[ScalpTrade] = []
                for d in days:
                    trades.extend(
                        replay_day(
                            d["day"], underlying, d["underlying_bars"],
                            d["ce_bars"], d["pe_bars"],
                            params=params, replay=rp, fill=fill,
                            qty=d.get("qty"), dte=d.get("dte"),
                            used_straddle_proxy=d.get("used_straddle_proxy", False),
                            rng=rng,
                        )
                    )
                m = compute_metrics(trades)
                pf = m.get("profit_factor", 0.0)
                # JSON-safe: inf → "inf" string at this terminal display boundary
                # (cells are summary output, never consumed numerically downstream).
                pf_cell = "inf" if (isinstance(pf, float) and not math.isfinite(pf)) else pf
                cells.append({
                    "spread": spread, "slip_pct": slip, "use_trail": use_trail,
                    "n_trades": m.get("n_trades", 0),
                    "net_expectancy": m.get("net_expectancy", 0.0),
                    "win_rate": m.get("win_rate", 0.0),
                    "break_even_win_rate": m.get("break_even_win_rate", 0.0),
                    "profit_factor": pf_cell,
                    "total_net_pnl": m.get("total_net_pnl", 0.0),
                })
    return {"underlying": underlying, "cells": cells}


def run_backtest(
    days: list[dict[str, Any]],
    underlying: str,
    *,
    params: Optional[ScalperParams] = None,
    replay: Optional[ReplayParams] = None,
    fill: Optional[FillModel] = None,
    seed: int = 7,
) -> dict[str, Any]:
    """Replay all ``days`` for one index at the BASE config and return
    {metrics, trades}. ``days`` items: {day, underlying_bars, ce_bars, pe_bars,
    [qty], [dte], [used_straddle_proxy]}."""
    params = params or ScalperParams()
    replay = replay or ReplayParams()
    fill = fill or FillModel()
    rng = random.Random(seed)
    trades: list[ScalpTrade] = []
    for d in days:
        trades.extend(
            replay_day(
                d["day"], underlying, d["underlying_bars"], d["ce_bars"], d["pe_bars"],
                params=params, replay=replay, fill=fill,
                qty=d.get("qty"), dte=d.get("dte"),
                used_straddle_proxy=d.get("used_straddle_proxy", False), rng=rng,
            )
        )
    return {"metrics": compute_metrics(trades), "trades": trades}


# ─────────────────────────────────────────────────────────────────────────────
# 10. Report
# ─────────────────────────────────────────────────────────────────────────────
def format_report(
    underlying: str,
    base: dict[str, Any],
    sweep: Optional[dict[str, Any]],
    *,
    args: Optional[dict[str, Any]] = None,
) -> str:
    """Human-readable report. Leads with the MODELLED-SPREAD caveat + PRELIMINARY."""
    m = base["metrics"]
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(f"SCALPER MINUTE-DATA BACKTEST — {underlying}")
    lines.append("=" * 78)
    lines.append("CAVEATS: " + CAVEATS)
    lines.append("NOTE: the premium PATH is REAL (replayed bars) — we NEVER synthesize")
    lines.append("      it; only the bid/ask SPREAD and slippage are modelled on top.")
    lines.append("DECISION: PRELIMINARY — forward-paper with a live ATM LTP stream is")
    lines.append("          the real go/no-go. A modelled spread can flatter OR punish EV.")
    if m.get("used_straddle_proxy"):
        lines.append("WARNING: STRADDLE-PROXY underlying path used (no index_bars 1min) — "
                     "reduced confidence.")
    if args:
        lines.append("ARGS: " + json.dumps(args, default=str))
    lines.append("-" * 78)
    if m.get("n_trades", 0) == 0:
        lines.append("NO TRADES.")
        return "\n".join(lines)
    lines.append(f"HEADLINE  net expectancy / scalp : ₹{m['net_expectancy']}")
    lines.append(f"          trades                  : {m['n_trades']} over {m['n_days']} days")
    lines.append(f"          win-rate                : {m['win_rate']:.1%}")
    lines.append(f"          break-even win-rate     : {m['break_even_win_rate']:.1%}")
    pf = m["profit_factor"]
    pf_str = ("inf (no losses in sample — NOT a real PF, sample too thin)"
              if m.get("zero_loss_sample") else f"{pf}")
    lines.append(f"          profit factor           : {pf_str}")
    lines.append(f"          gross / net total       : ₹{m['total_gross_pnl']} / ₹{m['total_net_pnl']}")
    drag = m.get("gross_vs_net_drag_pct")
    lines.append(f"          cost drag (gross→net)   : "
                 f"{f'{drag}%' if drag is not None else 'n/a (gross ≤ 0)'}")
    ft = m.get("fat_tail_top_decile_pct")
    lines.append(f"          fat-tail (top-decile)   : "
                 f"{f'{ft}% of net profit' if ft is not None else 'n/a (net ≤ 0 — no profit to share)'}")
    lines.append(f"          avg/max/min daily P&L   : ₹{m['avg_daily_pnl']} / "
                 f"₹{m['max_daily_pnl']} / ₹{m['min_daily_pnl']}")
    lines.append(f"          max drawdown            : ₹{m['max_drawdown']}")
    lines.append(f"          longest loss streak     : {m['longest_loss_streak']} trades")
    lines.append("-" * 78)
    lines.append("BY EXIT REASON:")
    for r, d in sorted(m["by_exit_reason"].items()):
        lines.append(f"  {r:<14} n={d['n']:<5} net=₹{d['net']:<12} avg=₹{d['avg_net']}")
    lines.append("-" * 78)
    lines.append("BY CELL (DTE × time × IV regime):")
    for k, d in sorted(m["by_cell"].items()):
        lines.append(f"  {k:<40} n={d['n']:<5} exp=₹{d['expectancy']}")
    if sweep is not None:
        lines.append("-" * 78)
        lines.append("FALSIFIER SWEEPS (slippage × spread × trail):")
        lines.append(f"  {'spread':>7} {'slip%':>7} {'trail':>6} {'n':>6} "
                     f"{'exp₹':>10} {'win%':>7} {'BE-win%':>8} {'PF':>7}")
        for c in sweep["cells"]:
            lines.append(
                f"  {c['spread']:>7} {c['slip_pct']*100:>6.1f}% {str(c['use_trail']):>6} "
                f"{c['n_trades']:>6} {c['net_expectancy']:>10} "
                f"{c['win_rate']*100:>6.1f}% {c['break_even_win_rate']*100:>7.1f}% "
                f"{str(c['profit_factor']):>7}"
            )
        lines.append("  NOTE: removing the trail (trail=False) SHOULD collapse EV if the edge")
        lines.append("        is real and thin. A flat EV across spread/slip is also a red flag.")
    lines.append("=" * 78)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 11. DB loaders (lazy — never touched by the pure tests)
# ─────────────────────────────────────────────────────────────────────────────
def _load_underlying_minute_bars(
    security_id: str, day: date
) -> list[Bar]:  # pragma: no cover — needs the DB
    """Read the index 1-min path for ``day`` from index_bars (timeframe '1min').
    Returns [] when absent (caller then flags the straddle-proxy fallback)."""
    from sqlalchemy import text  # noqa: PLC0415

    from db import get_session  # noqa: PLC0415

    start = datetime.combine(day, time(0, 0), tzinfo=_IST)
    end = start + timedelta(days=1)
    with get_session() as session:
        rows = session.execute(
            text(
                "SELECT time, open, high, low, close, volume FROM index_bars "
                "WHERE security_id = :sid AND timeframe = '1min' "
                "AND time >= :s AND time < :e ORDER BY time"
            ),
            {"sid": str(security_id), "s": start, "e": end},
        ).fetchall()
    return [
        Bar(t=r[0], open=float(r[1]), high=float(r[2]), low=float(r[3]),
            close=float(r[4]), volume=float(r[5] or 0))
        for r in rows
    ]


def _load_atm_option_bars(
    underlying_scrip: int, day: date
) -> tuple[list[Bar], list[Bar], Optional[date]]:  # pragma: no cover — needs the DB
    """Read the ATM (pseudo-strike offset 0) CE + PE 1-min bars for ``day`` from
    option_chain_snapshot. OHLC is recovered from the ``raw`` JSONB the
    rollingoption ingest stored (ltp = close; open/high/low live in raw). Returns
    (ce_bars, pe_bars, front_expiry_for_day)."""
    from sqlalchemy import text  # noqa: PLC0415

    from db import get_session  # noqa: PLC0415

    start = datetime.combine(day, time(0, 0), tzinfo=_IST)
    end = start + timedelta(days=1)
    with get_session() as session:
        rows = session.execute(
            text(
                "SELECT snapshot_time, option_type, expiry_date, ltp, iv, volume, raw "
                "FROM option_chain_snapshot "
                "WHERE underlying_scrip = :u AND strike = 0 "
                "AND snapshot_time >= :s AND snapshot_time < :e "
                "ORDER BY snapshot_time"
            ),
            {"u": underlying_scrip, "s": start, "e": end},
        ).fetchall()
    ce: list[Bar] = []
    pe: list[Bar] = []
    expiry: Optional[date] = None
    for st, ot, exp, ltp, iv, vol, raw in rows:
        expiry = exp or expiry
        raw = raw or {}
        o = raw.get("open")
        h = raw.get("high")
        lo = raw.get("low")
        cl = raw.get("close") if raw.get("close") is not None else ltp
        if cl is None:
            continue
        cl = float(cl)
        bar = Bar(
            t=st,
            open=float(o) if o is not None else cl,
            high=float(h) if h is not None else cl,
            low=float(lo) if lo is not None else cl,
            close=cl,
            volume=float(vol or 0),
            iv=float(iv) if iv is not None else None,
        )
        (ce if ot == "CE" else pe).append(bar)
    return ce, pe, expiry


def load_days_from_db(
    underlying: str, from_date: date, to_date: date
) -> list[dict[str, Any]]:  # pragma: no cover — needs the DB
    """Assemble per-day replay inputs from the DB over [from_date, to_date].

    For each trading day with ATM option bars: pulls the index 1-min underlying
    path (falls back to the straddle proxy + flags it if absent), the ATM CE/PE
    bars, and computes DTE from the day's front expiry. Days with no option bars
    are skipped."""
    from core.dhan_option_history import resolve_underlying  # noqa: PLC0415

    u = resolve_underlying(underlying)
    days: list[dict[str, Any]] = []
    d = from_date
    while d <= to_date:
        ce, pe, expiry = _load_atm_option_bars(u.security_id, d)
        if not ce and not pe:
            d += timedelta(days=1)
            continue
        ub = _load_underlying_minute_bars(str(u.security_id), d)
        used_proxy = False
        if len(ub) < 2:
            ub = straddle_proxy_underlying(ce, pe)
            used_proxy = True
            logger.warning(
                "no index_bars 1min for %s on %s — using STRADDLE-PROXY underlying "
                "(reduced confidence)", underlying, d,
            )
        dte = max((expiry - d).days, 0) if expiry else None
        days.append({
            "day": d, "underlying_bars": ub, "ce_bars": ce, "pe_bars": pe,
            "qty": LOT_BY_UNDERLYING.get(underlying, NIFTY_LOT),
            "dte": dte, "used_straddle_proxy": used_proxy,
        })
        d += timedelta(days=1)
    return days


# ─────────────────────────────────────────────────────────────────────────────
# 12. S3 archive (sibling of strike_sweep/orchestrator) — fail-open
# ─────────────────────────────────────────────────────────────────────────────
def archive_s3(
    base: dict[str, Any],
    sweep: Optional[dict[str, Any]],
    *,
    args: dict[str, Any],
    summary_text: str,
) -> Optional[str]:  # pragma: no cover — needs network/boto3
    """Write results.json + summary.txt + manifest.json under
    ``s3://<S3_BUCKET>/kronos/m3/scalper/<ts>/`` (sibling of strike_sweep). Fail-open."""
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        print("WARNING: --upload-s3 set but S3_BUCKET is empty — skipping upload.")
        return None
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"kronos/m3/scalper/{run_ts}"
    # Trades are dataclasses → serialise the metrics + sweep only (trades can be huge).
    # Sanitise a possibly-inf profit_factor to a string so the JSON stays STRICT
    # (json.dumps would otherwise emit a non-standard `Infinity` token).
    metrics = dict(base["metrics"])
    pf = metrics.get("profit_factor")
    if isinstance(pf, float) and not math.isfinite(pf):
        metrics["profit_factor"] = "inf"
    results = {"metrics": metrics, "sweep": sweep}
    manifest = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "args": args,
        "caveats": CAVEATS.split(" · "),
        "decision": "PRELIMINARY",
    }
    try:
        import boto3  # noqa: PLC0415

        s3 = boto3.client(
            "s3",
            region_name=os.environ.get(
                "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
            ),
        )
        s3.put_object(Bucket=bucket, Key=f"{prefix}/results.json",
                      Body=json.dumps(results, indent=2, default=str).encode())
        s3.put_object(Bucket=bucket, Key=f"{prefix}/summary.txt",
                      Body=summary_text.encode())
        s3.put_object(Bucket=bucket, Key=f"{prefix}/manifest.json",
                      Body=json.dumps(manifest, indent=2, default=str).encode())
        uri = f"s3://{bucket}/{prefix}/"
        print(f"Archived scalper backtest results to {uri}")
        return uri
    except Exception as exc:  # noqa: BLE001 — fail-open
        print(f"WARNING: S3 archival failed ({exc}) — results NOT uploaded.")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 13. CLI (DB init only after arg-parse so --help never touches it)
# ─────────────────────────────────────────────────────────────────────────────
def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        # Surface as an argparse error (clean exit, BEFORE the DB is touched) rather
        # than an unhandled ValueError traceback after init_db.
        raise argparse.ArgumentTypeError(f"--from/--to must be YYYY-MM-DD; got {s!r}") from exc


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Minute-data backtest harness for the long-option scalper "
                    "(replays real option bars; spread/slippage MODELLED + swept)."
    )
    p.add_argument("--underlying", default="NIFTY", help="index symbol (NIFTY, BANKNIFTY)")
    p.add_argument("--from", dest="from_date", type=_parse_date, required=True, help="YYYY-MM-DD")
    p.add_argument("--to", dest="to_date", type=_parse_date, required=True, help="YYYY-MM-DD")
    p.add_argument("--spread", type=float, default=1.0, help="modelled bid/ask spread ₹ (base)")
    p.add_argument("--slip-pct", type=float, default=0.01, help="slippage as fraction of premium (base)")
    p.add_argument("--signal", default="vwap_mom",
                   choices=["vwap_mom", "ema", "orb", "momentum"],
                   help="direction signal (default vwap_mom)")
    p.add_argument("--stop-pct", type=float, default=0.20)
    p.add_argument("--target-pct", type=float, default=0.10)
    p.add_argument("--trail-pct", type=float, default=0.12)
    p.add_argument("--no-trail", action="store_true", help="disable the trailing remainder")
    p.add_argument("--time-stop-min", type=int, default=5)
    p.add_argument("--seed", type=int, default=7, help="RNG seed (determinism)")
    p.add_argument("--sweep", action="store_true", help="run the mandatory falsifier sweeps")
    p.add_argument("--upload-s3", action="store_true", help="archive results to S3 (fail-open)")
    return p


def main() -> None:  # pragma: no cover — needs the dhan_trading DB
    """``python -m research.backtest.scalper_backtest --underlying NIFTY --from … --to …``."""
    logging.basicConfig(level=logging.INFO)
    args = _build_arg_parser().parse_args()

    from config import get_config  # noqa: PLC0415
    from db import init_db  # noqa: PLC0415

    init_db(get_config().db_url)

    from_d = args.from_date  # already a date (argparse type=_parse_date)
    to_d = args.to_date

    params = ScalperParams(signal=args.signal)
    replay = ReplayParams(
        stop_pct=args.stop_pct, target_pct=args.target_pct, trail_pct=args.trail_pct,
        use_trail=not args.no_trail, time_stop_min=args.time_stop_min,
    )
    fill = FillModel(spread=args.spread, slip_pct=args.slip_pct)

    days = load_days_from_db(args.underlying, from_d, to_d)
    base = run_backtest(days, args.underlying, params=params, replay=replay,
                        fill=fill, seed=args.seed)
    sweep = (run_sweep(days, args.underlying, params=params, base_replay=replay,
                       seed=args.seed) if args.sweep else None)

    arg_dict = vars(args)
    report = format_report(args.underlying, base, sweep, args=arg_dict)
    print(report)

    if args.upload_s3:
        archive_s3(base, sweep, args=arg_dict, summary_text=report)


if __name__ == "__main__":
    main()
