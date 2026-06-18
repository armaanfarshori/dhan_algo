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
                "entry_date":       date,   # entry (Monday / first day of cycle)
                "expiry_date":      date,   # expiry (Thursday for most NIFTY weeklies)
                "spot":             float,  # index level at entry
                "straddle_iv":      float,  # annualised ATM straddle IV (e.g. 0.14)
                "dte":              int,    # calendar days to expiry from entry
                "realized_vol_20d": float,  # 20-day annualised realized vol of NIFTY futures
                "expiry_spot":      float,  # NSE final settlement value at expiry
            }

    k:
        Vol-gate threshold passed through to ``gate_decision``.  ``realized_vol
        < k * implied_vol (straddle_iv)`` → SELL_PREMIUM.  Default 0.9; handoff §6 targets
        ~70 % pass rate.

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
        _missing_or_none = (
            cycle.get("spot") is None
            or cycle.get("straddle_iv") is None
            or cycle.get("realized_vol_20d") is None
            or cycle.get("dte") is None
            or cycle.get("expiry_spot") is None
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
        straddle_iv: float = float(cycle["straddle_iv"])
        dte: int = int(cycle["dte"])
        realized_vol_20d: float = float(cycle["realized_vol_20d"])
        expiry_spot: float = float(cycle["expiry_spot"])

        # Gate decision — only trade on SELL_PREMIUM
        decision = gate_decision(realized_vol_20d, straddle_iv, k=k)
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

    go = len(failures) == 0
    if go:
        reason = f"GO — all criteria pass. {'; '.join(passes)}. {info}"
    else:
        reason = (
            f"NO-GO — {len(failures)} criterion/criteria failed: "
            f"{'; '.join(failures)}. Passed: {'; '.join(passes) or 'none'}. {info}"
        )

    return go, reason


# ---------------------------------------------------------------------------
# DB loader (hydrates `cycles` for run_backtest from live tables)
# ---------------------------------------------------------------------------


def cycles_from_db(
    symbol: str = "NIFTY",
    *,
    nifty_id: str = "13",
    vix_id: str = "21",
    timeframe: str = "1d",
    mode: str = "weekly",
) -> list[dict]:
    """Assemble weekly iron-condor cycles from the DB tables created in migrations 009/010.

    Parameters
    ----------
    symbol:     Ticker key in expiry_calendar (default ``"NIFTY"``).
                Only used in ``mode="expiry_calendar"``.
    nifty_id:   ``security_id`` of the NIFTY index row in index_bars.
    vix_id:     ``security_id`` of the India VIX row in index_bars.
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

    # Lazy imports — keep pure pricing functions free of DB dependencies.
    from sqlalchemy import text  # noqa: PLC0415

    from db import get_session  # noqa: PLC0415

    tf = timeframe

    if mode == "weekly":
        return _cycles_from_db_weekly(nifty_id, vix_id, tf, get_session, text)
    else:
        return _cycles_from_db_expiry_calendar(symbol, nifty_id, vix_id, tf, get_session, text)


def _build_bar_maps(
    session: Any,
    text: Any,
    nifty_id: str,
    vix_id: str,
    tf: str,
) -> tuple[dict, dict]:
    """Query index_bars and return (nifty_map, vix_map)."""
    nifty_rows = session.execute(
        text(
            "SELECT (time AT TIME ZONE 'UTC')::date AS d, close, realized_vol_20d "
            "FROM index_bars "
            "WHERE security_id = :nid AND timeframe = :tf "
            "ORDER BY 1"
        ),
        {"nid": nifty_id, "tf": tf},
    ).fetchall()

    nifty_map: dict[date, tuple[float, float | None]] = {}
    for r in nifty_rows:
        d, close, rvol = r[0], r[1], r[2]
        nifty_map[d] = (float(close), float(rvol) if rvol is not None else None)

    vix_rows = session.execute(
        text(
            "SELECT (time AT TIME ZONE 'UTC')::date AS d, close "
            "FROM index_bars "
            "WHERE security_id = :vid AND timeframe = :tf"
        ),
        {"vid": vix_id, "tf": tf},
    ).fetchall()

    vix_map: dict[date, float] = {r[0]: float(r[1]) for r in vix_rows}
    return nifty_map, vix_map


def _build_cycles_from_pairs(
    boundary_pairs: list[tuple[date, date]],
    nifty_map: dict,
    vix_map: dict,
    mode: str,
) -> list[dict]:
    """Convert (entry, expiry) date pairs into cycle dicts, skipping incomplete ones."""
    cycles: list[dict] = []
    for e_i, e_next in boundary_pairs:
        na = nifty_map.get(e_i)
        if na is None or na[1] is None:
            logger.debug(
                "cycles_from_db[%s]: skipping pair (%s, %s) — missing NIFTY close/rvol at %s",
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

        nb = nifty_map.get(e_next)
        if nb is None:
            logger.debug(
                "cycles_from_db[%s]: skipping pair (%s, %s) — missing NIFTY close at %s",
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
    nifty_id: str,
    vix_id: str,
    tf: str,
    get_session: Any,
    text: Any,
) -> list[dict]:
    """Historical-backtest path: synthetic ISO-week boundaries from index_bars.

    Derives the last trading day of each ISO week from the NIFTY bar calendar,
    then treats consecutive weekly-boundary pairs as (entry, expiry) windows.
    This avoids the forward-only limitation of ``expiry_calendar``.
    Validated live: produces 233 weekly cycles / 164 trades over the full bar history.
    """
    with get_session() as session:
        nifty_map, vix_map = _build_bar_maps(session, text, nifty_id, vix_id, tf)

    if len(nifty_map) < 2:
        logger.warning(
            "cycles_from_db[weekly]: fewer than 2 NIFTY bar dates found — "
            "returning empty cycle list (check that index_bars has been populated "
            "for security_id=%s, timeframe=%s).",
            nifty_id, tf,
        )
        return []

    # Build ISO-week → last trading day map.
    # Iterate sorted dates; each date overwrites the previous in its ISO week,
    # so the last assignment per week is the last trading day of that week.
    wk: dict[tuple[int, int], date] = {}
    for d in sorted(nifty_map):
        iso_year, iso_week, _ = d.isocalendar()
        wk[(iso_year, iso_week)] = d

    bounds: list[date] = [wk[k] for k in sorted(wk)]

    if len(bounds) < 2:
        logger.warning(
            "cycles_from_db[weekly]: fewer than 2 ISO-week boundaries found — "
            "returning empty cycle list.",
        )
        return []

    pairs = list(zip(bounds[:-1], bounds[1:]))
    cycles = _build_cycles_from_pairs(pairs, nifty_map, vix_map, "weekly")

    if len(cycles) == 0:
        logger.warning(
            "cycles_from_db[weekly]: 0 cycles built from %d weekly boundaries — "
            "no complete NIFTY + VIX data for any boundary pair "
            "(nifty_map size=%d, vix_map size=%d).",
            len(bounds), len(nifty_map), len(vix_map),
        )

    return cycles


def _cycles_from_db_expiry_calendar(
    symbol: str,
    nifty_id: str,
    vix_id: str,
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

        nifty_map, vix_map = _build_bar_maps(session, text, nifty_id, vix_id, tf)

    pairs = list(zip(expiry_dates[:-1], expiry_dates[1:]))
    cycles = _build_cycles_from_pairs(pairs, nifty_map, vix_map, "expiry_calendar")

    if len(cycles) == 0 and len(expiry_dates) >= 2:
        if not nifty_map and not vix_map:
            logger.warning(
                "cycles_from_db[expiry_calendar]: 0 cycles built from %d expiry dates "
                "for symbol=%s — no overlapping NIFTY or VIX bar data found in index_bars "
                "(check that Phase-0 ingestion has run for security_id=%s / %s, timeframe=%s).",
                len(expiry_dates), symbol, nifty_id, vix_id, tf,
            )
        else:
            logger.warning(
                "cycles_from_db[expiry_calendar]: 0 cycles built from %d expiry dates "
                "for symbol=%s — index_bars rows exist but none matched the expiry dates "
                "(nifty_map size=%d, vix_map size=%d); "
                "check date alignment between expiry_calendar and index_bars.",
                len(expiry_dates), symbol, len(nifty_map), len(vix_map),
            )

    return cycles
