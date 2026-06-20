"""
NIFTY index-OPTIONS cost stack — post-April-2026 STT hike (NSE, Dhan rates).

Every backtested options leg pays the full statutory + brokerage stack.
Options costs are structurally different from equity intraday: STT is
sell-side-only on premium (not turnover), exercise carries a separate ITM
intrinsic-value STT, and slippage is expressed as a % of premium rather than
price.

Rates (post-April-2026 NSE/SEBI notifications):
    Brokerage       ₹20 flat per executed order (discount-broker standard)
    STT (sell)      0.15% of premium on SELL side  [raised from 0.10% Apr-2026]
    STT (exercise)  0.15% of intrinsic value on ITM exercise/assignment
                    [raised from 0.125% Apr-2026; handoff §7]
    NSE txn fee     ≈0.03503% of premium turnover (₹35.03/lakh)  [verify-me:
                    confirm current NSE circular; was 0.05% pre-SEBI
                    rationalisation, then revised post-Oct 2024 — the exact
                    current per-lakh rate must be checked before the go/no-go]
    SEBI fee        ₹10 per crore of turnover (0.000001)
    IPFT levy       ₹1 per crore of premium turnover (0.0000001) — defined as a
                    constant (IPFT_PCT) but NOT folded into the cost total: the
                    validated condor_costs path is regression-protected and must
                    not change. [verify-me]
    Stamp duty      0.003% on the BUY side premium
    GST             18% on (brokerage + exchange fee + SEBI fee)

NIFTY lot size = 65 units (as at June 2026); callers must pass qty in units.

Reference: handoff §7; the transaction-charge rationalisation was directed by
a SEBI circular dated Jul 2024 and implemented by NSE from Oct 2024 — the
exact NSE circular number is [verify-me before publishing the go/no-go] and
should not be invented or assumed.

core/charges.py (if it exists) is the runtime companion; this module is pure
backtest math with no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Statutory + brokerage rates — post-April-2026
# ---------------------------------------------------------------------------

# NIFTY contract lot size (units per lot) — confirmed 65 from the live Dhan detailed
# scrip master (2026-06-19). Single source of truth for the cost stack and the condor
# harness (research/backtest/fno_condor.py). Prefer reading lot_size from fno_instruments
# at runtime once that table is populated; this constant is the static fallback.
NIFTY_LOT = 65

# BANKNIFTY contract lot size (units per lot). NSE rationalised index-derivatives lot
# sizes in the Oct/Nov-2024 contract-size revision; BANKNIFTY was set to 35 units (up
# from 15). [verify-me before publishing the go/no-go — read LOT_SIZE for the live
# BANKNIFTY OPTIDX/FUTIDX rows from the Dhan detailed scrip master
# (core/fno_instruments.py → lot_size); do NOT assume this is still current.] This is a
# documented static fallback only; the scalper is NIFTY-first and BANKNIFTY is currently
# data-blocked (forward-only ingestion).
BANKNIFTY_LOT = 35

# Per-index static lot-size fallback. Callers that already know the live lot size (e.g.
# from fno_instruments) should pass it explicitly; this map is the documented fallback
# used by the scalper / multi-index helpers when a DB lot is unavailable.
INDEX_LOT_SIZE: dict[str, int] = {
    "NIFTY": NIFTY_LOT,
    "BANKNIFTY": BANKNIFTY_LOT,
}

# Post-April-2026 (handoff §7): options SELL-side STT raised 0.10% -> 0.15% on premium;
# exercise STT raised 0.125% -> 0.15% of intrinsic value.
OPTION_STT_SELL_PCT = 0.0015  # 0.15% of premium, SELL side only
OPTION_EXERCISE_STT_PCT = 0.0015  # 0.15% of intrinsic on ITM exercise/assignment

BROKERAGE_PER_ORDER = 20.0  # flat ₹20 per executed order (discount-broker standard)

OPTION_EXCHANGE_PCT = 0.0003503  # ₹35.03 / lakh of premium turnover — NSE options txn charge [verify-me before publishing the go/no-go: confirm the current NSE circular rate]

SEBI_PCT = 0.000001  # ₹10 / crore of turnover
IPFT_PCT = 0.0000001  # ₹1 / crore — NSE Investor Protection Fund Trust levy on options premium turnover. DEFINED but intentionally NOT wired into condor_costs/scalp totals (would change the validated, regression-protected condor path); negligible (~0.0001x SEBI) and pending a real-rate check [verify-me]
STAMP_BUY_PCT = 0.00003  # 0.003% on buy-side premium
GST_PCT = 0.18  # 18% on (brokerage + exchange fee + SEBI fee)


# ---------------------------------------------------------------------------
# Cost dataclass — mirrors RoundTripCosts in costs.py
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionTradeCosts:
    brokerage: float
    stt: float
    exchange_fee: float
    sebi_fee: float
    stamp_duty: float
    gst: float
    total: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def leg_turnover(premium: float, qty: int) -> float:
    """Premium turnover for one leg: premium (₹/unit) * qty (units).

    NIFTY standard lot = 65 units; callers are responsible for passing the
    correct qty (multiples of 65 for whole lots).
    """
    return premium * qty


def slippage(premium: float, pct: float = 0.005) -> float:
    """Adverse slippage on a single leg's premium.

    Default 0.5% — conservative estimate for liquid NIFTY options; wider
    spreads may warrant 1%+.  Always >= 0.
    """
    return max(0.0, premium * pct)


# ---------------------------------------------------------------------------
# Main cost calculator
# ---------------------------------------------------------------------------


def condor_costs(
    legs: list[tuple[float, int, str]],
    exercise_intrinsic: float = 0.0,
) -> OptionTradeCosts:
    """Compute the full statutory + brokerage cost for a multi-leg options trade.

    Parameters
    ----------
    legs:
        List of ``(premium_per_unit, qty_units, side)`` tuples.
        ``side`` must be ``"BUY"`` or ``"SELL"``.
        An iron condor entry is 4 legs (2 SELL short strikes + 2 BUY long
        wings); exit reverses them — callers pass all executed legs.
        ``qty_units`` should already reflect lot size (e.g. 65 for 1 NIFTY lot).
    exercise_intrinsic:
        Aggregate intrinsic value (₹) on any ITM legs that are exercised or
        assigned.  Pass 0.0 for trades closed in the market (no exercise STT).

    Returns
    -------
    OptionTradeCosts
        All components rounded consistently with costs.py (4 dp for small fees,
        2 dp for brokerage/stt/gst/total).
    """
    buy_turnover_total = 0.0
    sell_turnover_total = 0.0

    for premium, qty, side in legs:
        to = leg_turnover(premium, qty)
        if side == "BUY":
            buy_turnover_total += to
        elif side == "SELL":
            sell_turnover_total += to
        else:
            raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")

    total_turnover = buy_turnover_total + sell_turnover_total
    n_legs = len(legs)

    brokerage = BROKERAGE_PER_ORDER * n_legs

    stt = (
        OPTION_STT_SELL_PCT * sell_turnover_total
        + OPTION_EXERCISE_STT_PCT * exercise_intrinsic
    )

    exchange = OPTION_EXCHANGE_PCT * total_turnover

    sebi = SEBI_PCT * total_turnover

    stamp = STAMP_BUY_PCT * buy_turnover_total

    gst = GST_PCT * (brokerage + exchange + sebi)

    total = brokerage + stt + exchange + sebi + stamp + gst

    return OptionTradeCosts(
        brokerage=round(brokerage, 2),
        stt=round(stt, 2),
        exchange_fee=round(exchange, 4),
        sebi_fee=round(sebi, 4),
        stamp_duty=round(stamp, 4),
        gst=round(gst, 2),
        total=round(total, 2),
    )


# ---------------------------------------------------------------------------
# Scalper — single-leg long-option round trip (BUY → SELL)
# ---------------------------------------------------------------------------
#
# The options scalper is a DIRECTIONAL, single-leg trade: buy one option
# (CE or PE), hold for a short window, sell to close. Its cost profile differs
# from the (multi-leg, premium-selling) condor in two ways that matter for
# honest EV:
#
#   1. Drag is dominated by the bid/ask SPREAD, not the statutory stack. A
#      scalp must move enough to clear half-the-spread on BOTH the entry and the
#      exit (cross the spread twice). We model that as a per-leg slippage of
#      ``half_spread_pct`` of the premium, applied to entry and exit premium.
#   2. There is no exercise — the trade is always closed in the market, so
#      ``exercise_intrinsic=0``.
#
# Statutory + brokerage costs are computed by ``condor_costs`` (the validated,
# regression-protected path) over the two legs [(entry, qty, BUY), (exit, qty,
# SELL)]. STT (sell-only), stamp (buy-only), exchange / SEBI / GST therefore
# behave EXACTLY as in the condor. Slippage is returned SEPARATELY (not folded
# into the statutory total) so reports can show spread cost on its own line.


def scalp_round_trip_costs(
    entry_premium: float,
    exit_premium: float,
    qty: int,
    half_spread_pct: float = 0.0075,
) -> tuple[OptionTradeCosts, float]:
    """Cost of one single-leg long-option scalp round trip (BUY then SELL).

    Parameters
    ----------
    entry_premium:
        Premium per unit paid on the BUY leg (₹/unit).
    exit_premium:
        Premium per unit received on the SELL leg (₹/unit).
    qty:
        Quantity in UNITS (e.g. one NIFTY lot = ``NIFTY_LOT`` = 65 units).
    half_spread_pct:
        Half the bid/ask spread as a fraction of premium, applied per leg
        (default 0.75% — a single liquid NIFTY option leg crosses ~0.75% of
        premium each way, ~1.5% round trip). Negative values are floored at 0
        by :func:`slippage`.

    Returns
    -------
    (OptionTradeCosts, slippage_cost)
        ``OptionTradeCosts`` — statutory + brokerage for the two legs, computed
        by :func:`condor_costs` (STT sell-only, stamp buy-only, exchange/SEBI/
        GST as for the condor; ``exercise_intrinsic=0``).
        ``slippage_cost`` (float, ₹) — total spread cost across BOTH legs,
        ``slippage(entry_premium, half_spread_pct) * qty +
          slippage(exit_premium, half_spread_pct) * qty``. Kept SEPARATE from
        the statutory total so callers/reports can surface it independently.

    Raises
    ------
    ValueError
        If either premium is negative or ``qty <= 0`` (a scalp always trades a
        positive quantity at a non-negative premium; negative inputs would
        silently flip the sign of the statutory turnover).
    """
    if entry_premium < 0 or exit_premium < 0:
        raise ValueError(
            f"premiums must be >= 0, got entry={entry_premium!r}, exit={exit_premium!r}"
        )
    if qty <= 0:
        raise ValueError(f"qty must be > 0, got {qty!r}")

    legs = [
        (entry_premium, qty, "BUY"),
        (exit_premium, qty, "SELL"),
    ]
    statutory = condor_costs(legs, exercise_intrinsic=0.0)

    entry_slip = slippage(entry_premium, half_spread_pct) * qty
    exit_slip = slippage(exit_premium, half_spread_pct) * qty
    slippage_cost = round(entry_slip + exit_slip, 2)

    return statutory, slippage_cost


def scalp_breakeven(
    premium: float,
    lot_size: int = NIFTY_LOT,
    delta: float = 0.5,
    half_spread_pct: float = 0.0075,
) -> dict[str, float]:
    """Favorable move a long-option scalp must make to clear total drag.

    Computes the round-trip drag (statutory + brokerage + double-crossed
    spread) for a 1-lot scalp entered and exited at ``premium``, then converts
    it into the option-premium move and the UNDERLYING move needed to break
    even.

    The break-even premium move is the total round-trip cost spread over the
    lot's units; the underlying move is that premium move divided by the option
    ``delta`` (a 0.5-delta ATM option gains ~₹0.5 of premium per ₹1 of
    underlying).

    Parameters
    ----------
    premium:
        Per-unit option premium at entry/exit (₹/unit). Assumed equal on both
        legs for the break-even estimate (a flat round trip).
    lot_size:
        Units per lot (default ``NIFTY_LOT`` = 65). Pass ``BANKNIFTY_LOT`` or a
        live lot size for other indices.
    delta:
        Option delta used to translate premium move → underlying move
        (default 0.5, ATM). Must be > 0.
    half_spread_pct:
        Half-spread per leg as a fraction of premium (default 0.75%).

    Returns
    -------
    dict with keys:
        ``premium_pct``      — break-even premium move as a % of entry premium.
        ``premium_points``   — break-even premium move in ₹/unit.
        ``underlying_points``— break-even underlying move in index points.

    Raises
    ------
    ValueError
        If ``delta <= 0`` or ``lot_size <= 0``.
    """
    if delta <= 0:
        raise ValueError(f"delta must be > 0, got {delta!r}")
    if lot_size <= 0:
        raise ValueError(f"lot_size must be > 0, got {lot_size!r}")

    statutory, slippage_cost = scalp_round_trip_costs(
        entry_premium=premium,
        exit_premium=premium,
        qty=lot_size,
        half_spread_pct=half_spread_pct,
    )
    total_drag = statutory.total + slippage_cost

    premium_points = total_drag / lot_size
    premium_pct = (premium_points / premium * 100.0) if premium > 0 else 0.0
    underlying_points = premium_points / delta

    return {
        "premium_pct": round(premium_pct, 4),
        "premium_points": round(premium_points, 4),
        "underlying_points": round(underlying_points, 4),
    }
