"""Tests for research/backtest/mcx_costs.py — MCX commodities cost stack.

Pure math, no I/O. Worked full-lot round-trips for CRUDEOIL / GOLDM /
NATURALGAS must land at ≈2.2–3.2 bps of one-side notional (statutory +
brokerage, slippage excluded), matching the cost research. Also checks the
component breakdown, the brokerage floor, the slippage hook, and the options
round-trip.
"""
import pytest

from research.backtest import mcx_costs as mc


# ── futures round-trip: bps targets ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "name,notional,lo,hi",
    [
        ("CRUDEOIL 100bbl", 550_000, 2.2, 3.2),
        ("GOLDM 100g",      730_000, 2.2, 3.2),
        ("NATURALGAS 1250", 312_500, 2.2, 3.3),  # NG ≈3.23 bps — top of the band
    ],
)
def test_futures_roundtrip_bps_in_research_band(name, notional, lo, hi):
    c = mc.mcx_futures_roundtrip(notional, slippage_pct=0)  # statutory+brokerage only
    bps = c.total / notional * 1e4
    assert lo <= bps <= hi, f"{name}: {bps:.3f} bps outside [{lo}, {hi}]"


def test_futures_roundtrip_component_breakdown():
    """Verify each statutory component for a 5.5L/side CRUDEOIL round-trip."""
    n = 550_000.0
    c = mc.mcx_futures_roundtrip(n, slippage_pct=0)
    # CTT 0.01% sell side only
    assert c.ctt == pytest.approx(0.0001 * n, rel=1e-6)            # 55.0
    # MCX exchange ≈2.10/lakh on total (buy+sell) turnover
    assert c.exchange_fee == pytest.approx(mc.MCX_TXN_FUT_PCT * 2 * n, rel=1e-6)
    # SEBI 10/cr on total turnover
    assert c.sebi_fee == pytest.approx(mc.SEBI_PCT * 2 * n, rel=1e-6)
    # stamp 0.002% buy side only
    assert c.stamp_duty == pytest.approx(0.00002 * n, rel=1e-6)    # 11.0
    # brokerage: flat ₹20 floor hit on both sides (0.03% of 5.5L = 165 > 20)
    assert c.brokerage == pytest.approx(40.0)
    # GST 18% on (brokerage+exchange+sebi)
    assert c.gst == pytest.approx(0.18 * (c.brokerage + c.exchange_fee + c.sebi_fee), abs=0.01)
    # total = sum of all components (no slippage here)
    assert c.total == pytest.approx(
        c.brokerage + c.ctt + c.exchange_fee + c.sebi_fee + c.stamp_duty + c.gst,
        abs=0.02,
    )
    assert c.slippage == 0.0


def test_futures_brokerage_floor_and_pct():
    # Small notional → 0.03% is below ₹20 → floor ₹20 applies
    assert mc.futures_brokerage(10_000) == pytest.approx(min(20.0, 0.0003 * 10_000))
    assert mc.futures_brokerage(10_000) == pytest.approx(3.0)
    # Large notional → 0.03% caps at ₹20 (since 0.03% of 1L = 30 > 20)
    assert mc.futures_brokerage(1_000_000) == pytest.approx(20.0)


def test_futures_slippage_hook_adds_to_total():
    n = 550_000.0
    no_slip = mc.mcx_futures_roundtrip(n, slippage_pct=0)
    with_slip = mc.mcx_futures_roundtrip(n, slippage_pct=0.0002)  # 2 bps/side
    assert with_slip.slippage == pytest.approx(0.0002 * n * 2)    # both sides
    assert with_slip.total == pytest.approx(no_slip.total + with_slip.slippage, abs=0.02)


def test_slippage_helper_nonnegative():
    assert mc.slippage(100_000, 0.0002) == pytest.approx(20.0)
    assert mc.slippage(100_000, -1.0) == 0.0


# ── options round-trip ───────────────────────────────────────────────────────────
def test_options_roundtrip_spread_components():
    """Credit spread: sell 50k premium, buy 20k wing (slippage off)."""
    sell, buy = 50_000.0, 20_000.0
    c = mc.mcx_options_roundtrip(sell, buy, slippage_pct=0)
    assert c.ctt == pytest.approx(mc.CTT_OPT_SELL_PCT * sell)       # 0.05% sell only → 25.0
    assert c.exchange_fee == pytest.approx(mc.MCX_TXN_OPT_PCT * (sell + buy), rel=1e-6)
    assert c.sebi_fee == pytest.approx(mc.SEBI_PCT * (sell + buy), rel=1e-6)
    assert c.stamp_duty == pytest.approx(mc.STAMP_OPT_BUY_PCT * buy)  # buy side only
    assert c.brokerage == pytest.approx(40.0)                        # two legs × ₹20
    assert c.gst == pytest.approx(0.18 * (c.brokerage + c.exchange_fee + c.sebi_fee), abs=0.01)
    assert c.slippage == 0.0


def test_options_roundtrip_naked_sell_single_leg():
    """A naked sell (buy_prem=0) → one leg of brokerage, no stamp, CTT on sell."""
    c = mc.mcx_options_roundtrip(sell_prem=50_000.0, buy_prem=0.0, slippage_pct=0)
    assert c.brokerage == pytest.approx(20.0)   # single leg
    assert c.stamp_duty == 0.0                  # no buy side
    assert c.ctt == pytest.approx(mc.CTT_OPT_SELL_PCT * 50_000.0)


def test_options_slippage_on_premium():
    c = mc.mcx_options_roundtrip(50_000.0, 20_000.0, slippage_pct=0.005)
    assert c.slippage == pytest.approx(0.005 * 50_000.0 + 0.005 * 20_000.0)


# ── rates are marked verify-me (guard against accidental "blessing") ──────────────
def test_rates_are_documented_as_unverified():
    """Every rate constant is provisional; the module docstring + inline comments
    flag them # [verify-me]. This test just pins the working values so a silent
    change is caught in review."""
    assert mc.CTT_FUT_SELL_PCT == 0.0001
    assert mc.CTT_OPT_SELL_PCT == 0.0005
    assert mc.MCX_TXN_FUT_PCT == 0.0000210
    assert mc.MCX_TXN_OPT_PCT == 0.0004180
    assert mc.SEBI_PCT == 0.000001
    assert mc.STAMP_FUT_BUY_PCT == 0.00002
    assert mc.GST_PCT == 0.18
    assert mc.BROKERAGE_FLAT == 20.0
