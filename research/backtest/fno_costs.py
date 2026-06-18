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
    NSE txn fee     ≈0.03553% of premium turnover (₹35.53/lakh)  [verify-me:
                    confirm current NSE circular; was 0.05% pre-SEBI
                    rationalisation, then revised to ₹35.53/lakh eff. Oct 2024]
    SEBI fee        ₹10 per crore of turnover (0.000001)
    Stamp duty      0.003% on the BUY side premium
    GST             18% on (brokerage + exchange fee + SEBI fee)

NIFTY lot size = 75 units (as at June 2026); callers must pass qty in units.

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

# Post-April-2026 (handoff §7): options SELL-side STT raised 0.10% -> 0.15% on premium;
# exercise STT raised 0.125% -> 0.15% of intrinsic value.
OPTION_STT_SELL_PCT = 0.0015  # 0.15% of premium, SELL side only
OPTION_EXERCISE_STT_PCT = 0.0015  # 0.15% of intrinsic on ITM exercise/assignment

BROKERAGE_PER_ORDER = 20.0  # flat ₹20 per executed order (discount-broker standard)

OPTION_EXCHANGE_PCT = 0.0003553  # ₹35.53 / lakh of premium turnover — NSE rate eff. Oct 2024 [verify-me before publishing the go/no-go]

SEBI_PCT = 0.000001  # ₹10 / crore of turnover
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

    NIFTY standard lot = 75 units; callers are responsible for passing the
    correct qty (multiples of 75 for whole lots).
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
        ``qty_units`` should already reflect lot size (e.g. 75 for 1 NIFTY lot).
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
