"""
MCX (commodities) futures + options cost stack — Dhan / MCX rates.

Mirrors research/backtest/fno_costs.py for the MCX commodity segment. Every
backtested MCX leg pays the full statutory + brokerage stack. MCX costs differ
from NSE equity/F&O in a few ways: the sell-side transaction tax is **CTT**
(Commodities Transaction Tax), not STT; the MCX exchange transaction charge is
much lower than NSE on futures (≈₹2.10/lakh vs ₹35/lakh); and CTT on non-agri
commodity *futures* is 0.01% sell-side, while *options* carry 0.05% on the sell
premium.

EVERY rate below is marked ``# [verify-me]`` — these are working figures drawn
from the cost research and MUST be confirmed against the live MCX circular /
Dhan tariff before any go/no-go is published. Do NOT treat them as authoritative.

Rates (MCX, working figures — all [verify-me]):
    CTT (sell, futures)   0.01%  of futures turnover, SELL side, non-agri
                          commodities (gold/silver/energy/base metals)
    CTT (sell, options)   0.05%  of options SELL premium
    MCX txn (futures)     ≈₹2.10 / lakh of futures turnover (0.0021%)
    MCX txn (options)     ≈₹41.80 / lakh of options premium turnover (0.0418%)
    SEBI fee              ₹10 per crore of turnover (0.000001)
    Stamp duty (futures)  0.002% on the BUY side futures turnover
    Stamp duty (options)  0.003% on the BUY side options premium (NSE parity;
                          [verify-me] — MCX stamp may differ by state)
    Brokerage (futures)   min(₹20, 0.03% of turnover) per executed order (Dhan)
    Brokerage (options)   ₹20 flat per executed order (Dhan)
    GST                   18% on (brokerage + exchange fee + SEBI fee)

Worked round-trips (full-lot notional, see tests/test_mcx_costs.py) land at
≈2.2–3.2 bps of one-side notional for CRUDEOIL / GOLDM / NATURALGAS, matching
the cost research. The flat ₹20 brokerage floor makes small notionals
proportionally dearer — that is correct, not a bug.

This module is pure backtest math with no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Statutory + brokerage rates — MCX (all [verify-me])
# ---------------------------------------------------------------------------

CTT_FUT_SELL_PCT = 0.0001       # 0.01% of futures turnover, SELL side (non-agri)  # [verify-me]
CTT_OPT_SELL_PCT = 0.0005       # 0.05% of options premium, SELL side               # [verify-me]

MCX_TXN_FUT_PCT = 0.0000210     # ≈₹2.10 / lakh of futures turnover                 # [verify-me]
MCX_TXN_OPT_PCT = 0.0004180     # ≈₹41.80 / lakh of options premium turnover        # [verify-me]

SEBI_PCT = 0.000001             # ₹10 / crore of turnover                            # [verify-me]

STAMP_FUT_BUY_PCT = 0.00002     # 0.002% on buy-side futures turnover                # [verify-me]
STAMP_OPT_BUY_PCT = 0.00003     # 0.003% on buy-side options premium                 # [verify-me]

GST_PCT = 0.18                  # 18% on (brokerage + exchange fee + SEBI fee)       # [verify-me]

BROKERAGE_FLAT = 20.0           # ₹20 flat per executed order (Dhan options/floor)   # [verify-me]
BROKERAGE_FUT_PCT = 0.0003      # 0.03% of turnover (futures) — capped at the flat   # [verify-me]


# ---------------------------------------------------------------------------
# Cost dataclass — mirrors OptionTradeCosts in fno_costs.py
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class McxTradeCosts:
    brokerage: float
    ctt: float
    exchange_fee: float
    sebi_fee: float
    stamp_duty: float
    gst: float
    slippage: float
    total: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def futures_brokerage(turnover: float) -> float:
    """Dhan MCX futures brokerage for one executed order: min(₹20, 0.03% of
    turnover). [verify-me]"""
    return min(BROKERAGE_FLAT, BROKERAGE_FUT_PCT * turnover)


def slippage(notional: float, pct: float = 0.0002) -> float:
    """Adverse slippage hook for one MCX leg, expressed as a fraction of the
    leg's notional (default 2 bps — MCX top contracts are liquid; widen for
    illiquid/far-month). Always >= 0. [verify-me]"""
    return max(0.0, notional * pct)


# ---------------------------------------------------------------------------
# Futures round-trip
# ---------------------------------------------------------------------------


def mcx_futures_roundtrip(
    notional_per_side: float,
    *,
    slippage_pct: float = 0.0002,
) -> McxTradeCosts:
    """Full statutory + brokerage cost for a one-lot MCX *futures* round-trip
    (BUY entry + SELL exit), each side at ``notional_per_side`` (₹ = price ×
    lot units).

    CTT is sell-side only; stamp is buy-side only; the MCX exchange charge and
    SEBI fee apply to total (buy + sell) turnover; GST is on
    (brokerage + exchange + SEBI). Brokerage is charged per order (2 orders).

    A slippage component (default 2 bps/side, both sides) is included in
    ``total`` and broken out as ``slippage``; pass ``slippage_pct=0`` to exclude
    it (e.g. when slippage is modelled in the fill price instead).
    """
    buy_n = notional_per_side
    sell_n = notional_per_side
    total_turnover = buy_n + sell_n

    brokerage = futures_brokerage(buy_n) + futures_brokerage(sell_n)
    ctt = CTT_FUT_SELL_PCT * sell_n
    exchange = MCX_TXN_FUT_PCT * total_turnover
    sebi = SEBI_PCT * total_turnover
    stamp = STAMP_FUT_BUY_PCT * buy_n
    gst = GST_PCT * (brokerage + exchange + sebi)
    slip = slippage(buy_n, slippage_pct) + slippage(sell_n, slippage_pct)

    total = brokerage + ctt + exchange + sebi + stamp + gst + slip

    return McxTradeCosts(
        brokerage=round(brokerage, 2),
        ctt=round(ctt, 2),
        exchange_fee=round(exchange, 4),
        sebi_fee=round(sebi, 4),
        stamp_duty=round(stamp, 4),
        gst=round(gst, 2),
        slippage=round(slip, 2),
        total=round(total, 2),
    )


# ---------------------------------------------------------------------------
# Options round-trip
# ---------------------------------------------------------------------------


def mcx_options_roundtrip(
    sell_prem: float,
    buy_prem: float,
    *,
    slippage_pct: float = 0.005,
) -> McxTradeCosts:
    """Full statutory + brokerage cost for an MCX *options* trade with one SELL
    leg (premium turnover ``sell_prem``) and one BUY leg (``buy_prem``), each a
    ₹ premium turnover (premium/unit × lot units).

    For a defined-risk spread (sell a near strike, buy a far wing) pass both;
    for a naked sell pass ``buy_prem=0`` (and vice versa). CTT is 0.05% on the
    SELL premium; stamp is on the BUY premium; the MCX options exchange charge
    and SEBI fee apply to total premium turnover. Brokerage is ₹20 flat per leg
    (one per non-zero leg). Slippage (default 0.5% of premium per leg) is on the
    premium, mirroring fno_costs.slippage.
    """
    sell_n = max(0.0, sell_prem)
    buy_n = max(0.0, buy_prem)
    total_turnover = sell_n + buy_n
    n_legs = (1 if sell_n > 0 else 0) + (1 if buy_n > 0 else 0)

    brokerage = BROKERAGE_FLAT * n_legs
    ctt = CTT_OPT_SELL_PCT * sell_n
    exchange = MCX_TXN_OPT_PCT * total_turnover
    sebi = SEBI_PCT * total_turnover
    stamp = STAMP_OPT_BUY_PCT * buy_n
    gst = GST_PCT * (brokerage + exchange + sebi)
    slip = (sell_n * slippage_pct if sell_n > 0 else 0.0) + (
        buy_n * slippage_pct if buy_n > 0 else 0.0
    )

    total = brokerage + ctt + exchange + sebi + stamp + gst + slip

    return McxTradeCosts(
        brokerage=round(brokerage, 2),
        ctt=round(ctt, 2),
        exchange_fee=round(exchange, 4),
        sebi_fee=round(sebi, 4),
        stamp_duty=round(stamp, 4),
        gst=round(gst, 2),
        slippage=round(slip, 2),
        total=round(total, 2),
    )
