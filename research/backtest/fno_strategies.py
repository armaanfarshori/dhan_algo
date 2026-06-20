"""
Multi-leg options backtest engine for NIFTY weekly cycles.

Generalises ``research/backtest/fno_condor.py`` into a strategy-agnostic N-leg
engine. The condor module is left intact; this module *reuses* its primitives
via import and never re-implements them.

This module is **pure + deterministic** (no DB, no network) except for the thin
``cycles_from_db`` re-export. All pricing / resolution / cost / metrics functions
are unit-testable without a database.

Honesty ledger (§11 of the engine spec)
-----------------------------------------
1. Black-76 with a **single IV for all strikes** ignores the volatility smile /
   skew. OTM puts trade richer; OTM calls cheaper. For premium SELLERS this
   understates the short-leg credit → conservative → safe for a GO verdict
   (regime-dependent — in high-skew/crisis regimes this may not hold; flag GO
   from high-VIX cycles).
   For DEBIT / long-vol strategies (long straddle, long vertical, long calendar)
   the same single-IV assumption is NOT conservative — it can overstate entry
   edge → a GO on a debit strategy is WEAKER than a GO on a credit strategy and
   must be flagged in each strategy's own analysis.

2. Settlement uses the NIFTY index **daily CLOSE**, not the NSE Final Settlement
   Price (FSP), which is the 30-minute VWAP of NIFTY futures 15:00–15:30 IST. A
   GO verdict is preliminary and must be re-validated with FSP before any live
   consideration.

3. ``straddle_iv`` is historically ``India VIX / 100`` — a 30-day implied-vol
   proxy used for ~5–10-DTE weekly options → term-structure bias (weekly IV
   typically trades above the 30-day VIX due to short-end vol-of-vol premium).

4. ``span_pct`` for naked-short positions is a flat ~12 % approximation of the
   NIFTY SPAN+exposure margin. True SPAN scales with VIX — so in high-vol
   regimes the denominator is understated → return_on_margin is slightly
   **optimistic** (the one non-conservative number in this engine). A true SPAN
   file (NSE SPAN parameters, published daily) would refine it. Flag any GO on
   naked-short strategies that arises in high-VIX environments.

5. Multi-expiry strategies (calendar spread, diagonal) are historically
   infeasible — ``cycles_from_db`` carries ONE IV per cycle (VIX/100); we have
   no historical per-expiry term structure → ``run_strategy_backtest`` refuses
   any ``needs_multi_expiry=True`` spec in historical mode (returns a NO-GO, does
   NOT raise). Forward-paper via the real chain is feasible.

6. ``return_on_margin`` is the headline metric and is reported alongside the
   legacy ``return_on_capital``, but it is NOT yet a ``go_no_go`` criterion
   (informational for now; a future revision should add an ROM floor).
"""

from __future__ import annotations

import argparse
import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Literal, Optional

# ---------------------------------------------------------------------------
# Reused primitives — import, do NOT re-implement
# ---------------------------------------------------------------------------
from core.fno_derived import implied_move as _implied_move
from ml.fno_vol_gate import BUY_PREMIUM, DEFAULT_K, SELL_PREMIUM, gate_decision
from research.backtest.fno_condor import (
    _round_to_step,
    black76_call,
    black76_put,
    build_condor,
    cycles_from_db,  # re-exported unchanged
    go_no_go,
)
from research.backtest.fno_costs import (
    NIFTY_LOT,
    OPTION_EXERCISE_STT_PCT,
    condor_costs,
    slippage,
)

# Optional readability alias — condor_costs is already a generic N-leg function
leg_costs = condor_costs

logger = logging.getLogger("dhan.backtest.fno_strategies")

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
OptionType = Literal["CE", "PE"]
Side = Literal["BUY", "SELL"]
StrategyBuilder = Callable[..., "list[Leg]"]


# ---------------------------------------------------------------------------
# 1. Core data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Leg:
    """One option leg of a multi-leg strategy.

    ``strike`` is in index points (already snapped to the strike grid by the
    builder). ``qty_lots`` is a positive integer count of lots (always >= 1);
    BUY/SELL carries the direction — qty_lots is NEVER signed.
    Units = qty_lots * lot_size.
    """

    option_type: OptionType  # "CE" | "PE"
    side: Side               # "BUY" | "SELL"
    strike: float
    qty_lots: int = 1

    def signed(self) -> int:
        """+1 for SELL (premium received), -1 for BUY (premium paid)."""
        return +1 if self.side == "SELL" else -1


@dataclass(frozen=True)
class StrategySpec:
    """Metadata + builder for one strategy family.

    ``build`` must have the exact signature::

        def builder(spot, atm_strike, step, dte, sigma, params) -> list[Leg]

    It only decides strikes/sides/types. It never prices, never reads the DB.

    ``span_model`` controls the SPAN-margin approximation:
      "defined"          — max theoretical loss (credit or debit spreads).
      "naked_short"      — span_pct × notional per short lot.
      "spread_naked_mix" — defined part + naked part combined.
    """

    name: str
    build: StrategyBuilder
    defined_risk: bool       # True → max loss is bounded (spreads)
    sell_premium: bool       # True → strategy harvests VRP (gate must say SELL_PREMIUM)
    needs_multi_expiry: bool = False  # True → historically infeasible
    default_params: dict[str, Any] = field(default_factory=dict)
    span_model: str = "defined"  # "defined" | "naked_short" | "spread_naked_mix"


@dataclass
class StrategyTrade:
    """Outcome record for a single cycle of any multi-leg strategy."""

    entry_date: date
    expiry_date: date
    legs: list[Leg]
    fills: list[float]        # per-unit fill price (after slippage), one per leg
    entry_net: float           # net credit (+) or debit (−) per unit across all legs
    gross_pnl: float           # ₹ before costs
    costs: float               # ₹ total statutory + brokerage
    net_pnl: float             # gross_pnl − costs
    span: float                # ₹ blocked margin (SPAN approximation)
    win: bool                  # net_pnl > 0

    # Diagnostics
    gate_decision_label: str = ""
    straddle_iv: float = 0.0
    realized_vol_20d: float = 0.0
    expiry_spot: float = 0.0
    strategy: str = ""

    # Jade lizard: upside risk = max(0, call_spread_width − entry_net_per_unit).
    # Non-zero when the jade-lizard invariant is violated (credit < spread width).
    # Always 0.0 for non-jade trades.
    upside_risk: float = 0.0


# ---------------------------------------------------------------------------
# 2. Pricing helpers
# ---------------------------------------------------------------------------


def price_leg_hist(leg: Leg, spot: float, sigma: float, dte: int) -> float:
    """Black-76 premium per UNIT for one leg. F ≈ spot. T = dte / 365.

    sigma source = cycle's straddle_iv (VIX/100 historically — a 30-day
    implied proxy for a ~5–10-DTE weekly; see honesty ledger §3).
    """
    T = dte / 365.0
    if leg.option_type == "CE":
        return black76_call(spot, leg.strike, T, sigma)
    return black76_put(spot, leg.strike, T, sigma)


def fill_price(leg: Leg, mid: float, slip_pct: float = 0.005) -> float:
    """Adverse fill: SELL receives less, BUY pays more."""
    s = slippage(mid, slip_pct)
    return mid - s if leg.side == "SELL" else mid + s


def net_premium_per_unit(legs: list[Leg], fills: list[float]) -> float:
    """Σ leg.signed() * fill_price * qty_lots — positive = net CREDIT received, negative = net DEBIT paid.

    qty_lots is included so that ratio spreads (BUY 1 / SELL 2) are weighted
    correctly.  For all single-qty strategies this is identity with the old
    formulation (qty_lots=1 everywhere).
    """
    return sum(leg.signed() * f * leg.qty_lots for leg, f in zip(legs, fills))


# ---------------------------------------------------------------------------
# 2b. Forward / paper mode — real-chain pricing
# ---------------------------------------------------------------------------


def price_leg_chain(
    leg: Leg,
    ce: dict[int, float],
    pe: dict[int, float],
) -> tuple[int, float]:
    """Return (actual_strike_used, ltp_per_unit) from the real option chain.

    ``ce`` / ``pe`` map ``int(strike)`` → ltp. Nearest-strike fallback when the
    exact strike has no quote (mirrors ``core.fno_paper._nearest_strike``).

    Caller MUST bail the whole cycle if any leg ltp is None or <= 0 (incomplete
    chain), exactly like ``core.fno_paper.record_paper_entry``.
    """
    chain = ce if leg.option_type == "CE" else pe
    if not chain:
        return int(leg.strike), 0.0
    target = int(leg.strike)
    if target in chain:
        actual = target
    else:
        actual = min(chain.keys(), key=lambda k: abs(k - target))
    return actual, chain[actual]


# ---------------------------------------------------------------------------
# 3. Resolution at expiry (intrinsic, index-close proxy)
# ---------------------------------------------------------------------------


def leg_intrinsic(leg: Leg, S: float) -> float:
    """Per-unit intrinsic at expiry (buyer's value, always >= 0)."""
    if leg.option_type == "CE":
        return max(S - leg.strike, 0.0)
    return max(leg.strike - S, 0.0)


def resolve_legs(
    legs: list[Leg],
    entry_net_per_unit: float,
    expiry_spot: float,
    lot: int = NIFTY_LOT,
) -> dict[str, Any]:
    """Gross P&L at expiry for the whole position (all lots), leg by leg.

    For each leg the writer's (our) P&L from holding to expiry is:
      SELL leg:  +entry_fill − intrinsic  (kept premium minus payout)
      BUY  leg:  −entry_fill + intrinsic  (paid premium recovered as intrinsic)

    Because entry_net_per_unit = Σ signed*fill_per_unit (already collapsed), we
    must track per-leg fills separately to support unequal qty_lots.  This
    function computes gross P&L leg-by-leg:

      net_pnl_per_position =
          Σ_legs  leg.signed() * (entry_fill_leg − leg_intrinsic(leg, S)) * leg.qty_lots * lot

    **This collapses to resolve_condor when all four legs have qty_lots=1 and
    sides follow the iron-condor pattern.**

    Caveat: settlement uses the NIFTY index daily CLOSE, not the NSE FSP
    (30-min VWAP of NIFTY futures 15:00–15:30 IST). A GO verdict is preliminary
    and must be re-validated with FSP before any live consideration.

    Parameters
    ----------
    legs:
        List of Leg objects for the strategy.
    entry_net_per_unit:
        Net premium per unit (Σ signed * fill). Used as the aggregate credit or
        debit baseline; per-leg fills are inferred from the entry_fills kwarg
        when provided, otherwise the function works from net for symmetric legs.

    Note
    ----
    This overload (entry_net_per_unit only, no per-leg fills) works correctly
    for symmetric single-qty strategies: we distribute the net contribution of
    each leg by its proportional signed fill implied by the net. For unequal-qty
    positions you must call resolve_legs_with_fills which accepts per-leg fills.
    Returns dict with key "gross_pnl".
    """
    assert all(l.qty_lots == 1 for l in legs), (
        "use resolve_legs_with_fills for unequal qty_lots — "
        "resolve_legs assumes all legs have qty_lots=1"
    )
    S = expiry_spot
    # Gross P&L using the aggregate: entry_net_per_unit is Σ_legs signed*fill_leg
    # and intrinsic PnL is − Σ_legs signed*intrinsic_leg * qty_lots
    # For the aggregate formula to work with unequal qty_lots we need:
    #   gross = entry_net_per_unit * (some lot) − Σ signed*intrinsic*qty_lots*lot
    # But entry_net_per_unit has already mixed together legs with different qty_lots.
    # To be precise, this function should not combine entry_net across different qty_lots
    # without knowing per-leg fills. The spec's §3 formula:
    #   net_pnl_per_position = Σ_legs leg.signed() * (entry_fill_leg − intrinsic) * qty_lots * lot
    # requires per-leg fills. We therefore require the caller to pass them in;
    # but for the "simple" two-arg API used in the condor equivalence test, we need
    # a convention. We adopt: for the simple resolve_legs(legs, entry_net, spot, lot),
    # we use entry_net as the baseline for a 1-lot-per-leg symmetric case.
    # For unequal qty, callers must use resolve_legs_with_fills.
    #
    # For backward compatibility / condor equivalence: compute net per unit then multiply
    # by the single lot (all qty_lots=1 implied by this API).
    gross_per_unit = entry_net_per_unit - sum(
        leg.signed() * leg_intrinsic(leg, S) for leg in legs
    )
    gross_pnl = gross_per_unit * lot
    return {"gross_pnl": gross_pnl}


def resolve_legs_with_fills(
    legs: list[Leg],
    fills: list[float],
    expiry_spot: float,
    lot: int = NIFTY_LOT,
) -> dict[str, Any]:
    """Gross P&L at expiry, leg-by-leg with per-leg fills and unequal qty_lots.

    Formula (spec §3):
        net_pnl_per_position =
            Σ_legs  leg.signed() * (fill_leg − leg_intrinsic(leg, S)) * leg.qty_lots * lot

    This correctly handles ratio spreads and unequal quantity legs.

    Caveat: settlement uses NIFTY index daily CLOSE, not NSE FSP.
    """
    S = expiry_spot
    gross_pnl = sum(
        leg.signed() * (fill - leg_intrinsic(leg, S)) * leg.qty_lots * lot
        for leg, fill in zip(legs, fills)
    )
    return {"gross_pnl": gross_pnl}


# ---------------------------------------------------------------------------
# 4. Cost integration (every leg pays — never bypass condor_costs)
# ---------------------------------------------------------------------------


def _compute_costs(
    legs: list[Leg],
    fills: list[float],
    expiry_spot: float,
    lot: int = NIFTY_LOT,
) -> float:
    """Entry costs + exit exercise-STT for a hold-to-expiry strategy.

    Entry: all legs pay full statutory stack via condor_costs.
    Exit: no brokerage (cash settlement); exercise STT on ITM LONG legs.
    """
    S = expiry_spot
    # Entry cost legs: (fill_price_per_unit, qty_units, side)
    cost_legs = [(f, leg.qty_lots * lot, leg.side) for leg, f in zip(legs, fills)]
    entry_costs = condor_costs(cost_legs).total

    # Exercise STT on ITM long legs only (short legs: worthless expiry → no STT on exit)
    exercise_intrinsic = sum(
        leg_intrinsic(leg, S) * leg.qty_lots * lot
        for leg in legs
        if leg.side == "BUY" and leg_intrinsic(leg, S) > 0
    )
    exit_costs = OPTION_EXERCISE_STT_PCT * exercise_intrinsic

    return entry_costs + exit_costs


# ---------------------------------------------------------------------------
# 5. SPAN-margin approximation
# ---------------------------------------------------------------------------


def span_margin(
    spec: StrategySpec,
    legs: list[Leg],
    entry_net_per_unit: float,
    spot: float,
    lot: int = NIFTY_LOT,
    params: Optional[dict[str, Any]] = None,
) -> float:
    """Approximate the broker-blocked SPAN+exposure margin (₹) for the whole position.

    Model selection via ``spec.span_model``:

    "defined" (spreads: iron condor, vertical/credit spread, iron fly, butterfly)
        Broker blocks the **max theoretical loss** of the spread:
          wing_width_pts = max over (long-k, short-k) same-type pairs |long_k − short_k|
          max_loss_per_unit = max(0, wing_width_pts − entry_net_per_unit)  [credit case]
        For a DEBIT spread: max_loss = the net debit paid.
        Conservative (slightly overstates true SPAN) → understates ROM → safe for GO.

    "naked_short" (short straddle, short strangle, naked short)
        No max loss. Approximates SPAN+exposure as:
          span = span_pct × spot × lot × max_qty_lots_among_SELL_legs
        span_pct defaults to 0.12 (~12 % of notional per lot) — empirically the
        NIFTY SPAN+exposure range is 10–15 %; 0.12 is a defensible mid.
        Convention: for a straddle (CE short + PE short), SEBI combines the margin
        into ONE block (not double). We use ``max qty_lots among SELL legs`` to
        approximate this netting. **This is the ONE non-conservative denominator**:
        in high-VIX regimes the true SPAN can exceed 12 % → ROM is slightly
        optimistic. A true SPAN file (NSE daily SPAN parameters) would refine it.

    "spread_naked_mix" (ratio spreads with a naked tail)
        span = defined_part_max_loss + naked_part(span_pct × notional).

    Parameters
    ----------
    entry_net_per_unit:
        Net credit (>0) or net debit (<0) per unit from fills (Σ signed*fill).
    """
    p = params or {}
    model = spec.span_model

    if model == "defined":
        return _span_defined(legs, entry_net_per_unit, lot)

    if model == "naked_short":
        span_pct = p.get("span_pct", spec.default_params.get("span_pct", 0.12))
        return _span_naked_short(legs, spot, lot, span_pct)

    if model == "spread_naked_mix":
        span_pct = p.get("span_pct", spec.default_params.get("span_pct", 0.12))
        defined_part = _span_defined(legs, entry_net_per_unit, lot)
        naked_part = _span_naked_short(legs, spot, lot, span_pct)
        return defined_part + naked_part

    raise ValueError(f"Unknown span_model={spec.span_model!r}")


def _span_defined(
    legs: list[Leg],
    entry_net_per_unit: float,
    lot: int,
) -> float:
    """Max-loss SPAN for defined-risk spreads."""
    # Collect (strike, option_type, side) to find widest same-type wing pair
    ce_sell = [leg.strike for leg in legs if leg.option_type == "CE" and leg.side == "SELL"]
    ce_buy = [leg.strike for leg in legs if leg.option_type == "CE" and leg.side == "BUY"]
    pe_sell = [leg.strike for leg in legs if leg.option_type == "PE" and leg.side == "SELL"]
    pe_buy = [leg.strike for leg in legs if leg.option_type == "PE" and leg.side == "BUY"]

    widths: list[float] = []
    # Call spread width: |long_call_k − short_call_k|
    if ce_sell and ce_buy:
        widths.append(abs(max(ce_buy) - min(ce_sell)))
    # Put spread width: |short_put_k − long_put_k|
    if pe_sell and pe_buy:
        widths.append(abs(max(pe_sell) - min(pe_buy)))

    if widths:
        wing_width = max(widths)
        # Credit strategy: max loss = wing_width − credit (floored at 0)
        # Debit strategy: entry_net_per_unit < 0; max loss = abs(debit)
        if entry_net_per_unit >= 0:
            max_loss_per_unit = max(0.0, wing_width - entry_net_per_unit)
        else:
            # Debit paid = -entry_net_per_unit; that IS the max loss for a defined-debit strategy
            max_loss_per_unit = abs(entry_net_per_unit)
        return max_loss_per_unit * lot
    else:
        # No identifiable spread pair (e.g. long straddle: two BUY legs)
        # Max loss for a pure debit = net debit paid
        return abs(entry_net_per_unit) * lot


def _span_naked_short(
    legs: list[Leg],
    spot: float,
    lot: int,
    span_pct: float,
) -> float:
    """SPAN+exposure approximation for naked/undefined-risk short positions.

    Naked lot count = NET uncovered shorts per option_type
        = max(0, Σ SELL qty_lots − Σ BUY qty_lots) for that type, summed.

    For pure naked (straddle/strangle: only SELL legs of each type) this equals
    total shorts per type — unchanged from the prior behaviour.

    For ratio spreads (BUY 1 / SELL 2 of the same type) the long covers one
    short leg, leaving only 1 uncovered lot — not 2 (which the old max() would
    have returned when the SELL leg carries qty_lots=2).

    Convention for straddle / strangle: NIFTY SPAN combines the two short legs
    into one margin block.  Summing the net uncovered qty across both CE and PE
    naturally achieves this: net_CE=1, net_PE=1, SPAN blocks on max(1,1)=1
    lot-equivalent, which is what the combined-block approximation intends.
    We sum the per-type nets (not max), which slightly overstates for straddles
    but remains conservative and consistent.
    """
    from collections import defaultdict
    sell_by_type: dict[str, int] = defaultdict(int)
    buy_by_type: dict[str, int] = defaultdict(int)
    for leg in legs:
        if leg.side == "SELL":
            sell_by_type[leg.option_type] += leg.qty_lots
        else:
            buy_by_type[leg.option_type] += leg.qty_lots
    # Net uncovered shorts per option_type (floored at 0)
    naked_lots = sum(
        max(0, sell_by_type[ot] - buy_by_type[ot])
        for ot in set(sell_by_type)
    )
    if naked_lots == 0:
        return 0.0
    return span_pct * spot * lot * naked_lots


# ---------------------------------------------------------------------------
# 6. Builder functions (concrete, deterministic)
# ---------------------------------------------------------------------------


def build_short_straddle(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Short straddle: SELL ATM CE + SELL ATM PE."""
    return [
        Leg("CE", "SELL", float(atm_strike), 1),
        Leg("PE", "SELL", float(atm_strike), 1),
    ]


def build_short_strangle(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Short strangle: SELL CE above ATM, SELL PE below ATM (at ± move_mult × implied_move)."""
    move_mult = params.get("move_mult", 1.0)
    M = _implied_move(spot, sigma, dte) or 0.0
    short_call_k = float(_round_to_step(spot + move_mult * M, step))
    short_put_k = float(_round_to_step(spot - move_mult * M, step))
    return [
        Leg("CE", "SELL", short_call_k, 1),
        Leg("PE", "SELL", short_put_k, 1),
    ]


def build_iron_condor(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Iron condor: reuses build_condor internally, converts to Leg list.

    Order: SELL PE short_put_k, BUY PE long_put_k, SELL CE short_call_k, BUY CE long_call_k.
    This matches the condor resolution order used in resolve_condor for equivalence testing.
    """
    move_mult = params.get("move_mult", 1.5)
    wing_strikes = params.get("wing_strikes", 2)
    M = _implied_move(spot, sigma, dte) or 0.0
    strikes = build_condor(spot, M, move_mult=move_mult, wing_strikes=wing_strikes, step=step)
    return [
        Leg("PE", "SELL", float(strikes["short_put_k"]), 1),
        Leg("PE", "BUY",  float(strikes["long_put_k"]),  1),
        Leg("CE", "SELL", float(strikes["short_call_k"]), 1),
        Leg("CE", "BUY",  float(strikes["long_call_k"]),  1),
    ]


def build_iron_fly(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Iron fly: short ATM straddle + long OTM wings at atm ± wing_strikes × step."""
    wing_strikes = params.get("wing_strikes", 4)
    long_call_k = float(atm_strike + wing_strikes * step)
    long_put_k = float(atm_strike - wing_strikes * step)
    return [
        Leg("CE", "SELL", float(atm_strike), 1),
        Leg("PE", "SELL", float(atm_strike), 1),
        Leg("CE", "BUY",  long_call_k, 1),
        Leg("PE", "BUY",  long_put_k,  1),
    ]


def build_credit_put_spread(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Credit put spread: SELL PE at ATM − implied_move, BUY PE width_strikes lower."""
    short_mult = params.get("short_mult", 1.0)
    width_strikes = params.get("width_strikes", 2)
    M = _implied_move(spot, sigma, dte) or 0.0
    short_put_k = float(_round_to_step(spot - short_mult * M, step))
    long_put_k = short_put_k - width_strikes * step
    return [
        Leg("PE", "SELL", short_put_k, 1),
        Leg("PE", "BUY",  long_put_k,  1),
    ]


def build_long_straddle(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Long straddle: BUY ATM CE + BUY ATM PE.

    WARNING: under single-IV Black-76 this is NOT conservative (see module
    docstring §1). A GO on a long straddle is weaker than on a credit strategy.
    """
    return [
        Leg("CE", "BUY", float(atm_strike), 1),
        Leg("PE", "BUY", float(atm_strike), 1),
    ]


def build_calendar_spread(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Calendar spread placeholder — INFEASIBLE in historical mode.

    Requires two distinct expiries (near + far). ``cycles_from_db`` carries ONE
    IV per cycle → cannot price near-vs-far honestly. Blocked by
    ``run_strategy_backtest`` via the ``needs_multi_expiry`` guard.
    """
    return [
        Leg("CE", "SELL", float(atm_strike), 1),  # near-expiry (placeholder)
        Leg("CE", "BUY",  float(atm_strike), 1),  # far-expiry  (placeholder)
    ]


# ---------------------------------------------------------------------------
# 6b. New builders — asymmetric, directional, and ratio spreads
# ---------------------------------------------------------------------------


def _wing_points(wing_width: float, implied_move: float, in_move_units: bool, step: int) -> int:
    """Convert a wing-width parameter to an absolute distance in points on the grid.

    Parameters
    ----------
    wing_width:
        Wing width in points (when ``in_move_units=False``) or as a multiple of
        ``implied_move`` (when ``in_move_units=True``).
    implied_move:
        The implied move in index points (``spot · sigma · √(dte/365)``).
    in_move_units:
        When True, ``wing_width`` is treated as ``wing_width × implied_move``.
    step:
        Strike grid spacing (50 for NIFTY).

    Returns
    -------
    int
        Wing distance in points, rounded to the grid, minimum one step.
    """
    raw = wing_width * implied_move if in_move_units else wing_width
    return max(step, _round_to_step(raw, step))


def build_broken_wing_condor(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Asymmetric (broken-wing) iron condor with different put/call wing widths.

    Short strikes are placed symmetrically at ``spot ± move_mult · implied_move``.
    Wings are asymmetric — ``put_wing`` and ``call_wing`` may differ, skewing the
    risk profile.  The wider wing defines the structure's max loss.

    Strike placement (per defined_risk.md §3):
        short_put_k  = round(spot − move_mult · implied_move)
        short_call_k = round(spot + move_mult · implied_move)
        put_wing     = _wing_points(put_wing_width,  implied_move, wing_in_move_units, step)
        call_wing    = _wing_points(call_wing_width, implied_move, wing_in_move_units, step)
        long_put_k   = short_put_k  − put_wing
        long_call_k  = short_call_k + call_wing

    Params:
        move_mult (float, default 1.5):
            Short-strike offset from spot in implied-move units.
        put_wing_width (float | None, default None):
            Explicit put-side wing in points (or move units).  When None, derived
            from ``base_wing · skew``.
        call_wing_width (float | None, default None):
            Explicit call-side wing.  When None, derived from ``base_wing / skew``.
        base_wing (float, default 100.0):
            Base wing used with ``skew`` when explicit widths are None.
        skew (float, default 1.5):
            Multiplicative skew.  ``put_wing = base_wing · skew``,
            ``call_wing = base_wing / skew``.  ``skew > 1`` → wider put wing
            (downside-protected).  Ignored when explicit widths are supplied.
        wing_in_move_units (bool, default False):
            When True, wing widths are interpreted as multiples of implied_move.

    Max-loss (per-side, per-unit):
        put_side_loss  = put_wing  − credit_per_unit_adj
        call_side_loss = call_wing − credit_per_unit_adj
        max_loss       = max(put_side_loss, call_side_loss, 0) · NIFTY_LOT

    SPAN ≈ max_loss (the max-of-sides); ``span_model="defined"`` in the registry.

    Honesty caveat: the economic case for a wider put wing rests on put skew
    (OTM puts being richer than flat-σ Black-76 prices them).  Our single-σ proxy
    cannot see skew, so it understates the cost of the wide put wing and overstates
    the broken-wing's edge.  This is the least trustworthy strategy under Phase-0
    pricing; a per-strike IV source is required before any GO.
    """
    move_mult = params.get("move_mult", 1.5)
    wing_in_move_units: bool = bool(params.get("wing_in_move_units", False))

    M = _implied_move(spot, sigma, dte) or 0.0

    short_put_k  = float(_round_to_step(spot - move_mult * M, step))
    short_call_k = float(_round_to_step(spot + move_mult * M, step))

    # Resolve explicit widths; fall back to base_wing × skew
    put_wing_width_raw  = params.get("put_wing_width", None)
    call_wing_width_raw = params.get("call_wing_width", None)

    if put_wing_width_raw is None or call_wing_width_raw is None:
        base_wing = float(params.get("base_wing", 100.0))
        skew      = float(params.get("skew", 1.5))
        if put_wing_width_raw is None:
            put_wing_width_raw  = base_wing * skew
        if call_wing_width_raw is None:
            call_wing_width_raw = base_wing / skew

    put_wing  = _wing_points(float(put_wing_width_raw),  M, wing_in_move_units, step)
    call_wing = _wing_points(float(call_wing_width_raw), M, wing_in_move_units, step)

    long_put_k  = short_put_k  - put_wing
    long_call_k = short_call_k + call_wing

    return [
        Leg("PE", "SELL", short_put_k,  1),
        Leg("PE", "BUY",  long_put_k,   1),
        Leg("CE", "SELL", short_call_k, 1),
        Leg("CE", "BUY",  long_call_k,  1),
    ]


def build_jade_lizard(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Jade Lizard: short OTM put + short OTM call spread (short call + long call).

    Sized so **total credit ≥ call-spread width → no upside risk**.  Downside risk
    is undefined (the naked short put).  Partial defined risk.

    Legs (per premium_sell_undefined.md §5):
        K_sp = round(spot − put_mult  · implied_move)   # short put (naked, OTM)
        K_sc = round(spot + call_mult · implied_move)   # short call (OTM)
        K_lc = K_sc + wing_strikes · step               # long call (hedge)

    Legs list order: [SELL PE @ K_sp, SELL CE @ K_sc, BUY CE @ K_lc].

    Params:
        put_mult (float, default 1.0):
            Short put offset below spot in implied-move units.
        call_mult (float, default 0.5):
            Short call offset above spot in implied-move units.
        wing_strikes (int, default 2):
            Number of steps between short call and long call.
            ``call_spread_width = wing_strikes · step``.

    Jade-Lizard invariant: ``credit_per_unit ≥ call_spread_width`` → no upside risk.
    The builder DOES NOT enforce this; the backtest engine records
    ``upside_risk = max(0, call_spread_width − credit)`` per cycle.  The constraint
    must be verified during analysis — a violated invariant means the position DOES
    carry capped upside risk and results must say so.

    SPAN model: ``"spread_naked_mix"`` in the registry.  The short put is naked
    (full SPAN); the call spread is defined-risk (max-loss margin only).

    Honesty caveat: single-σ Black-76 ignores put skew, so the naked put's
    premium and breakeven are understated exactly where the undefined risk lives.
    """
    put_mult    = float(params.get("put_mult",    1.0))
    call_mult   = float(params.get("call_mult",   0.5))
    wing_strikes = int(params.get("wing_strikes", 2))

    M = _implied_move(spot, sigma, dte) or 0.0

    k_sp = float(_round_to_step(spot - put_mult  * M, step))
    k_sc = float(_round_to_step(spot + call_mult * M, step))
    k_lc = k_sc + wing_strikes * step

    return [
        Leg("PE", "SELL", k_sp, 1),
        Leg("CE", "SELL", k_sc, 1),
        Leg("CE", "BUY",  k_lc, 1),
    ]


def build_bull_put_spread(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Bull put spread (credit spread, bullish): SELL higher-strike PE + BUY lower-strike PE.

    Profit if spot stays **above** the short put at expiry.  Defined risk = the
    net debit to unwind (max loss = width_pts − credit).

    Strike placement (per verticals.md §3.1):
        near_strike (short PE) = round(spot − move_mult · implied_move)
        far_strike  (long  PE) = near_strike − width · step

    Params:
        move_mult (float, default 0.5):
            Short-put offset below spot in implied-move units.
        width (int, default 2):
            Number of grid steps between the two strikes.
            ``width_pts = width · step``.

    Note: the existing ``credit_put_spread`` registry key uses a slightly different
    parameterisation (``short_mult`` + ``width_strikes``).  This builder uses the
    ``VerticalParams`` convention (``move_mult`` + ``width``) and is registered
    separately under ``bull_put_spread``.
    """
    move_mult = float(params.get("move_mult", 0.5))
    width     = int(params.get("width", 2))

    M = _implied_move(spot, sigma, dte) or 0.0
    k_short = float(_round_to_step(spot - move_mult * M, step))
    k_long  = k_short - width * step

    return [
        Leg("PE", "SELL", k_short, 1),
        Leg("PE", "BUY",  k_long,  1),
    ]


def build_bear_call_spread(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Bear call spread (credit spread, bearish): SELL lower-strike CE + BUY higher-strike CE.

    Profit if spot stays **below** the short call at expiry.  Defined risk.

    Strike placement (per verticals.md §3.2):
        near_strike (short CE) = round(spot + move_mult · implied_move)
        far_strike  (long  CE) = near_strike + width · step

    Params:
        move_mult (float, default 0.5):
            Short-call offset above spot in implied-move units.
        width (int, default 2):
            Number of grid steps between the two strikes.
    """
    move_mult = float(params.get("move_mult", 0.5))
    width     = int(params.get("width", 2))

    M = _implied_move(spot, sigma, dte) or 0.0
    k_short = float(_round_to_step(spot + move_mult * M, step))
    k_long  = k_short + width * step

    return [
        Leg("CE", "SELL", k_short, 1),
        Leg("CE", "BUY",  k_long,  1),
    ]


def build_bull_call_spread(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Bull call spread (DEBIT spread, bullish): BUY lower-strike CE + SELL higher-strike CE.

    Net debit paid at entry.  Max profit = ``width_pts − debit`` (when spot ≥
    short call at expiry).  Max loss = the debit paid.

    Strike placement (per verticals.md §3.3):
        near_strike (long  CE) = round(spot + move_mult · implied_move)   # BUY the nearer call
        far_strike  (short CE) = near_strike + width · step               # SELL further OTM

    Params:
        move_mult (float, default 0.0):
            Long-call offset above spot in implied-move units.  Default 0 → near-ATM
            long leg, consistent with the debit-spread convention in the spec.
        width (int, default 2):
            Number of grid steps between the two strikes.

    Honesty caveat: under single-IV Black-76 the debit is NOT priced conservatively
    (no skew adjustment).  A GO on a debit spread is weaker evidence than on a
    credit spread.
    """
    move_mult = float(params.get("move_mult", 0.0))
    width     = int(params.get("width", 2))

    M = _implied_move(spot, sigma, dte) or 0.0
    k_long  = float(_round_to_step(spot + move_mult * M, step))
    k_short = k_long + width * step

    return [
        Leg("CE", "BUY",  k_long,  1),
        Leg("CE", "SELL", k_short, 1),
    ]


def build_bear_put_spread(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Bear put spread (DEBIT spread, bearish): BUY higher-strike PE + SELL lower-strike PE.

    Net debit paid at entry.  Max profit = ``width_pts − debit`` (when spot ≤
    short put at expiry).  Max loss = the debit paid.

    Strike placement (per verticals.md §3.4):
        near_strike (long  PE) = round(spot − move_mult · implied_move)   # BUY the nearer put
        far_strike  (short PE) = near_strike − width · step               # SELL further OTM

    Params:
        move_mult (float, default 0.0):
            Long-put offset below spot in implied-move units.  Default 0 → near-ATM.
        width (int, default 2):
            Number of grid steps between the two strikes.

    Honesty caveat: same single-IV non-conservative caveat as bull_call_spread.
    """
    move_mult = float(params.get("move_mult", 0.0))
    width     = int(params.get("width", 2))

    M = _implied_move(spot, sigma, dte) or 0.0
    k_long  = float(_round_to_step(spot - move_mult * M, step))
    k_short = k_long - width * step

    return [
        Leg("PE", "BUY",  k_long,  1),
        Leg("PE", "SELL", k_short, 1),
    ]


def build_ratio_spread(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """1×2 ratio spread: BUY 1 nearer-the-money option, SELL 2 further-OTM of same type/expiry.

    Variant:
        ``"call"`` — Buy 1 CE @ K1, sell 2 CE @ K2 (K2 > K1).  Net short upside.
        ``"put"``  — Buy 1 PE @ K1, sell 2 PE @ K2 (K2 < K1).  Net short downside.

    Net entry is typically a **credit** (2× OTM premium − 1× nearer premium > 0).
    The position has **partial undefined risk** because one of the two short legs is
    uncovered beyond K2 (call variant: net short 1 contract for S > K2).

    Strike placement (per ratio_calendar.md §2.3):
        em = implied_move(spot, sigma, dte)
        K1 = round(atm ± k1_mult · em)   # long, nearer (default: ATM → k1_mult=0)
        K2 = round(atm ± k2_mult · em)   # short, further OTM (k2_mult > k1_mult)
        ± sign: + for call variant, − for put variant.

    If rounding collapses K1 == K2 (tiny implied move / very low sigma), K2 is
    widened by one step so the structure is always a true 1×2 at two distinct strikes.

    Params:
        variant (str, default ``"call"``):
            ``"call"`` or ``"put"``.
        k1_mult (float, default 0.0):
            Long-leg offset from ATM in implied-move units.
        k2_mult (float, default 1.0):
            Short-leg offset from ATM in implied-move units.  Must place K2 further
            from ATM than K1 (enforced by the K2-widening guard).

    Legs list order: [BUY 1 lot @ K1, SELL 2 lots @ K2].

    SPAN: ``"spread_naked_mix"`` — the long covers one short leg (defined-risk part);
    the second short is uncovered (naked_short part).

    Payoff (call variant, per unit):
        payoff(S) = credit + max(S − K1, 0) − 2 · max(S − K2, 0)
        peak at S = K2:  credit + (K2 − K1).
        Upper breakeven:  2·K2 − K1 + credit.
        S > upper_BE → unbounded loss (net short 1 contract).

    A risk stop is required for live/paper use.  The backtest runner does NOT
    apply an intra-cycle stop; it resolves at expiry only (see run_strategy_backtest).
    This makes the ratio-spread backtest tail-blind — treat any GO as diagnostic only.

    Honesty caveat: one of the two short legs is naked → max loss is unbounded
    (call) or floor-at-zero-spot (put).  This builder is registered under
    ``span_model="spread_naked_mix"`` so the runner uses the composite margin proxy.
    A true SPAN file is needed before any capital deployment.
    """
    variant = params.get("variant", "call")
    k1_mult = float(params.get("k1_mult", 0.0))
    k2_mult = float(params.get("k2_mult", 1.0))

    M = _implied_move(spot, sigma, dte) or 0.0

    if variant == "put":
        k1 = float(_round_to_step(atm_strike - k1_mult * M, step))
        k2 = float(_round_to_step(atm_strike - k2_mult * M, step))
        # Enforce K2 < K1 (put variant); widen if collapsed
        if k2 >= k1:
            k2 = k1 - step
    else:
        # call variant (default)
        k1 = float(_round_to_step(atm_strike + k1_mult * M, step))
        k2 = float(_round_to_step(atm_strike + k2_mult * M, step))
        # Enforce K2 > K1 (call variant); widen if collapsed
        if k2 <= k1:
            k2 = k1 + step

    opt_type: OptionType = "PE" if variant == "put" else "CE"

    return [
        Leg(opt_type, "BUY",  k1, 1),  # long 1 lot
        Leg(opt_type, "SELL", k2, 2),  # short 2 lots (net uncovered)
    ]


# ---------------------------------------------------------------------------
# 7. Strategy registry
# ---------------------------------------------------------------------------

FNO_STRATEGIES: dict[str, StrategySpec] = {
    "short_straddle": StrategySpec(
        name="short_straddle",
        build=build_short_straddle,
        defined_risk=False,
        sell_premium=True,
        needs_multi_expiry=False,
        span_model="naked_short",
        default_params={"span_pct": 0.12},
    ),
    "short_strangle": StrategySpec(
        name="short_strangle",
        build=build_short_strangle,
        defined_risk=False,
        sell_premium=True,
        needs_multi_expiry=False,
        span_model="naked_short",
        default_params={"move_mult": 1.0, "span_pct": 0.12},
    ),
    "iron_condor": StrategySpec(
        name="iron_condor",
        build=build_iron_condor,
        defined_risk=True,
        sell_premium=True,
        needs_multi_expiry=False,
        span_model="defined",
        default_params={"move_mult": 1.5, "wing_strikes": 2},
    ),
    "iron_fly": StrategySpec(
        name="iron_fly",
        build=build_iron_fly,
        defined_risk=True,
        sell_premium=True,
        needs_multi_expiry=False,
        span_model="defined",
        default_params={"wing_strikes": 4},
    ),
    "credit_put_spread": StrategySpec(
        name="credit_put_spread",
        build=build_credit_put_spread,
        defined_risk=True,
        sell_premium=True,
        needs_multi_expiry=False,
        span_model="defined",
        default_params={"short_mult": 1.0, "width_strikes": 2},
    ),
    "long_straddle": StrategySpec(
        name="long_straddle",
        build=build_long_straddle,
        defined_risk=True,
        sell_premium=False,
        needs_multi_expiry=False,
        span_model="defined",
        default_params={},
    ),
    "calendar_spread": StrategySpec(
        name="calendar_spread",
        build=build_calendar_spread,
        defined_risk=True,
        sell_premium=True,
        needs_multi_expiry=True,
        span_model="defined",
        default_params={},
    ),
    # ── New builders (Phase-0 options-strategies expansion) ──────────────────
    "broken_wing_condor": StrategySpec(
        name="broken_wing_condor",
        build=build_broken_wing_condor,
        defined_risk=True,
        sell_premium=True,
        needs_multi_expiry=False,
        span_model="defined",
        default_params={
            "move_mult": 1.5,
            "base_wing": 100.0,
            "skew": 1.5,
            "wing_in_move_units": False,
        },
    ),
    "jade_lizard": StrategySpec(
        name="jade_lizard",
        build=build_jade_lizard,
        defined_risk=False,          # naked short put on the downside
        sell_premium=True,
        needs_multi_expiry=False,
        span_model="spread_naked_mix",
        default_params={
            "put_mult": 1.0,
            "call_mult": 0.5,
            "wing_strikes": 2,
            "span_pct": 0.12,
        },
    ),
    "bull_put_spread": StrategySpec(
        name="bull_put_spread",
        build=build_bull_put_spread,
        defined_risk=True,
        sell_premium=True,
        needs_multi_expiry=False,
        span_model="defined",
        default_params={"move_mult": 0.5, "width": 2},
    ),
    "bear_call_spread": StrategySpec(
        name="bear_call_spread",
        build=build_bear_call_spread,
        defined_risk=True,
        sell_premium=True,
        needs_multi_expiry=False,
        span_model="defined",
        default_params={"move_mult": 0.5, "width": 2},
    ),
    "bull_call_spread": StrategySpec(
        name="bull_call_spread",
        build=build_bull_call_spread,
        defined_risk=True,
        sell_premium=False,          # debit spread → buy-premium gate
        needs_multi_expiry=False,
        span_model="defined",
        default_params={"move_mult": 0.0, "width": 2},
    ),
    "bear_put_spread": StrategySpec(
        name="bear_put_spread",
        build=build_bear_put_spread,
        defined_risk=True,
        sell_premium=False,          # debit spread → buy-premium gate
        needs_multi_expiry=False,
        span_model="defined",
        default_params={"move_mult": 0.0, "width": 2},
    ),
    "ratio_spread": StrategySpec(
        name="ratio_spread",
        build=build_ratio_spread,
        defined_risk=False,          # partial undefined risk (naked tail)
        sell_premium=True,           # credit strategy: 2·OTM_prem > 1·nearer_prem
        needs_multi_expiry=False,
        span_model="spread_naked_mix",
        default_params={
            "variant": "call",
            # k1_mult=0.25 places the long leg OTM enough that 2×c2 > c1,
            # making the default entry a genuine net credit consistent with
            # sell_premium=True gating.  k1_mult=0 (ATM long) tends to a
            # net debit at typical vol, which contradicts the credit intent.
            "k1_mult": 0.25,
            "k2_mult": 1.0,
            "span_pct": 0.10,
        },
    ),
}


# ---------------------------------------------------------------------------
# 8. Backtest runner
# ---------------------------------------------------------------------------


def _sharpe_from_pnls(pnls: list[float]) -> float:
    """Annualised per-trade Sharpe (× √52 weekly). Returns 0.0 if < 2 trades."""
    n = len(pnls)
    if n < 2:
        return 0.0
    mean = sum(pnls) / n
    variance = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    std = math.sqrt(variance)
    return (mean / std * math.sqrt(52)) if std > 1e-9 else 0.0


def _max_drawdown(pnls: list[float]) -> float:
    """Peak-to-trough cumulative drawdown. Returns a NEGATIVE number (or 0.0)."""
    cum = 0.0
    peak = 0.0
    worst = 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = cum - peak
        if dd < worst:
            worst = dd
    return worst


def _zero_metrics(n_cycles: int, capital: float, strategy: str) -> dict[str, Any]:
    """Return the zero-filled metrics dict shape for an empty-trades result."""
    metrics: dict[str, Any] = {
        "strategy": strategy,
        "trades": [],
        "n_cycles": n_cycles,
        "n_trades": 0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "sharpe": 0.0,
        "sharpe_is": 0.0,
        "sharpe_oos": 0.0,
        "max_drawdown": 0.0,
        "net_pnl": 0.0,
        "return_on_capital": 0.0,
        "return_on_margin": 0.0,
        "mean_span": 0.0,
    }
    metrics["go_no_go"] = go_no_go(metrics, capital=capital)
    return metrics


def run_strategy_backtest(
    spec: StrategySpec,
    cycles: list[dict[str, Any]],
    params: Optional[dict[str, Any]] = None,
    *,
    k: float = DEFAULT_K,
    capital: float = 200_000.0,
    lot: int = NIFTY_LOT,
    step: int = 50,
    slip_pct: float = 0.005,
) -> dict[str, Any]:
    """Run ``spec`` over ``cycles``. Returns the SAME metrics dict shape as
    ``fno_condor.run_backtest`` PLUS return-on-margin fields.

    Gate routing
    ------------
    - ``spec.sell_premium=True``  → trade only on ``SELL_PREMIUM`` gate decisions.
    - ``spec.sell_premium=False`` → trade only on ``BUY_PREMIUM`` gate decisions.
    - ``STAND_ASIDE`` → never trade (either type).

    Multi-expiry guard
    ------------------
    ``needs_multi_expiry=True`` → refuse immediately with a no-go reason
    "requires term structure — historically infeasible; forward-paper only".
    Does NOT raise; returns the zero-metrics dict with that reason in go_no_go.

    Metrics dict keys
    -----------------
    Identical to fno_condor.run_backtest PLUS:
      - ``return_on_margin`` — net_pnl / Σ span (0.0 if no trades)
      - ``mean_span``        — average blocked margin per trade (₹)
      - ``sharpe_is``        — in-sample Sharpe (first 70 % of trades chronologically)
      - ``sharpe_oos``       — out-of-sample Sharpe (last 30 %)
      - ``strategy``         — spec.name

    ``go_no_go`` reused unchanged from fno_condor. ROM is informational — not yet a
    go_no_go criterion; a future revision should add an ROM floor.
    """
    if capital <= 0:
        raise ValueError("capital must be > 0")

    merged_params: dict[str, Any] = {**spec.default_params, **(params or {})}

    # ── Multi-expiry guard ─────────────────────────────────────────────────
    if spec.needs_multi_expiry:
        reason = (
            "requires term structure — historically infeasible; forward-paper only"
        )
        logger.info(
            "run_strategy_backtest: strategy=%s refused in historical mode (%s)",
            spec.name, reason,
        )
        metrics = _zero_metrics(len(cycles), capital, spec.name)
        # Override the go_no_go reason to surface the infeasibility note
        metrics["go_no_go"] = (
            False,
            f"NO-GO — {reason}. n_trades=0.",
        )
        return metrics

    trades: list[StrategyTrade] = []

    for cycle in cycles:
        # ── Required-field guard ─────────────────────────────────────────
        if any(
            cycle.get(field) is None
            for field in ("spot", "straddle_iv", "realized_vol_20d", "dte", "expiry_spot")
        ):
            continue

        entry_date: date = cycle.get("entry_date", date.today())
        expiry_date: date = cycle.get("expiry_date", date.today())
        spot: float = float(cycle["spot"])
        straddle_iv: float = float(cycle["straddle_iv"])
        dte: int = int(cycle["dte"])
        realized_vol_20d: float = float(cycle["realized_vol_20d"])
        expiry_spot: float = float(cycle["expiry_spot"])

        # ── Sanity guards (mirrors fno_condor.run_backtest) ─────────────
        if spot <= 0:
            continue
        if _implied_move(spot, straddle_iv, dte) <= 0:
            continue

        # ── Gate decision ────────────────────────────────────────────────
        decision = gate_decision(realized_vol_20d, straddle_iv, k=k)
        want = SELL_PREMIUM if spec.sell_premium else BUY_PREMIUM
        if decision != want:
            logger.debug(
                "Cycle %s skipped: gate=%s want=%s (rv=%.4f iv=%.4f)",
                cycle.get("entry_date", "<?>"),
                decision,
                want,
                realized_vol_20d,
                straddle_iv,
            )
            continue

        # ── Build legs ───────────────────────────────────────────────────
        atm_strike = _round_to_step(spot, step)
        legs = spec.build(spot, atm_strike, step, dte, straddle_iv, merged_params)

        # ── Price + slippage ─────────────────────────────────────────────
        mids = [price_leg_hist(leg, spot, straddle_iv, dte) for leg in legs]
        fills_ = [fill_price(leg, mid, slip_pct) for leg, mid in zip(legs, mids)]

        # ── Entry net (credit or debit) ──────────────────────────────────
        entry_net = net_premium_per_unit(legs, fills_)

        # ── Resolution ──────────────────────────────────────────────────
        gross_result = resolve_legs_with_fills(legs, fills_, expiry_spot, lot)
        gross_pnl = gross_result["gross_pnl"]

        # ── Costs ────────────────────────────────────────────────────────
        total_costs = _compute_costs(legs, fills_, expiry_spot, lot)
        net_pnl = gross_pnl - total_costs

        # ── SPAN margin ──────────────────────────────────────────────────
        span = span_margin(spec, legs, entry_net, spot, lot, merged_params)

        # ── Jade lizard: compute upside_risk per cycle ───────────────────
        # upside_risk = max(0, call_spread_width − entry_net_per_unit).
        # Non-zero when the invariant (credit ≥ spread width) is violated.
        upside_risk = 0.0
        if spec.name == "jade_lizard":
            # Legs order: [SELL PE, SELL CE, BUY CE]; call spread = lc − sc
            ce_buy_legs  = [l for l in legs if l.option_type == "CE" and l.side == "BUY"]
            ce_sell_legs = [l for l in legs if l.option_type == "CE" and l.side == "SELL"]
            if ce_buy_legs and ce_sell_legs:
                call_spread_width = ce_buy_legs[0].strike - ce_sell_legs[0].strike
                upside_risk = max(0.0, call_spread_width - entry_net)

        trades.append(
            StrategyTrade(
                entry_date=entry_date,
                expiry_date=expiry_date,
                legs=legs,
                fills=fills_,
                entry_net=entry_net,
                gross_pnl=gross_pnl,
                costs=total_costs,
                net_pnl=net_pnl,
                span=span,
                win=net_pnl > 0,
                gate_decision_label=decision,
                straddle_iv=straddle_iv,
                realized_vol_20d=realized_vol_20d,
                expiry_spot=expiry_spot,
                strategy=spec.name,
                upside_risk=upside_risk,
            )
        )

    # ── Aggregate ─────────────────────────────────────────────────────────
    n_cycles = len(cycles)
    n_trades = len(trades)

    if n_trades == 0:
        return _zero_metrics(n_cycles, capital, spec.name)

    # Sort by entry_date (chronological) before IS/OOS split.
    # Precondition: trades must be in chronological order so the 70/30 split
    # correctly separates earlier (in-sample) from later (out-of-sample) cycles.
    # Without sorting, unsorted cycle input would leak IS data into the OOS set.
    trades.sort(key=lambda t: t.entry_date)

    pnls = [t.net_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = len(wins) / n_trades
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    sharpe = _sharpe_from_pnls(pnls)

    # IS/OOS split — 70/30 chronological (NEVER random on a time series)
    split_idx = max(1, int(0.7 * n_trades))
    pnls_is = pnls[:split_idx]
    pnls_oos = pnls[split_idx:]
    sharpe_is = _sharpe_from_pnls(pnls_is) if len(pnls_is) >= 2 else 0.0
    sharpe_oos = _sharpe_from_pnls(pnls_oos) if len(pnls_oos) >= 2 else 0.0

    max_dd = _max_drawdown(pnls)
    net_pnl = sum(pnls)
    return_on_capital = net_pnl / capital

    spans = [t.span for t in trades]
    total_span = sum(spans)
    return_on_margin = net_pnl / total_span if total_span > 0 else 0.0
    mean_span = total_span / n_trades

    metrics: dict[str, Any] = {
        "strategy": spec.name,
        "trades": trades,
        "n_cycles": n_cycles,
        "n_trades": n_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "sharpe": sharpe,
        "sharpe_is": sharpe_is,
        "sharpe_oos": sharpe_oos,
        "max_drawdown": max_dd,
        "net_pnl": net_pnl,
        "return_on_capital": return_on_capital,
        "return_on_margin": return_on_margin,
        "mean_span": mean_span,
    }
    go_result = go_no_go(metrics, capital=capital)

    # For undefined-risk (naked / partial) strategies, append a disclaimer so
    # a GO verdict is never read as clean.  These strategies have no intra-cycle
    # path stop in the backtest — only expiry resolution — making them tail-blind.
    _UNDEFINED_RISK_DISCLAIMER = (
        " | UNDEFINED-RISK, EXPIRY-ONLY backtest — tail-blind"
        " (no path stop, SPAN approximate); treat any GO as diagnostic only."
    )
    _UNDEFINED_RISK_STRATEGIES = {
        "short_straddle", "short_strangle", "jade_lizard", "ratio_spread",
    }
    if not spec.defined_risk or spec.name in _UNDEFINED_RISK_STRATEGIES:
        go_flag, go_reason = go_result
        go_result = (go_flag, go_reason + _UNDEFINED_RISK_DISCLAIMER)

    metrics["go_no_go"] = go_result
    return metrics


# ---------------------------------------------------------------------------
# 9. Optional CLI (never edits equity __main__.py)
# ---------------------------------------------------------------------------


def main() -> None:  # pragma: no cover
    """Entry point for ``python -m research.backtest.fno_strategies``."""
    parser = argparse.ArgumentParser(
        description="Multi-leg F&O backtest engine — strategy selector",
    )
    parser.add_argument(
        "--strategy",
        default="iron_condor",
        choices=[*FNO_STRATEGIES, "all"],
        help=f"Strategy name or 'all'. Choices: {[*list(FNO_STRATEGIES), 'all']}",
    )
    parser.add_argument(
        "--mode",
        default="weekly",
        choices=["weekly", "expiry_calendar"],
    )
    parser.add_argument("--k", type=float, default=DEFAULT_K)
    parser.add_argument("--capital", type=float, default=200_000.0)
    parser.add_argument("--span-pct", type=float, default=None)
    parser.add_argument(
        "--param",
        action="append",
        metavar="KEY=VAL",
        default=[],
        help="Extra params as key=val (may be repeated). Val is parsed as float if possible.",
    )
    args = parser.parse_args()

    extra: dict[str, Any] = {}
    if args.span_pct is not None:
        extra["span_pct"] = args.span_pct
    for kv in args.param:
        k2, _, v = kv.partition("=")
        try:
            extra[k2.strip()] = float(v)
        except ValueError:
            extra[k2.strip()] = v

    cycs = cycles_from_db(mode=args.mode)

    names = list(FNO_STRATEGIES) if args.strategy == "all" else [args.strategy]
    rows = []
    for name in names:
        spec = FNO_STRATEGIES[name]
        m = run_strategy_backtest(spec, cycs, extra, k=args.k, capital=args.capital)
        go, reason = m["go_no_go"]
        rows.append((name, m["n_trades"], m["net_pnl"], m["return_on_margin"], go, reason))

    if args.strategy == "all":
        hdr = f"{'strategy':<20} {'n':>5} {'net_pnl':>12} {'ROM':>8} {'go?':>5}"
        print(hdr)
        print("-" * len(hdr))
        for name, n, pnl, rom, go, _ in rows:
            print(f"{name:<20} {n:>5} {pnl:>12,.0f} {rom:>8.2%} {'GO' if go else 'NO-GO':>5}")
    else:
        name, n, pnl, rom, go, reason = rows[0]
        print(f"strategy={name}  n_trades={n}  net_pnl=₹{pnl:,.0f}  ROM={rom:.2%}")
        print(f"go_no_go: {reason}")


if __name__ == "__main__":
    main()
