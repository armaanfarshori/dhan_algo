"""
Iron-condor backtest harness for NIFTY weekly cycles (daily-step).

Implements Phase 1 of the F&O backtest spec (handoff §7).  It is pure /
deterministic: no DB access, no network calls.  Cycle data is passed in
by the caller; a separate DB loader (future step) will hydrate `cycles`.

Pricing
-------
Black-76 undiscounted: F ≈ spot (index options priced off spot; funding cost
omitted at daily resolution — negligible for sub-10-DTE NIFTY weeklies and
consistent with the handoff's "implied move from straddle_iv" approach).

    d1 = (ln(F/K) + 0.5·σ²·T) / (σ·√T)
    d2 = d1 − σ·√T
    call = F·Φ(d1) − K·Φ(d2)   (undiscounted; multiply by discount factor
                                  if needed in future)
    put  = K·Φ(−d2) − F·Φ(−d1)

CDF approximation: standard erf formula — math-only, no scipy dependency.

Payoff at expiry (short iron condor)
-------------------------------------
    credit        = (short_put + short_call) − (long_put + long_call)
    max_loss      = wing_width − credit          (wing_width = step * wing_strikes)
    gross_pnl     = credit − max(0, short_put_k − S) − max(0, S − short_call_k)
                           + max(0, long_put_k  − S) + max(0, S − long_call_k)

All quantities are per-unit (single option).  Multiply by lot size for ₹ P&L.

Costs
-----
Delegates to ``research.backtest.fno_costs.condor_costs`` and ``slippage``
(sibling module, written in parallel — see locked interface in the task brief).

Gate
----
Delegates to ``ml.fno_vol_gate.gate_decision`` (new module, written in
parallel).  Only ``SELL_PREMIUM`` cycles are traded.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# ---------------------------------------------------------------------------
# Sibling-module imports (written in parallel; typed against their locked
# interfaces so this file is ruff-clean + ast-parseable immediately).
# ---------------------------------------------------------------------------
from core.fno_derived import implied_move as _implied_move
from ml.fno_vol_gate import SELL_PREMIUM, gate_decision
from research.backtest.fno_costs import NIFTY_LOT, condor_costs, slippage

# db and sqlalchemy are lazy-imported inside cycles_from_db so that
# the pure pricing / backtest functions remain DB-free.

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SQRT2 = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)

logger = logging.getLogger("dhan.backtest.fno_condor")

# ---------------------------------------------------------------------------
# Day-count convention (Phase-0a fidelity fix #2)
# ---------------------------------------------------------------------------
# India VIX (and any ATM-straddle IV) is a CALENDAR-day annualised implied vol:
# its √-time scaling uses 365 calendar days, and `implied_move` /
# `price_condor` correctly use T = dte / 365 (dte is calendar days). Black-76
# pricing is therefore internally consistent (calendar IV × calendar T).
#
# The ONLY day-count inconsistency in the pipeline is the vol-regime GATE
# comparison: `realized_vol_20d` is annualised with √252 (TRADING_DAYS, per
# core.fno_derived.realized_vol_series) while `straddle_iv` (VIX/100) is
# annualised with √365. Comparing them directly mixes the two bases and biases
# the realized/implied ratio by √(365/252) ≈ 1.204 (~20%) — exactly the
# brief's "~20% error". We convert the realized vol onto the calendar (365)
# basis before the gate compares it to the calendar-annualised implied vol so
# both sides share ONE convention: CALENDAR (365) days.
CALENDAR_DAYS = 365
TRADING_DAYS = 252
# realized(√252) → realized(√365): a vol annualised over N_a periods/yr maps to
# basis N_b by multiplying by √(N_a / N_b). 252 trading → 365 calendar.
REALIZED_TO_CALENDAR_FACTOR = math.sqrt(TRADING_DAYS / CALENDAR_DAYS)  # ≈ 0.8307


def realized_vol_to_calendar_basis(
    realized_vol_trading: float | None,
    trading_days: int = TRADING_DAYS,
    calendar_days: int = CALENDAR_DAYS,
) -> float | None:
    """Convert a √(trading-day)-annualised realized vol onto the calendar (365)
    basis so it is directly comparable to a calendar-annualised implied vol
    (India VIX / ATM straddle IV).

    realized_vol_20d (core.fno_derived) annualises daily vol with √252; VIX
    annualises with √365. Multiply by √(252/365) to rebase. ``None`` passes
    through. See the module-level day-count note (fidelity fix #2).
    """
    if realized_vol_trading is None:
        return None
    return float(realized_vol_trading) * math.sqrt(trading_days / calendar_days)


# ---------------------------------------------------------------------------
# Index / underlying configuration (Phase-0a fidelity fix #1, index-agnostic)
# ---------------------------------------------------------------------------
# F&O code must NOT be NIFTY-hardcoded — the platform is moving multi-index.
# Each tradable underlying carries its own contract spec: security ids, lot
# size, strike grid, and (critically) its weekly-expiry weekday rule, which
# differs per index and changes over time (NSE moved NIFTY weeklies Thursday→
# Tuesday on a cutover date; BANKNIFTY/FINNIFTY weeklies were discontinued →
# monthly-only). All expiry-weekday logic below takes an ``IndexConfig`` so a
# non-NIFTY underlying drives its own rule; ``NIFTY`` is the default.
#
# Python weekday(): Monday=0 ... Sunday=6 → Tuesday=1, Thursday=3.
_TUESDAY = 1
_THURSDAY = 3

# NIFTY weekly-expiry Thursday→Tuesday cutover. (Project date convention:
# real-life 2025-09-01 is represented here as 2026-09-01 — the repo runs one
# year ahead of the real-world calendar; today is 2026-06-20.) Kept as a
# module-level constant for the NIFTY default + backward-compatible imports.
NIFTY_TUESDAY_EXPIRY_CUTOVER = date(2026, 9, 1)


@dataclass(frozen=True)
class IndexConfig:
    """Contract spec for one F&O underlying (index-agnostic).

    Carries everything the backtest needs that is index-specific so no NIFTY
    constant leaks into the pricing / cycle-building code.

    Attributes
    ----------
    symbol:
        Ticker key (e.g. ``"NIFTY"``) used against ``expiry_calendar``.
    security_id:
        ``security_id`` of the index row in ``index_bars`` (string — Dhan ids
        are strings; NIFTY = ``"13"``).
    vix_security_id:
        ``security_id`` of the volatility index used as the IV proxy (India VIX
        = ``"21"`` for NIFTY). ``None`` for indices without a dedicated vol index.
    lot_size:
        Contract lot size (units per lot). NIFTY = 65.
    strike_step:
        Strike-grid spacing in index points. NIFTY = 50.
    has_weeklies:
        Whether the index has weekly expiries. ``False`` → monthly-only
        (e.g. BANKNIFTY/FINNIFTY after weeklies were discontinued); the weekly
        backtest path then snaps to the monthly-expiry weekday only.
    expiry_weekday:
        Python weekday int (Mon=0..Sun=6) of the (current) weekly/monthly
        expiry. For NIFTY this is the POST-cutover weekday (Tuesday=1).
    pre_cutover_weekday:
        Expiry weekday BEFORE ``cutover_date`` (NIFTY = Thursday=3). ``None``
        when the index never changed its expiry weekday.
    cutover_date:
        Date on/after which ``expiry_weekday`` applies and before which
        ``pre_cutover_weekday`` applies. ``None`` when there is no cutover.
    """

    symbol: str
    security_id: str
    vix_security_id: str | None
    lot_size: int
    strike_step: int
    has_weeklies: bool
    expiry_weekday: int
    pre_cutover_weekday: int | None = None
    cutover_date: date | None = None


# Default underlying — NIFTY. Weeklies exist; expiry weekday is Thursday before
# the 2026-09-01 cutover and Tuesday on/after it. security_id=13, vix=21,
# lot=65, strike grid 50.
NIFTY = IndexConfig(
    symbol="NIFTY",
    security_id="13",
    vix_security_id="21",
    lot_size=65,
    strike_step=50,
    has_weeklies=True,
    expiry_weekday=_TUESDAY,
    pre_cutover_weekday=_THURSDAY,
    cutover_date=NIFTY_TUESDAY_EXPIRY_CUTOVER,
)


def expiry_weekday_for(cycle_date: date, index: IndexConfig = NIFTY) -> int:
    """Return ``index``'s weekly/monthly-expiry weekday (Python weekday int)
    effective on ``cycle_date``.

    Date-aware: when the index defines a ``cutover_date`` + ``pre_cutover_weekday``
    (e.g. NIFTY's Thursday→Tuesday move) the pre-cutover weekday applies before
    the cutover and ``expiry_weekday`` on/after it. Indices with no cutover use
    ``expiry_weekday`` unconditionally. Multi-year backtests therefore use the
    correct expiry day for each cycle instead of a single hardcoded weekday
    (fidelity fix #1) — and a non-NIFTY underlying drives its own rule.
    """
    if (
        index.cutover_date is not None
        and index.pre_cutover_weekday is not None
        and cycle_date < index.cutover_date
    ):
        return index.pre_cutover_weekday
    return index.expiry_weekday


def snap_to_expiry_weekday(
    trading_days_in_week: list[date],
    index: IndexConfig = NIFTY,
) -> date | None:
    """Pick the expiry-day boundary from an ISO week's available trading days.

    Given the sorted trading days that fall in one ISO week, return the trading
    day that best represents the expiry for that week, using ``index``'s rule:

    * Target weekday = ``expiry_weekday_for`` of the week's last trading day
      (e.g. NIFTY Thursday before the cutover, Tuesday on/after).
    * If a trading day on the target weekday exists, use it (holiday-free week).
    * Otherwise the expiry-day was a holiday → fall back to the last trading day
      on or before the target weekday (NSE rolls a holiday expiry to the prior
      trading day); if none precede it, use the week's last trading day.

    Returns ``None`` for an empty week. Index-agnostic: a non-NIFTY underlying
    (different weekday / monthly-only) snaps to its own expiry weekday. This
    keeps synthetic cycle boundaries aligned to the real expiry weekday +
    holiday rule, replacing the old "last trading day of the ISO week" heuristic.
    """
    if not trading_days_in_week:
        return None
    days = sorted(trading_days_in_week)
    target_wd = expiry_weekday_for(days[-1], index=index)

    exact = [d for d in days if d.weekday() == target_wd]
    if exact:
        return exact[-1]

    # Holiday on the expiry weekday → roll back to the last trading day on/before it.
    on_or_before = [d for d in days if d.weekday() <= target_wd]
    if on_or_before:
        return on_or_before[-1]
    return days[-1]


# ---------------------------------------------------------------------------
# IV source resolution (Phase-0a fidelity fix #3)
# ---------------------------------------------------------------------------
# Prefer forward-collected REAL ATM straddle IV (option_atm_iv.straddle_iv,
# projected from option_chain_snapshot) over the India-VIX-as-IV proxy whenever
# it exists for a cycle. Real IV is the per-expiry (~4-7 DTE) ATM IV; the VIX
# proxy is a 30-day IV that the 2026-06-19 live snapshot showed sits ~0.75×
# above the true short-tenor IV in calm regimes — i.e. the proxy is OPTIMISTIC
# in calm/contango. Using real IV where available removes that bias for those
# cycles. The source is returned + logged + stored per cycle.

IV_SOURCE_REAL = "real_atm"      # option_atm_iv.straddle_iv (forward-collected)
IV_SOURCE_VIX_PROXY = "vix_proxy"  # India VIX / 100 (historical fallback)


def resolve_iv_source(cycle: dict[str, Any]) -> tuple[float | None, str]:
    """Pick the IV to price a cycle, preferring real ATM IV over the VIX proxy.

    Looks for a real per-expiry ATM straddle IV on the cycle under
    ``atm_straddle_iv`` (annualised fraction, e.g. 0.10). When present and
    positive it is used (source = ``real_atm``). Otherwise falls back to the
    cycle's ``straddle_iv`` (the VIX/100 proxy assembled by ``cycles_from_db``),
    source = ``vix_proxy``.

    Returns ``(iv, source_label)``. ``iv`` may be ``None`` if neither is a usable
    positive value (the caller then skips the cycle).
    """
    real = cycle.get("atm_straddle_iv")
    if real is not None:
        try:
            real_f = float(real)
            if real_f > 0:
                return real_f, IV_SOURCE_REAL
        except (TypeError, ValueError):
            pass

    proxy = cycle.get("straddle_iv")
    if proxy is not None:
        try:
            proxy_f = float(proxy)
            if proxy_f > 0:
                return proxy_f, IV_SOURCE_VIX_PROXY
        except (TypeError, ValueError):
            pass

    return None, IV_SOURCE_VIX_PROXY


# ---------------------------------------------------------------------------
# Settlement-price resolution (Phase-0a fidelity fix #4)
# ---------------------------------------------------------------------------
# NSE's official Final Settlement Price (FSP) for index options is the weighted
# average of the underlying over the last half hour (15:00–15:30 IST) on expiry
# day, NOT the last-tick close. Prefer real FSP when collected; else the best
# available proxy (last-half-hour VWAP); else the bar close. The residual
# limitation is flagged in go_no_go when no real FSP was used.

FSP_SOURCE_OFFICIAL = "fsp_official"      # cycle["fsp"] — NSE final settlement
FSP_SOURCE_HALFHOUR_VWAP = "halfhour_vwap"  # last-30-min VWAP proxy
FSP_SOURCE_CLOSE_PROXY = "close_proxy"      # bar close fallback (least faithful)


def resolve_settlement_price(cycle: dict[str, Any]) -> tuple[float | None, str]:
    """Pick the expiry settlement price, preferring NSE FSP over proxies.

    Preference order:
      1. ``fsp``                         → official NSE Final Settlement Price.
      2. ``expiry_halfhour_vwap``        → 15:00–15:30 VWAP proxy (best proxy).
      3. ``expiry_spot``                 → bar close (least faithful fallback).

    Returns ``(settlement_price, source_label)``; ``settlement_price`` is
    ``None`` only when every candidate is missing/invalid (caller skips).
    """
    for key, label in (
        ("fsp", FSP_SOURCE_OFFICIAL),
        ("expiry_halfhour_vwap", FSP_SOURCE_HALFHOUR_VWAP),
        ("expiry_spot", FSP_SOURCE_CLOSE_PROXY),
    ):
        val = cycle.get(key)
        if val is not None:
            try:
                val_f = float(val)
                if val_f > 0:
                    return val_f, label
            except (TypeError, ValueError):
                continue
    return None, FSP_SOURCE_CLOSE_PROXY


# ---------------------------------------------------------------------------
# Black-76 option pricing (undiscounted)
# ---------------------------------------------------------------------------

def _ncdf(x: float) -> float:
    """Standard normal CDF via erf: Φ(x) = 0.5 · (1 + erf(x / √2))."""
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def black76_call(F: float, K: float, T: float, sigma: float) -> float:
    """Undiscounted Black-76 call price.

    Parameters
    ----------
    F:     Forward price (approximated as spot for index options).
    K:     Strike.
    T:     Time to expiry in years (= DTE / 365).
    sigma: Annualised implied volatility (e.g. 0.15 for 15 %).

    Guards
    ------
    T ≤ 0 or sigma ≤ 0 → intrinsic value (max(F − K, 0)).
    """
    if T <= 0.0 or sigma <= 0.0:
        return max(F - K, 0.0)
    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return F * _ncdf(d1) - K * _ncdf(d2)


def black76_put(F: float, K: float, T: float, sigma: float) -> float:
    """Undiscounted Black-76 put price.

    Parameters and guards identical to :func:`black76_call`.
    Uses put-call parity: put = call − (F − K).
    """
    if T <= 0.0 or sigma <= 0.0:
        return max(K - F, 0.0)
    # put-call parity: P = C - (F - K)  (undiscounted)
    call = black76_call(F, K, T, sigma)
    return call - (F - K)


# ---------------------------------------------------------------------------
# Condor construction
# ---------------------------------------------------------------------------

def _round_to_step(value: float, step: int) -> int:
    """Round ``value`` to the nearest multiple of ``step`` (round-half-UP)."""
    return int(math.floor(value / step + 0.5)) * step


def build_condor(
    spot: float,
    expected_move: float,
    *,
    wing_strikes: int = 2,
    step: int = 50,
    move_mult: float = 1.5,
) -> dict[str, int]:
    """Compute the four strike prices of a NIFTY iron condor.

    Parameters
    ----------
    spot:          Current index level.
    expected_move: Implied move in index points (= spot · IV · √(DTE/365)).
                   Short strikes are placed at ± ``move_mult`` × this value.
    wing_strikes:  Number of ``step``-increments beyond the shorts for the
                   long (protection) legs.  Default 2 → wing_width = 100 pts.
    step:          Strike grid spacing.  NIFTY = 50.
    move_mult:     Multiplier applied to ``expected_move`` before rounding to
                   the strike grid.  Default 1.5 per handoff §7 (shorts ≈ ATM
                   ± 1.5 × expected_move).  Pass explicitly if you want a
                   different multiple.

    Returns
    -------
    dict with keys: ``short_put_k``, ``long_put_k``,
                    ``short_call_k``, ``long_call_k``.
    """
    short_put_k = _round_to_step(spot - move_mult * expected_move, step)
    short_call_k = _round_to_step(spot + move_mult * expected_move, step)
    long_put_k = short_put_k - wing_strikes * step
    long_call_k = short_call_k + wing_strikes * step
    return {
        "short_put_k": short_put_k,
        "long_put_k": long_put_k,
        "short_call_k": short_call_k,
        "long_call_k": long_call_k,
    }


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class CondorTrade:
    """Outcome record for a single weekly iron-condor cycle."""

    entry_date: date
    expiry_date: date
    strikes: dict[str, int]       # from build_condor
    credit: float                  # ₹ credit received per lot (after slippage)
    max_loss: float                 # ₹ max possible loss per lot
    gross_pnl: float               # ₹ before costs
    costs: float                   # ₹ total statutory + brokerage costs
    net_pnl: float                 # gross_pnl − costs
    win: bool                      # net_pnl > 0

    # Optional diagnostics — populated by run_backtest
    gate_decision_label: str = ""
    straddle_iv: float = 0.0
    realized_vol_20d: float = 0.0
    expiry_spot: float = 0.0
    leg_premiums: dict[str, float] = field(default_factory=dict)
    # Phase-0a fidelity provenance
    iv_source: str = ""            # real_atm | vix_proxy (fix #3)
    settlement_source: str = ""    # fsp_official | halfhour_vwap | close_proxy (fix #4)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def price_condor(
    spot: float,
    straddle_iv: float,
    dte: int,
    strikes: dict[str, int],
    lot: int = NIFTY_LOT,
) -> dict[str, Any]:
    """Price all four legs with Black-76 and return a pricing dict.

    Parameters
    ----------
    spot:        Index level at entry (used as the Black-76 forward F).
    straddle_iv: Annualised IV from the ATM straddle (e.g. 0.15 = 15 %).
                 A single IV is used for all four legs; skew is ignored at
                 this resolution (daily-step; Phase 0 does not hold a full
                 option chain with per-strike IV).
    dte:         Calendar days to expiry.
    strikes:     Output of :func:`build_condor`.
    lot:         Lot size (default NIFTY_LOT = 65).

    Returns
    -------
    dict with keys:
        ``"credit_per_unit"`` — net credit per unit (float).
        ``"credit_total"``    — credit_per_unit × lot (float).
        ``"leg_premiums"``    — dict of per-unit Black-76 premiums with keys
                                ``"short_put"``, ``"short_call"``,
                                ``"long_put"``, ``"long_call"``.
    """
    T = dte / 365.0
    F = spot
    iv = straddle_iv

    sp = black76_put(F, strikes["short_put_k"], T, iv)
    lp = black76_put(F, strikes["long_put_k"], T, iv)
    sc = black76_call(F, strikes["short_call_k"], T, iv)
    lc = black76_call(F, strikes["long_call_k"], T, iv)

    leg_premiums = {
        "short_put": sp,
        "long_put": lp,
        "short_call": sc,
        "long_call": lc,
    }

    # Credit per unit = short legs received − long legs paid
    credit_per_unit = (sp + sc) - (lp + lc)
    credit_total = credit_per_unit * lot
    return {
        "credit_per_unit": credit_per_unit,
        "credit_total": credit_total,
        "leg_premiums": leg_premiums,
    }


# ---------------------------------------------------------------------------
# Expiry resolution
# ---------------------------------------------------------------------------

def resolve_condor(
    strikes: dict[str, int],
    credit_per_unit: float,
    expiry_spot: float,
    lot: int = NIFTY_LOT,
) -> dict[str, Any]:
    """Compute iron-condor gross P&L at expiry.

    Standard short iron-condor payoff:
      - Full credit kept if ``expiry_spot`` finishes between the two short
        strikes.
      - Loss on the put side if expiry_spot < short_put_k.
      - Loss on the call side if expiry_spot > short_call_k.
      - Max loss capped at wing_width − credit (the long legs absorb beyond).

    Parameters
    ----------
    strikes:         Output of :func:`build_condor`.
    credit_per_unit: Net premium collected per unit at entry (after slippage).
    expiry_spot:     Index settlement price (NSE NIFTY final settlement value).
    lot:             Lot size.

    Returns
    -------
    dict with at least key ``"gross_pnl"`` — per-lot P&L in ₹ (negative = loss).
    """
    S = expiry_spot
    spk = strikes["short_put_k"]
    lpk = strikes["long_put_k"]
    sck = strikes["short_call_k"]
    lck = strikes["long_call_k"]

    # Intrinsic payoffs of the four legs at expiry (buyer's perspective)
    short_put_loss = max(spk - S, 0.0)
    long_put_gain = max(lpk - S, 0.0)   # noqa: SIM910 — clarity
    short_call_loss = max(S - sck, 0.0)
    long_call_gain = max(S - lck, 0.0)

    # Net loss from option payoffs (from the short-condor writer's perspective)
    option_payoff_loss = (short_put_loss - long_put_gain) + (short_call_loss - long_call_gain)

    # Gross P&L per unit: credit collected minus net option losses
    gross_per_unit = credit_per_unit - option_payoff_loss
    return {"gross_pnl": gross_per_unit * lot}


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------

def run_backtest(
    cycles: list[dict[str, Any]],
    k: float = 0.9,
    move_mult: float = 1.5,
    capital: float = 200_000.0,
) -> dict[str, Any]:
    """Run the iron-condor backtest over a list of weekly NIFTY cycles.

    Parameters
    ----------
    cycles:
        List of dicts, each representing one weekly expiry window:

        .. code-block:: python

            {
                "entry_date":       date,   # entry (first day of cycle)
                "expiry_date":      date,   # expiry day (Thu pre-cutover / Tue on/after)
                "spot":             float,  # index level at entry
                "straddle_iv":      float,  # VIX/100 proxy IV (annualised fraction)
                "dte":              int,    # calendar days to expiry from entry
                "realized_vol_20d": float,  # 20-day √252-annualised realized vol (NIFTY spot)
                "expiry_spot":      float,  # index daily CLOSE at expiry (settlement proxy)

                # ── Optional Phase-0a fidelity inputs (preferred when present) ──
                "atm_straddle_iv":  float,  # REAL per-expiry ATM IV → preferred over VIX (#3)
                "fsp":              float,  # NSE official Final Settlement Price (#4)
                "expiry_halfhour_vwap": float,  # 15:00-15:30 VWAP FSP proxy (#4)
            }

    k:
        Vol-gate threshold passed through to ``gate_decision``.  SELL_PREMIUM
        when ``realized_vol < k * implied_vol``.  Default 0.9; handoff §6 targets
        ~70 % pass rate.

        Day-count (fidelity fix #2): ``realized_vol_20d`` is √252-annualised
        while the implied vol (VIX/100 or real ATM IV) is √365-annualised. The
        gate rebases the realized vol onto the calendar (365) basis via
        ``realized_vol_to_calendar_basis`` BEFORE comparing, so both sides share
        ONE convention (calendar 365). This removes the ~√(365/252)≈1.204 (~20%)
        cross-basis bias in the realized/implied ratio. ``k`` should therefore be
        (re)calibrated against calendar-basis ratios.

    move_mult:
        Multiplier on implied move when placing short strikes.  Default 1.5 per
        handoff §7 (shorts ≈ ATM ± 1.5 × expected_move).  Pass explicitly if you
        want a different multiple.

    capital:
        Allocated capital in ₹ for return + drawdown calculations.

    Returns
    -------
    dict with keys:

    * ``"trades"``            — list of :class:`CondorTrade`
    * ``"n_cycles"``          — total cycles evaluated
    * ``"n_trades"``          — cycles where the gate said SELL_PREMIUM
    * ``"win_rate"``          — fraction of trades with net_pnl > 0
    * ``"profit_factor"``     — gross wins / abs(gross losses) (or inf if no losses)
    * ``"sharpe"``            — annualised per-trade Sharpe (std × √52 weekly)
    * ``"max_drawdown"``      — peak-to-trough on cumulative net_pnl (₹, positive = loss)
    * ``"net_pnl"``           — total net P&L (₹)
    * ``"return_on_capital"`` — net_pnl / capital
    * ``"go_no_go"``          — output of :func:`go_no_go`
    """
    trades: list[CondorTrade] = []

    for cycle in cycles:
        # Guard: skip malformed cycles (missing required keys or None-valued fields).
        # This allows non-cycles_from_db callers to pass partially-built dicts without
        # crashing.  Well-formed cycles are unaffected.
        # An IV is acceptable from either the real ATM field or the VIX proxy;
        # a settlement price from FSP / half-hour VWAP / close (fixes #3, #4).
        _has_iv = cycle.get("atm_straddle_iv") is not None or cycle.get("straddle_iv") is not None
        _has_settlement = (
            cycle.get("fsp") is not None
            or cycle.get("expiry_halfhour_vwap") is not None
            or cycle.get("expiry_spot") is not None
        )
        _missing_or_none = (
            cycle.get("spot") is None
            or not _has_iv
            or cycle.get("realized_vol_20d") is None
            or cycle.get("dte") is None
            or not _has_settlement
        )
        if _missing_or_none:
            logger.debug(
                "Cycle %s skipped: one or more required fields are missing/None.",
                cycle.get("cycle_id", cycle.get("entry_date", "<unknown>")),
            )
            continue

        entry_date: date = cycle.get("entry_date", date.today())
        expiry_date: date = cycle.get("expiry_date", date.today())
        spot: float = float(cycle["spot"])
        dte: int = int(cycle["dte"])
        realized_vol_20d: float = float(cycle["realized_vol_20d"])

        # IV source — prefer REAL per-expiry ATM IV over the VIX proxy (fix #3).
        straddle_iv_opt, iv_source = resolve_iv_source(cycle)
        if straddle_iv_opt is None or straddle_iv_opt <= 0:
            logger.debug(
                "Cycle %s skipped: no usable IV (atm/vix both missing/<=0).",
                cycle.get("cycle_id", entry_date),
            )
            continue
        straddle_iv: float = straddle_iv_opt

        # Settlement price — prefer NSE FSP, then half-hour VWAP, then close (fix #4).
        settlement_opt, settlement_source = resolve_settlement_price(cycle)
        if settlement_opt is None or settlement_opt <= 0:
            logger.debug(
                "Cycle %s skipped: no usable settlement price.",
                cycle.get("cycle_id", entry_date),
            )
            continue
        expiry_spot: float = settlement_opt

        # Gate decision — only trade on SELL_PREMIUM.
        # Day-count fix #2: rebase √252 realized vol onto the calendar (365)
        # basis so it is comparable to the calendar-annualised implied vol.
        realized_vol_cal = realized_vol_to_calendar_basis(realized_vol_20d)
        decision = gate_decision(realized_vol_cal, straddle_iv, k=k)
        logger.debug(
            "Cycle %s: iv_source=%s iv=%.4f settle_source=%s realized(252)=%.4f "
            "realized(365)=%.4f gate=%s",
            cycle.get("cycle_id", entry_date), iv_source, straddle_iv,
            settlement_source, realized_vol_20d, realized_vol_cal or 0.0, decision,
        )
        if decision != SELL_PREMIUM:
            logger.debug(
                "Cycle %s skipped: gate=%s (realized_vol=%.4f, straddle_iv=%.4f)",
                cycle.get("cycle_id", entry_date),
                decision,
                realized_vol_20d,
                straddle_iv,
            )
            continue

        # Implied move in index points via core.fno_derived.implied_move
        em = _implied_move(spot, straddle_iv, dte)
        if em is None or em <= 0:
            logger.debug(
                "Cycle %s skipped: implied_move is None or <=0 (spot=%.2f, iv=%.4f, dte=%d)",
                cycle.get("cycle_id", entry_date),
                spot,
                straddle_iv,
                dte,
            )
            continue

        # Build strikes
        strikes = build_condor(spot, em, move_mult=move_mult)

        # Price condor (no slippage yet — applied below to OTM wings only)
        price_result = price_condor(spot, straddle_iv, dte, strikes)
        leg_premiums = price_result["leg_premiums"]

        # Apply slippage to OTM wing premiums (wider bid-ask on OTM legs).
        # Short wings receive less; long wings cost more — both adverse.
        lp_slip = slippage(leg_premiums["long_put"])
        lc_slip = slippage(leg_premiums["long_call"])
        sp_slip = slippage(leg_premiums["short_put"])
        sc_slip = slippage(leg_premiums["short_call"])

        # Adjusted per-unit credit after slippage:
        # short legs receive (premium − slippage); long legs pay (premium + slippage)
        credit_per_unit_adj = (
            (leg_premiums["short_put"] - sp_slip)
            + (leg_premiums["short_call"] - sc_slip)
            - (leg_premiums["long_put"] + lp_slip)
            - (leg_premiums["long_call"] + lc_slip)
        )
        credit_per_lot_adj = credit_per_unit_adj * NIFTY_LOT

        # Max loss per lot (wing width in points × lot, minus credit).
        # Clamp to 0 — for high-IV cycles credit can exceed wing width in theory,
        # but the realised max loss can never be negative.
        wing_width_pts = strikes["short_put_k"] - strikes["long_put_k"]  # same for call side
        max_loss_per_lot = max(0.0, wing_width_pts - credit_per_unit_adj) * NIFTY_LOT

        # Gross P&L at expiry
        gross = resolve_condor(strikes, credit_per_unit_adj, expiry_spot)["gross_pnl"]

        # Exercise intrinsic for costs: amount of intrinsic value exercised at expiry
        # (relevant for STT on exercise — ITM at expiry means the long legs have value)
        S = expiry_spot
        lp_intrinsic = max(strikes["long_put_k"] - S, 0.0) * NIFTY_LOT
        lc_intrinsic = max(S - strikes["long_call_k"], 0.0) * NIFTY_LOT
        exercise_intrinsic = lp_intrinsic + lc_intrinsic

        # Leg list: (slippage-adjusted premium_per_unit, lot, side) for all 4 legs
        # Short legs filled at (mid - slip); long legs filled at (mid + slip).
        # Costs (STT, exchange fee, stamp) are charged on the executed price.
        legs = [
            (leg_premiums["short_put"] - sp_slip, NIFTY_LOT, "SELL"),
            (leg_premiums["long_put"] + lp_slip, NIFTY_LOT, "BUY"),
            (leg_premiums["short_call"] - sc_slip, NIFTY_LOT, "SELL"),
            (leg_premiums["long_call"] + lc_slip, NIFTY_LOT, "BUY"),
        ]
        cost_result = condor_costs(legs, exercise_intrinsic=exercise_intrinsic)
        total_costs = cost_result.total

        net = gross - total_costs

        trades.append(
            CondorTrade(
                entry_date=entry_date,
                expiry_date=expiry_date,
                strikes=strikes,
                credit=credit_per_lot_adj,
                max_loss=max_loss_per_lot,
                gross_pnl=gross,
                costs=total_costs,
                net_pnl=net,
                win=net > 0,
                gate_decision_label=decision,
                straddle_iv=straddle_iv,
                realized_vol_20d=realized_vol_20d,
                expiry_spot=expiry_spot,
                leg_premiums=leg_premiums,
                iv_source=iv_source,
                settlement_source=settlement_source,
            )
        )

    # ---------------------------------------------------------------------------
    # Aggregate metrics
    # ---------------------------------------------------------------------------
    n_cycles = len(cycles)
    n_trades = len(trades)

    if n_trades == 0:
        metrics: dict[str, Any] = {
            "trades": trades,
            "n_cycles": n_cycles,
            "n_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "net_pnl": 0.0,
            "return_on_capital": 0.0,
        }
        metrics["go_no_go"] = go_no_go(metrics, capital=capital)
        return metrics

    pnls = [t.net_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = len(wins) / n_trades
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    # Sharpe: per-trade return / std × √52 (weekly frequency → 52 periods/yr)
    mean_pnl = sum(pnls) / n_trades
    if n_trades > 1:
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (n_trades - 1)
        std_pnl = math.sqrt(variance)
    else:
        std_pnl = 0.0
    sharpe = (mean_pnl / std_pnl * math.sqrt(52)) if std_pnl > 1e-9 else 0.0

    # Max drawdown on cumulative net P&L — stored as a NEGATIVE value
    cum_pnl = 0.0
    peak = 0.0
    worst_dd = 0.0  # most negative (largest absolute drawdown found so far)
    for p in pnls:
        cum_pnl += p
        if cum_pnl > peak:
            peak = cum_pnl
        dd = cum_pnl - peak  # negative when below peak
        if dd < worst_dd:
            worst_dd = dd
    max_drawdown = worst_dd  # negative number (or 0.0 if no drawdown)

    net_pnl = sum(pnls)
    return_on_capital = net_pnl / capital

    # Provenance counts for the honesty disclaimers (fixes #3 + #4): how many
    # traded cycles used REAL ATM IV vs the VIX proxy, and OFFICIAL FSP vs a
    # close/VWAP proxy. Surfaced in go_no_go so a GO can flag residual bias.
    n_real_iv = sum(1 for t in trades if t.iv_source == IV_SOURCE_REAL)
    n_official_fsp = sum(1 for t in trades if t.settlement_source == FSP_SOURCE_OFFICIAL)

    metrics = {
        "trades": trades,
        "n_cycles": n_cycles,
        "n_trades": n_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "net_pnl": net_pnl,
        "return_on_capital": return_on_capital,
        "n_real_iv": n_real_iv,
        "n_vix_proxy_iv": n_trades - n_real_iv,
        "n_official_fsp": n_official_fsp,
        "n_proxy_settlement": n_trades - n_official_fsp,
    }
    metrics["go_no_go"] = go_no_go(metrics, capital=capital)
    return metrics


# ---------------------------------------------------------------------------
# Go / no-go gate
# ---------------------------------------------------------------------------

def go_no_go(metrics: dict[str, Any], capital: float = 200_000.0) -> tuple[bool, str]:
    """Evaluate Phase-2 promotion criteria from aggregated backtest metrics.

    Criteria (from handoff §7):
    * Positive expectancy after costs: net_pnl > 0 **and** profit_factor > 1.
    * Max drawdown < 15 % of allocated capital.

    Parameters
    ----------
    metrics: Output dict from :func:`run_backtest` (or compatible dict).
    capital: Allocated capital in ₹.  Default ₹2,00,000.

    Returns
    -------
    (go: bool, reason: str)
        ``go`` is True only when ALL criteria pass.
        ``reason`` cites the specific numbers for the PR report.
    """
    net_pnl: float = metrics.get("net_pnl", 0.0)
    profit_factor: float = metrics.get("profit_factor", 0.0)
    max_drawdown: float = metrics.get("max_drawdown", float("-inf"))
    n_trades: int = metrics.get("n_trades", 0)
    win_rate: float = metrics.get("win_rate", 0.0)
    sharpe: float = metrics.get("sharpe", 0.0)
    dd_limit = 0.15 * capital

    failures: list[str] = []
    passes: list[str] = []

    # Criterion 0: statistical significance — need at least 30 trades
    if n_trades >= 30:
        passes.append(f"n_trades={n_trades} ≥ 30")
    else:
        failures.append(
            f"n_trades={n_trades} < 30 (insufficient sample size for statistical significance)"
        )

    # Criterion 1: positive net P&L
    if net_pnl > 0:
        passes.append(f"net_pnl=₹{net_pnl:,.0f} > 0")
    else:
        failures.append(f"net_pnl=₹{net_pnl:,.0f} ≤ 0")

    # Criterion 2: profit factor > 1
    pf_str = f"{profit_factor:.2f}" if profit_factor != float("inf") else "∞"
    if profit_factor > 1.0:
        passes.append(f"profit_factor={pf_str} > 1")
    else:
        failures.append(f"profit_factor={pf_str} ≤ 1")

    # Criterion 3: positive Sharpe ratio
    if sharpe > 0:
        passes.append(f"sharpe={sharpe:.2f} > 0")
    else:
        failures.append(f"sharpe={sharpe:.2f} ≤ 0 (negative Sharpe)")

    # Criterion 4: max drawdown < 15% of capital
    # max_drawdown is stored as a NEGATIVE number; use abs() to compare magnitude.
    abs_dd = abs(max_drawdown)
    if abs_dd < dd_limit:
        passes.append(
            f"max_drawdown=₹{max_drawdown:,.0f} (abs ₹{abs_dd:,.0f}) < 15%×capital=₹{dd_limit:,.0f}"
        )
    else:
        failures.append(
            f"max_drawdown=₹{max_drawdown:,.0f} (abs ₹{abs_dd:,.0f}) ≥ 15%×capital=₹{dd_limit:,.0f}"
        )

    # Summary stats (informational, not criteria)
    info = (
        f"n_trades={n_trades}, win_rate={win_rate:.1%}, "
        f"sharpe={sharpe:.2f}, return_on_capital={metrics.get('return_on_capital', 0):.1%}"
    )

    # ── Fidelity provenance disclaimer (fixes #3 + #4) ──────────────────────
    # Does NOT change the GO/NO-GO verdict — it flags the residual proxy bias so
    # a GO is read honestly. Direction: VIX-proxy IV is OPTIMISTIC in calm
    # regimes; close-proxy settlement (vs NSE FSP) is AMBIGUOUS near-the-money.
    n_real_iv = metrics.get("n_real_iv")
    n_official_fsp = metrics.get("n_official_fsp")
    fidelity_notes: list[str] = []
    if n_real_iv is not None and n_trades > 0:
        n_proxy_iv = metrics.get("n_vix_proxy_iv", n_trades - n_real_iv)
        if n_proxy_iv > 0:
            fidelity_notes.append(
                f"IV: {n_real_iv}/{n_trades} real ATM, {n_proxy_iv} on VIX proxy "
                "(proxy OPTIMISTIC in calm/contango — edge may be lower)"
            )
        else:
            fidelity_notes.append(f"IV: all {n_trades} on REAL ATM IV")
    if n_official_fsp is not None and n_trades > 0:
        n_proxy_settle = metrics.get("n_proxy_settlement", n_trades - n_official_fsp)
        if n_proxy_settle > 0:
            fidelity_notes.append(
                f"settle: {n_official_fsp}/{n_trades} NSE FSP, {n_proxy_settle} on "
                "close/VWAP proxy (not FSP — near-the-money weeks may flip win/loss)"
            )
        else:
            fidelity_notes.append(f"settle: all {n_trades} on NSE FSP")
    fidelity = (" FIDELITY: " + "; ".join(fidelity_notes) + ".") if fidelity_notes else ""

    go = len(failures) == 0
    if go:
        reason = f"GO — all criteria pass. {'; '.join(passes)}. {info}{fidelity}"
    else:
        reason = (
            f"NO-GO — {len(failures)} criterion/criteria failed: "
            f"{'; '.join(failures)}. Passed: {'; '.join(passes) or 'none'}. {info}{fidelity}"
        )

    return go, reason


# ---------------------------------------------------------------------------
# DB loader (hydrates `cycles` for run_backtest from live tables)
# ---------------------------------------------------------------------------


def cycles_from_db(
    symbol: str | None = None,
    *,
    index: IndexConfig = NIFTY,
    index_id: str | None = None,
    nifty_id: str | None = None,
    vix_id: str | None = None,
    timeframe: str = "1d",
    mode: str = "weekly",
) -> list[dict]:
    """Assemble weekly iron-condor cycles from the DB tables created in migrations 009/010.

    Index-agnostic: pass an ``IndexConfig`` (default ``NIFTY``) to drive the
    underlying's security ids, symbol, and expiry-weekday rule. The legacy
    ``index_id`` / ``vix_id`` / ``symbol`` keyword args still override the config
    fields for callers that pass them explicitly.

    Parameters
    ----------
    index:      Underlying contract spec (default ``NIFTY``). Supplies ``symbol``,
                ``security_id``, ``vix_security_id`` and the expiry-weekday rule.
    symbol:     Ticker key in expiry_calendar. Defaults to ``index.symbol``.
                Only used in ``mode="expiry_calendar"``.
    index_id:   ``security_id`` of the index row in index_bars. Defaults to
                ``index.security_id``.
    nifty_id:   Deprecated alias for ``index_id`` (back-compat for existing
                callers); ``index_id`` takes precedence when both are given.
    vix_id:     ``security_id`` of the volatility index row in index_bars.
                Defaults to ``index.vix_security_id``.
    timeframe:  Bar timeframe stored in index_bars (default ``"1d"``).
    mode:       Cycle-boundary strategy:

                ``"weekly"`` *(default — historical backtest path)*
                    Derives synthetic ISO-week boundaries directly from the
                    NIFTY trading calendar stored in ``index_bars``.  The last
                    trading day of each ISO week becomes a boundary; consecutive
                    boundary pairs form one cycle.  This is the correct path for
                    historical backtesting because Dhan's expiry endpoint is
                    **forward-only** — ``expiry_calendar`` only holds *future*
                    expiry dates and cannot drive a historical backtest.

                ``"expiry_calendar"`` *(forward / live path)*
                    Uses ``expiry_calendar`` weekly (or all) expiry dates.
                    Suitable once historical expiries are recorded in the table,
                    or for forward-looking paper-trading simulation.

    Returns
    -------
    list of cycle dicts ordered by entry_date ascending, each with keys:

    .. code-block:: python

        {
            "entry_date":       date,   # boundary day (entry)
            "expiry_date":      date,   # next boundary day (settlement proxy)
            "spot":             float,  # NIFTY close at entry_date
            "realized_vol_20d": float,  # NIFTY 20-day realised vol at entry_date
            "straddle_iv":      float,  # India VIX close at entry_date / 100
            "dte":              int,    # (expiry_date - entry_date).days
            "expiry_spot":      float,  # NIFTY close at expiry_date
        }

    Raises
    ------
    ValueError
        If ``mode`` is not one of ``"weekly"`` or ``"expiry_calendar"``.

    .. rubric:: Fidelity caveats (daily-step approximation)

    * **Entry price:** uses the CLOSE of the boundary day, not the next
      morning's open.  Real entry would be 09:30–10:00 IST — close-to-open
      gap can be 50–200 pts on volatile weeks.
    * **Settlement price:** uses the NIFTY index daily CLOSE, not the NSE
      official Final Settlement Price (FSP), which is the 30-minute weighted
      average of NIFTY futures from 15:00–15:30 IST.
    * **straddle_iv proxy:** ``straddle_iv = India VIX close / 100``.  VIX is
      a 30-day implied-vol index.  The true weekly ATM straddle IV (≈ 7-DTE
      term) typically trades *above* VIX due to the term structure of variance.
    * **Realised vs implied horizon mismatch:** ``realized_vol_20d`` is a
      20-calendar-day backward-looking measure; VIX is a 30-day forward measure.

    Net direction of bias: most caveats are **conservative** (understated credit,
    tighter shorts than market practice).  A GO verdict is preliminary-but-
    trustworthy; a NO-GO is solid.  A GO must be re-validated with NSE FSP data
    and real per-expiry ATM IV before any live consideration.
    """
    if mode not in ("weekly", "expiry_calendar"):
        raise ValueError(
            f"cycles_from_db: unknown mode={mode!r}; "
            "expected 'weekly' (historical) or 'expiry_calendar' (forward/live)."
        )

    # Resolve effective ids/symbol: explicit kwargs override the index config.
    # `index_id` wins over the deprecated `nifty_id` alias, which wins over config.
    eff_symbol = symbol if symbol is not None else index.symbol
    _id = index_id if index_id is not None else nifty_id
    eff_index_id = _id if _id is not None else index.security_id
    eff_vix_id = vix_id if vix_id is not None else index.vix_security_id

    # Lazy imports — keep pure pricing functions free of DB dependencies.
    from sqlalchemy import text  # noqa: PLC0415

    from db import get_session  # noqa: PLC0415

    tf = timeframe

    if mode == "weekly":
        return _cycles_from_db_weekly(eff_index_id, eff_vix_id, tf, get_session, text, index)
    else:
        return _cycles_from_db_expiry_calendar(
            eff_symbol, eff_index_id, eff_vix_id, tf, get_session, text
        )


def _build_bar_maps(
    session: Any,
    text: Any,
    index_id: str,
    vix_id: str | None,
    tf: str,
) -> tuple[dict, dict]:
    """Query index_bars and return (index_map, vix_map)."""
    index_rows = session.execute(
        text(
            "SELECT (time AT TIME ZONE 'UTC')::date AS d, close, realized_vol_20d "
            "FROM index_bars "
            "WHERE security_id = :nid AND timeframe = :tf "
            "ORDER BY 1"
        ),
        {"nid": index_id, "tf": tf},
    ).fetchall()

    index_map: dict[date, tuple[float, float | None]] = {}
    for r in index_rows:
        d, close, rvol = r[0], r[1], r[2]
        index_map[d] = (float(close), float(rvol) if rvol is not None else None)

    vix_rows = session.execute(
        text(
            "SELECT (time AT TIME ZONE 'UTC')::date AS d, close "
            "FROM index_bars "
            "WHERE security_id = :vid AND timeframe = :tf"
        ),
        {"vid": vix_id, "tf": tf},
    ).fetchall()

    vix_map: dict[date, float] = {r[0]: float(r[1]) for r in vix_rows}
    return index_map, vix_map


def _build_cycles_from_pairs(
    boundary_pairs: list[tuple[date, date]],
    index_map: dict,
    vix_map: dict,
    mode: str,
) -> list[dict]:
    """Convert (entry, expiry) date pairs into cycle dicts, skipping incomplete ones."""
    cycles: list[dict] = []
    for e_i, e_next in boundary_pairs:
        na = index_map.get(e_i)
        if na is None or na[1] is None:
            logger.debug(
                "cycles_from_db[%s]: skipping pair (%s, %s) — missing index close/rvol at %s",
                mode, e_i, e_next, e_i,
            )
            continue

        va = vix_map.get(e_i)
        if va is None:
            logger.debug(
                "cycles_from_db[%s]: skipping pair (%s, %s) — missing VIX close at %s",
                mode, e_i, e_next, e_i,
            )
            continue

        nb = index_map.get(e_next)
        if nb is None:
            logger.debug(
                "cycles_from_db[%s]: skipping pair (%s, %s) — missing index close at %s",
                mode, e_i, e_next, e_next,
            )
            continue

        spot, rvol = na
        cycles.append(
            {
                "entry_date": e_i,
                "expiry_date": e_next,
                "spot": spot,
                "realized_vol_20d": rvol,
                "straddle_iv": va / 100.0,
                "dte": (e_next - e_i).days,
                "expiry_spot": nb[0],
            }
        )
    return cycles


def _cycles_from_db_weekly(
    index_id: str,
    vix_id: str | None,
    tf: str,
    get_session: Any,
    text: Any,
    index: IndexConfig = NIFTY,
) -> list[dict]:
    """Historical-backtest path: synthetic ISO-week boundaries from index_bars.

    Derives each ISO week's expiry boundary from the underlying bar calendar
    (using ``index``'s expiry-weekday rule), then treats consecutive boundary
    pairs as (entry, expiry) windows. This avoids the forward-only limitation of
    ``expiry_calendar``. Validated live on NIFTY: 233 weekly cycles / 164 trades
    over the full bar history.
    """
    with get_session() as session:
        index_map, vix_map = _build_bar_maps(session, text, index_id, vix_id, tf)

    if len(index_map) < 2:
        logger.warning(
            "cycles_from_db[weekly]: fewer than 2 %s bar dates found — "
            "returning empty cycle list (check that index_bars has been populated "
            "for security_id=%s, timeframe=%s).",
            index.symbol, index_id, tf,
        )
        return []

    # Build ISO-week → list of trading days, then snap each week to its real
    # expiry weekday (per ``index``'s rule, holiday-rolled) via
    # snap_to_expiry_weekday — fidelity fix #1. This replaces the old "last
    # trading day of the ISO week" heuristic (which floats to Fri on a full week
    # and ignored the index's expiry-weekday cutover).
    wk_days: dict[tuple[int, int], list[date]] = {}
    for d in sorted(index_map):
        iso_year, iso_week, _ = d.isocalendar()
        wk_days.setdefault((iso_year, iso_week), []).append(d)

    bounds: list[date] = []
    for kk in sorted(wk_days):
        b = snap_to_expiry_weekday(wk_days[kk], index=index)
        if b is not None:
            bounds.append(b)

    if len(bounds) < 2:
        logger.warning(
            "cycles_from_db[weekly]: fewer than 2 ISO-week boundaries found — "
            "returning empty cycle list.",
        )
        return []

    pairs = list(zip(bounds[:-1], bounds[1:]))
    cycles = _build_cycles_from_pairs(pairs, index_map, vix_map, "weekly")

    if len(cycles) == 0:
        logger.warning(
            "cycles_from_db[weekly]: 0 cycles built from %d weekly boundaries — "
            "no complete %s + VIX data for any boundary pair "
            "(index_map size=%d, vix_map size=%d).",
            len(bounds), index.symbol, len(index_map), len(vix_map),
        )

    return cycles


def _cycles_from_db_expiry_calendar(
    symbol: str,
    index_id: str,
    vix_id: str | None,
    tf: str,
    get_session: Any,
    text: Any,
) -> list[dict]:
    """Forward/live path: cycle boundaries from expiry_calendar.

    Reads weekly (or all) expiry dates from ``expiry_calendar`` and pairs
    consecutive dates as (entry, expiry) windows.  Suitable once historical
    expiries are recorded in the table, or for forward-looking simulation.
    """
    with get_session() as session:
        # ── 1. Expiry dates ──────────────────────────────────────────────────
        rows = session.execute(
            text(
                "SELECT expiry_date FROM expiry_calendar "
                "WHERE symbol = :sym AND expiry_type = 'weekly' "
                "ORDER BY expiry_date ASC"
            ),
            {"sym": symbol},
        ).fetchall()

        if not rows:
            # Fallback: no weekly flag — use all expiries for this symbol.
            rows = session.execute(
                text(
                    "SELECT expiry_date FROM expiry_calendar "
                    "WHERE symbol = :sym "
                    "ORDER BY expiry_date ASC"
                ),
                {"sym": symbol},
            ).fetchall()

        expiry_dates: list[date] = [r[0] for r in rows]

        if len(expiry_dates) < 2:
            logger.warning(
                "cycles_from_db[expiry_calendar]: fewer than 2 expiry dates found "
                "for symbol=%s — returning empty cycle list.",
                symbol,
            )
            return []

        index_map, vix_map = _build_bar_maps(session, text, index_id, vix_id, tf)

    pairs = list(zip(expiry_dates[:-1], expiry_dates[1:]))
    cycles = _build_cycles_from_pairs(pairs, index_map, vix_map, "expiry_calendar")

    if len(cycles) == 0 and len(expiry_dates) >= 2:
        if not index_map and not vix_map:
            logger.warning(
                "cycles_from_db[expiry_calendar]: 0 cycles built from %d expiry dates "
                "for symbol=%s — no overlapping index or VIX bar data found in index_bars "
                "(check that Phase-0 ingestion has run for security_id=%s / %s, timeframe=%s).",
                len(expiry_dates), symbol, index_id, vix_id, tf,
            )
        else:
            logger.warning(
                "cycles_from_db[expiry_calendar]: 0 cycles built from %d expiry dates "
                "for symbol=%s — index_bars rows exist but none matched the expiry dates "
                "(index_map size=%d, vix_map size=%d); "
                "check date alignment between expiry_calendar and index_bars.",
                len(expiry_dates), symbol, len(index_map), len(vix_map),
            )

    return cycles
