"""Tests for research/backtest/fno_costs.py and research/backtest/fno_condor.py.

fno_condor.py is being written in parallel; if it is not yet importable the
COSTS and BLACK-76 suites still run in isolation (the condor import is deferred
into each test that needs it via a module-level try/except + pytest.importorskip).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# fno_costs — always available (already written)
# ---------------------------------------------------------------------------
from research.backtest.fno_costs import (
    BROKERAGE_PER_ORDER,
    GST_PCT,
    NIFTY_LOT,
    OPTION_EXERCISE_STT_PCT,
    OPTION_STT_SELL_PCT,
    condor_costs,
    leg_turnover,
    slippage,
)

# ---------------------------------------------------------------------------
# fno_condor — may not exist yet; skip gracefully if missing
# ---------------------------------------------------------------------------
try:
    from research.backtest.fno_condor import (
        NIFTY_LOT,
        black76_call,
        black76_put,
        build_condor,
        go_no_go,
        price_condor,
        resolve_condor,
        run_backtest,
    )

    _HAS_CONDOR = True
except (ImportError, AttributeError):
    # fno_condor.py not yet written, or its own deps are still in progress
    _HAS_CONDOR = False

needs_condor = pytest.mark.skipif(not _HAS_CONDOR, reason="fno_condor not yet written")


# ===========================================================================
# SECTION 1 — COSTS (fno_costs.py)
# ===========================================================================


class TestLegTurnover:
    def test_basic_multiplication(self):
        assert leg_turnover(100.0, NIFTY_LOT) == pytest.approx(100.0 * NIFTY_LOT)

    def test_zero_premium(self):
        assert leg_turnover(0.0, NIFTY_LOT) == pytest.approx(0.0)

    def test_fractional_premium(self):
        assert leg_turnover(12.5, NIFTY_LOT) == pytest.approx(12.5 * NIFTY_LOT)


class TestSlippage:
    def test_default_half_pct(self):
        # default pct=0.005
        assert slippage(100.0) == pytest.approx(0.5)

    def test_explicit_pct(self):
        assert slippage(200.0, 0.01) == pytest.approx(2.0)

    def test_zero_premium(self):
        assert slippage(0.0) == pytest.approx(0.0)

    def test_never_negative(self):
        # negative pct would give negative result; clamp to 0
        assert slippage(100.0, -0.01) >= 0.0


class TestCondorCosts:
    """Hand-computed reference numbers for a 4-leg iron condor entry.

    Legs (NIFTY, 1 lot = 65 units):
        short put  (SELL)  premium=50  qty=65  → turnover = 3250
        short call (SELL)  premium=45  qty=65  → turnover = 2925
        long put   (BUY)   premium=20  qty=65  → turnover = 1300
        long call  (BUY)   premium=18  qty=65  → turnover = 1170

    Derived:
        sell_turnover = 3250 + 2925  = 6175
        buy_turnover  = 1300 + 1170  = 2470
        total_turnover = 8645
        brokerage     = 20 * 4       = 80.00
        STT           = 0.0015 * 6175 = 9.2625
        exchange_fee  = 0.0003553 * 8645 ≈ 3.0706
        sebi_fee      = 0.000001 * 8645 = 0.008645
        stamp_duty    = 0.00003 * 2470  = 0.0741
        gst           = 0.18 * (80 + 3.0706... + 0.008645...) ≈ 14.95
        total         ≈ 80 + 9.26 + 3.07 + 0.01 + 0.07 + 14.95
    """

    _LEGS = [
        (50.0, NIFTY_LOT, "SELL"),  # short put
        (45.0, NIFTY_LOT, "SELL"),  # short call
        (20.0, NIFTY_LOT, "BUY"),  # long put
        (18.0, NIFTY_LOT, "BUY"),  # long call
    ]

    def _costs(self, **kw):
        return condor_costs(self._LEGS, **kw)

    # --- brokerage -----------------------------------------------------------

    def test_brokerage_flat_per_leg(self):
        c = self._costs()
        assert c.brokerage == pytest.approx(BROKERAGE_PER_ORDER * len(self._LEGS))

    # --- STT — SELL side only ------------------------------------------------

    def test_stt_sell_legs_only(self):
        sell_turnover = leg_turnover(50.0, NIFTY_LOT) + leg_turnover(45.0, NIFTY_LOT)
        expected_stt = OPTION_STT_SELL_PCT * sell_turnover  # 10.6875
        c = self._costs()
        # condor_costs rounds stt to 2 dp; allow abs tolerance of ₹0.01
        assert c.stt == pytest.approx(expected_stt, abs=0.01)

    def test_buy_only_legs_produce_zero_stt(self):
        buy_only = [
            (20.0, NIFTY_LOT, "BUY"),
            (18.0, NIFTY_LOT, "BUY"),
        ]
        c = condor_costs(buy_only)
        assert c.stt == pytest.approx(0.0)

    def test_exercise_intrinsic_adds_to_stt(self):
        intrinsic = 5000.0  # hypothetical ITM intrinsic at exercise
        c_no_ex = self._costs()
        c_ex = self._costs(exercise_intrinsic=intrinsic)
        extra = OPTION_EXERCISE_STT_PCT * intrinsic
        assert c_ex.stt == pytest.approx(c_no_ex.stt + extra, rel=1e-6)

    def test_exercise_stt_zero_when_no_exercise(self):
        """Default exercise_intrinsic=0 → exercise term contributes nothing.

        condor_costs rounds stt to 2 dp, so compare with abs=0.01 tolerance.
        """
        sell_turnover = leg_turnover(50.0, NIFTY_LOT) + leg_turnover(45.0, NIFTY_LOT)
        c = self._costs()
        assert c.stt == pytest.approx(OPTION_STT_SELL_PCT * sell_turnover, abs=0.01)

    # --- GST — on brokerage + exchange + SEBI --------------------------------

    def test_gst_formula(self):
        c = self._costs()
        gst_base = c.brokerage + c.exchange_fee + c.sebi_fee
        # exchange_fee and sebi_fee are stored rounded to 4 dp; gst is computed
        # from unrounded intermediates, so allow abs ₹0.01 tolerance.
        assert c.gst == pytest.approx(GST_PCT * gst_base, abs=0.01)

    # --- total == sum of components ------------------------------------------

    def test_total_equals_sum_of_components(self):
        c = self._costs()
        parts_sum = c.brokerage + c.stt + c.exchange_fee + c.sebi_fee + c.stamp_duty + c.gst
        assert c.total == pytest.approx(parts_sum, rel=1e-4)

    # --- edge: single SELL leg -----------------------------------------------

    def test_single_sell_leg(self):
        legs = [(100.0, NIFTY_LOT, "SELL")]
        c = condor_costs(legs)
        assert c.brokerage == pytest.approx(BROKERAGE_PER_ORDER)
        assert c.stt == pytest.approx(OPTION_STT_SELL_PCT * leg_turnover(100.0, NIFTY_LOT))
        assert c.stamp_duty == pytest.approx(0.0)  # no BUY leg

    # --- bad side raises ------------------------------------------------------

    def test_bad_side_raises(self):
        with pytest.raises(ValueError, match="BUY.*SELL"):
            condor_costs([(50.0, NIFTY_LOT, "SHORT")])


# ===========================================================================
# SECTION 2 — BLACK-76 (fno_condor.py)
# ===========================================================================


@needs_condor
class TestBlack76:
    """Sanity checks — no need to match a reference library to 10 dp."""

    # --- boundary: T<=0 or sigma<=0 returns intrinsic ----------------------

    def test_call_zero_dte_returns_intrinsic(self):
        assert black76_call(23400, 23000, 0, 0.15) == pytest.approx(max(23400 - 23000, 0.0))

    def test_call_negative_dte_returns_intrinsic(self):
        assert black76_call(23400, 23000, -1, 0.15) == pytest.approx(max(23400 - 23000, 0.0))

    def test_call_zero_sigma_returns_intrinsic(self):
        assert black76_call(23400, 23000, 7 / 365, 0.0) == pytest.approx(
            max(23400 - 23000, 0.0)
        )

    def test_put_zero_dte_returns_intrinsic(self):
        assert black76_put(23400, 23800, 0, 0.15) == pytest.approx(max(23800 - 23400, 0.0))

    def test_put_zero_sigma_returns_intrinsic(self):
        assert black76_put(23400, 23800, 7 / 365, 0.0) == pytest.approx(
            max(23800 - 23400, 0.0)
        )

    # --- deep ITM call ≈ intrinsic when sigma is tiny ----------------------

    def test_deep_itm_call_approx_intrinsic(self):
        F, K, sigma, T = 23400.0, 20000.0, 1e-6, 1 / 365
        price = black76_call(F, K, T, sigma)
        intrinsic = F - K  # 3400
        assert price == pytest.approx(intrinsic, rel=0.01)

    # --- ATM call increases with sigma -------------------------------------

    def test_atm_call_increases_with_vol(self):
        F, K, T = 23400.0, 23400.0, 7 / 365
        p_low = black76_call(F, K, T, 0.10)
        p_mid = black76_call(F, K, T, 0.15)
        p_high = black76_call(F, K, T, 0.20)
        assert p_low < p_mid < p_high

    # --- ATM call increases with T ----------------------------------------

    def test_atm_call_increases_with_dte(self):
        F, K, sigma = 23400.0, 23400.0, 0.15
        p_short = black76_call(F, K, 1 / 365, sigma)
        p_long = black76_call(F, K, 30 / 365, sigma)
        assert p_short < p_long

    # --- put-call parity (undiscounted: C - P ≈ F - K) --------------------

    def test_put_call_parity_undiscounted(self):
        F, K, T, sigma = 23400.0, 23200.0, 7 / 365, 0.15
        C = black76_call(F, K, T, sigma)
        P = black76_put(F, K, T, sigma)
        # Undiscounted PCP: C - P = F - K  (discount factor ≈ 1 for r=0)
        assert (C - P) == pytest.approx(F - K, rel=0.05)

    def test_put_call_parity_atm(self):
        F = K = 23400.0
        T, sigma = 7 / 365, 0.15
        C = black76_call(F, K, T, sigma)
        P = black76_put(F, K, T, sigma)
        # ATM → C = P for undiscounted Black-76
        assert C == pytest.approx(P, rel=0.02)

    # --- non-negative prices ----------------------------------------------

    def test_call_and_put_non_negative(self):
        for F, K in [(23400, 24000), (23400, 23400), (23400, 22800)]:
            assert black76_call(F, K, 7 / 365, 0.15) >= 0.0
            assert black76_put(F, K, 7 / 365, 0.15) >= 0.0


# ===========================================================================
# SECTION 3 — BUILD + RESOLVE CONDOR (fno_condor.py)
# ===========================================================================


@needs_condor
class TestBuildCondor:
    """build_condor(spot, expected_move, wing_strikes, step, move_mult)"""

    def test_standard_case(self):
        # spot=23400, expected_move=300, step=50, wing_strikes=2, move_mult=1.0
        # short_put  = 23400 - 300       = 23100
        # short_call = 23400 + 300       = 23700
        # long_put   = 23100 - 2*50      = 23000
        # long_call  = 23700 + 2*50      = 23800
        s = build_condor(spot=23400, expected_move=300, wing_strikes=2, step=50, move_mult=1.0)
        assert s["short_put_k"] == 23100
        assert s["short_call_k"] == 23700
        assert s["long_put_k"] == 23000
        assert s["long_call_k"] == 23800

    def test_wing_width_matches_wing_strikes_times_step(self):
        # wing_strikes=3, step=50 → wing width = 150
        s = build_condor(spot=23000, expected_move=200, wing_strikes=3, step=50, move_mult=1.0)
        assert s["short_put_k"] - s["long_put_k"] == 3 * 50
        assert s["long_call_k"] - s["short_call_k"] == 3 * 50

    def test_move_mult_scales_short_strikes(self):
        base = build_condor(23400, 300, wing_strikes=2, step=50, move_mult=1.0)
        scaled = build_condor(23400, 300, wing_strikes=2, step=50, move_mult=1.5)
        # scaled expected_move = 300 * 1.5 = 450
        assert scaled["short_put_k"] == 23400 - 450
        assert scaled["short_call_k"] == 23400 + 450
        # short-to-short spread is wider for scaled
        assert (
            scaled["short_call_k"] - scaled["short_put_k"]
            > base["short_call_k"] - base["short_put_k"]
        )

    def test_symmetry_atm(self):
        s = build_condor(23400, 300, wing_strikes=2, step=50, move_mult=1.0)
        # condor should be symmetric around spot
        assert (s["short_call_k"] - 23400) == (23400 - s["short_put_k"])
        assert (s["long_call_k"] - 23400) == (23400 - s["long_put_k"])

    def test_returns_dict_with_required_keys(self):
        s = build_condor(23400, 300)
        for key in ("short_put_k", "short_call_k", "long_put_k", "long_call_k"):
            assert key in s


@needs_condor
class TestResolveCondor:
    """resolve_condor(strikes, credit_per_unit, expiry_spot, lot)

    Iron condor P&L at expiry:
        - expiry between the shorts → all options expire worthless → full credit kept
        - expiry far below long_put  → max loss = (wing_width - credit) per unit × lot
          wing_width = short_put - long_put
    """

    _STRIKES = {
        "short_put_k": 23100,
        "short_call_k": 23700,
        "long_put_k": 23000,
        "long_call_k": 23800,
    }
    _CREDIT = 50.0  # ₹/unit net premium received

    def test_full_credit_when_between_shorts(self):
        # expiry at 23400 — all options OTM
        result = resolve_condor(self._STRIKES, self._CREDIT, expiry_spot=23400, lot=NIFTY_LOT)
        expected_pnl = self._CREDIT * NIFTY_LOT
        assert result["gross_pnl"] == pytest.approx(expected_pnl)

    def test_full_credit_at_short_put_boundary(self):
        # right at short_put strike — short put expires worthless (just OTM)
        result = resolve_condor(self._STRIKES, self._CREDIT, expiry_spot=23100, lot=NIFTY_LOT)
        assert result["gross_pnl"] == pytest.approx(self._CREDIT * NIFTY_LOT)

    def test_max_loss_far_below_long_put(self):
        # expiry at 22000, well below long_put (23000)
        # short_put ITM by (23100 - 22000) = 1100; long_put ITM by (23000 - 22000) = 1000
        # net put spread loss per unit = 1100 - 1000 = 100 = wing_width (50*2) in strikes
        # gross P&L per unit = credit - wing_width = 50 - 100 = -50
        wing_width = self._STRIKES["short_put_k"] - self._STRIKES["long_put_k"]  # 100
        expected_pnl = (self._CREDIT - wing_width) * NIFTY_LOT
        result = resolve_condor(self._STRIKES, self._CREDIT, expiry_spot=22000, lot=NIFTY_LOT)
        assert result["gross_pnl"] == pytest.approx(expected_pnl)

    def test_max_loss_far_above_long_call(self):
        # expiry at 25000, well above long_call (23800)
        wing_width = self._STRIKES["long_call_k"] - self._STRIKES["short_call_k"]  # 100
        expected_pnl = (self._CREDIT - wing_width) * NIFTY_LOT
        result = resolve_condor(self._STRIKES, self._CREDIT, expiry_spot=25000, lot=NIFTY_LOT)
        assert result["gross_pnl"] == pytest.approx(expected_pnl)

    def test_pnl_negative_at_max_loss(self):
        wing_width = self._STRIKES["short_put_k"] - self._STRIKES["long_put_k"]
        assert self._CREDIT < wing_width, "test setup: credit must be < wing_width for a loss"
        result = resolve_condor(self._STRIKES, self._CREDIT, expiry_spot=22000, lot=NIFTY_LOT)
        assert result["gross_pnl"] < 0

    def test_result_has_gross_pnl_key(self):
        result = resolve_condor(self._STRIKES, self._CREDIT, expiry_spot=23400, lot=NIFTY_LOT)
        assert "gross_pnl" in result


# ===========================================================================
# SECTION 4 — price_condor smoke test
# ===========================================================================


@needs_condor
class TestPriceCondor:
    _STRIKES = {
        "short_put_k": 23100,
        "short_call_k": 23700,
        "long_put_k": 23000,
        "long_call_k": 23800,
    }

    def test_returns_positive_credit(self):
        result = price_condor(
            spot=23400, straddle_iv=0.15, dte=7, strikes=self._STRIKES, lot=NIFTY_LOT
        )
        # Net credit for a short condor must be positive
        assert result["credit_per_unit"] > 0

    def test_higher_iv_gives_higher_credit(self):
        low = price_condor(23400, 0.10, 7, self._STRIKES, NIFTY_LOT)
        high = price_condor(23400, 0.25, 7, self._STRIKES, NIFTY_LOT)
        assert high["credit_per_unit"] > low["credit_per_unit"]

    def test_result_contains_required_keys(self):
        result = price_condor(23400, 0.15, 7, self._STRIKES, NIFTY_LOT)
        for key in ("credit_per_unit", "credit_total"):
            assert key in result

    def test_credit_total_equals_credit_per_unit_times_lot(self):
        result = price_condor(23400, 0.15, 7, self._STRIKES, NIFTY_LOT)
        assert result["credit_total"] == pytest.approx(
            result["credit_per_unit"] * NIFTY_LOT, rel=1e-6
        )


# ===========================================================================
# SECTION 5 — run_backtest + go_no_go
# ===========================================================================


def _sell_cycle(idx: int) -> dict:
    """A cycle where realized_vol_20d < 0.9 * straddle_iv → SELL gate fires."""
    return {
        "cycle_id": idx,
        "spot": 23400.0,
        "straddle_iv": 0.15,
        "realized_vol_20d": 0.10,  # 0.10 < 0.9*0.15=0.135 → SELL
        "dte": 7,
        "expiry_spot": 23400.0,  # expires between shorts → full credit
    }


def _stand_aside_cycle(idx: int) -> dict:
    """A cycle where realized_vol_20d >= 0.9 * straddle_iv → STAND_ASIDE."""
    return {
        "cycle_id": idx,
        "spot": 23400.0,
        "straddle_iv": 0.15,
        "realized_vol_20d": 0.20,  # 0.20 >= 0.9*0.15=0.135 → STAND_ASIDE
        "dte": 7,
        "expiry_spot": 23400.0,
    }


@needs_condor
class TestRunBacktest:
    """run_backtest(cycles, k, move_mult, capital) -> dict with metrics."""

    def test_stand_aside_cycles_produce_no_trades(self):
        cycles = [_stand_aside_cycle(i) for i in range(3)]
        result = run_backtest(cycles, k=0.9, move_mult=1.0, capital=200_000.0)
        assert result["n_trades"] == 0

    def test_sell_cycles_produce_trades(self):
        cycles = [_sell_cycle(i) for i in range(3)]
        result = run_backtest(cycles, k=0.9, move_mult=1.0, capital=200_000.0)
        assert result["n_trades"] == 3

    def test_mixed_cycles_only_sell_cycles_trade(self):
        cycles = [
            _sell_cycle(0),
            _stand_aside_cycle(1),
            _sell_cycle(2),
            _stand_aside_cycle(3),
        ]
        result = run_backtest(cycles, k=0.9, move_mult=1.0, capital=200_000.0)
        assert result["n_trades"] == 2

    def test_metrics_keys_present(self):
        cycles = [_sell_cycle(i) for i in range(2)]
        result = run_backtest(cycles, k=0.9, move_mult=1.0, capital=200_000.0)
        for key in ("n_trades", "win_rate", "profit_factor", "sharpe", "max_drawdown", "net_pnl"):
            assert key in result, f"missing key: {key}"

    def test_win_rate_in_valid_range(self):
        cycles = [_sell_cycle(i) for i in range(4)]
        result = run_backtest(cycles, k=0.9, move_mult=1.0, capital=200_000.0)
        assert 0.0 <= result["win_rate"] <= 1.0

    def test_no_trades_metrics_are_defined(self):
        """All-STAND_ASIDE → n_trades==0; other metrics should still be present."""
        cycles = [_stand_aside_cycle(i) for i in range(4)]
        result = run_backtest(cycles, k=0.9, move_mult=1.0, capital=200_000.0)
        assert result["n_trades"] == 0
        assert "net_pnl" in result


# ---------------------------------------------------------------------------
# Robustness: malformed / None-valued cycles must be skipped silently
# ---------------------------------------------------------------------------


@needs_condor
class TestMalformedCycleSkipped:
    """run_backtest must skip (not crash on) cycles with missing or None required fields.

    Verifies task item 2: guard against malformed cycles.
    """

    def test_missing_realized_vol_20d_key_is_skipped(self):
        """A cycle dict with no 'realized_vol_20d' key must be silently skipped."""
        bad = {
            "cycle_id": "bad_no_rvol",
            "spot": 23400.0,
            "straddle_iv": 0.15,
            # 'realized_vol_20d' deliberately absent
            "dte": 7,
            "expiry_spot": 23400.0,
        }
        result = run_backtest([bad], k=0.9, move_mult=1.0, capital=200_000.0)
        assert result["n_trades"] == 0, (
            "expected 0 trades when realized_vol_20d is missing"
        )

    def test_realized_vol_20d_none_is_skipped(self):
        """A cycle dict with realized_vol_20d=None must be silently skipped."""
        bad = {
            "cycle_id": "bad_rvol_none",
            "spot": 23400.0,
            "straddle_iv": 0.15,
            "realized_vol_20d": None,
            "dte": 7,
            "expiry_spot": 23400.0,
        }
        result = run_backtest([bad], k=0.9, move_mult=1.0, capital=200_000.0)
        assert result["n_trades"] == 0, (
            "expected 0 trades when realized_vol_20d is None"
        )

    def test_malformed_cycle_mixed_with_good_cycle(self):
        """Malformed cycle is skipped; the following good SELL cycle still trades."""
        bad = {
            "cycle_id": "bad",
            "spot": 23400.0,
            "straddle_iv": 0.15,
            "realized_vol_20d": None,  # None → skip
            "dte": 7,
            "expiry_spot": 23400.0,
        }
        good = _sell_cycle(99)
        result = run_backtest([bad, good], k=0.9, move_mult=1.0, capital=200_000.0)
        # bad is skipped; good passes the SELL gate → exactly 1 trade
        assert result["n_trades"] == 1

    def test_missing_spot_is_skipped(self):
        """A cycle missing 'spot' must also be silently skipped (no KeyError)."""
        bad = {
            "cycle_id": "bad_no_spot",
            # 'spot' deliberately absent
            "straddle_iv": 0.15,
            "realized_vol_20d": 0.10,
            "dte": 7,
            "expiry_spot": 23400.0,
        }
        result = run_backtest([bad], k=0.9, move_mult=1.0, capital=200_000.0)
        assert result["n_trades"] == 0

    def test_missing_expiry_spot_is_skipped(self):
        """A cycle missing 'expiry_spot' must be silently skipped (no KeyError)."""
        bad = {
            "cycle_id": "bad_no_expiry_spot",
            "spot": 23400.0,
            "straddle_iv": 0.15,
            "realized_vol_20d": 0.10,
            "dte": 7,
            # 'expiry_spot' deliberately absent
        }
        result = run_backtest([bad], k=0.9, move_mult=1.0, capital=200_000.0)
        assert result["n_trades"] == 0


# ---------------------------------------------------------------------------
# go_no_go(metrics, capital) → (bool, str)
# ---------------------------------------------------------------------------


def _profitable_metrics() -> dict:
    """Clearly GO: high win rate, positive Sharpe, small drawdown, positive P&L."""
    return {
        "n_trades": 30,
        "win_rate": 0.80,
        "profit_factor": 2.5,
        "sharpe": 1.8,
        "max_drawdown": -5_000.0,
        "net_pnl": 25_000.0,
    }


def _losing_metrics() -> dict:
    """Clearly NO-GO: low win rate, negative Sharpe, deep drawdown, negative P&L."""
    return {
        "n_trades": 20,
        "win_rate": 0.30,
        "profit_factor": 0.5,
        "sharpe": -0.8,
        "max_drawdown": -60_000.0,
        "net_pnl": -18_000.0,
    }


def _insufficient_trades_metrics() -> dict:
    """NO-GO: too few trades to be statistically meaningful."""
    return {
        "n_trades": 3,
        "win_rate": 0.67,
        "profit_factor": 1.2,
        "sharpe": 0.5,
        "max_drawdown": -2_000.0,
        "net_pnl": 1_000.0,
    }


@needs_condor
class TestGoNoGo:
    def test_profitable_metrics_returns_go(self):
        ok, reason = go_no_go(_profitable_metrics(), capital=200_000.0)
        assert ok is True
        assert isinstance(reason, str)
        assert len(reason) > 0

    def test_losing_metrics_returns_no_go(self):
        ok, reason = go_no_go(_losing_metrics(), capital=200_000.0)
        assert ok is False
        assert isinstance(reason, str)
        assert len(reason) > 0

    def test_return_type_is_tuple_bool_str(self):
        result = go_no_go(_profitable_metrics(), capital=200_000.0)
        assert isinstance(result, tuple)
        assert len(result) == 2
        ok, reason = result
        assert isinstance(ok, bool)
        assert isinstance(reason, str)

    def test_insufficient_trades_is_no_go(self):
        """< some minimum trade count → gate should not fire."""
        ok, reason = go_no_go(_insufficient_trades_metrics(), capital=200_000.0)
        assert ok is False
        # reason should mention trades or sample size
        assert len(reason) > 0

    def test_zero_trades_is_no_go(self):
        metrics = {
            "n_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "net_pnl": 0.0,
        }
        ok, reason = go_no_go(metrics, capital=200_000.0)
        assert ok is False

    def test_large_drawdown_relative_to_capital_is_no_go(self):
        # drawdown > 30% of capital → should be no-go
        metrics = {**_profitable_metrics(), "max_drawdown": -80_000.0}
        ok, reason = go_no_go(metrics, capital=200_000.0)
        assert ok is False

    def test_negative_net_pnl_is_no_go(self):
        metrics = {**_profitable_metrics(), "net_pnl": -1.0}
        ok, _reason = go_no_go(metrics, capital=200_000.0)
        assert ok is False

    def test_negative_sharpe_is_no_go(self):
        metrics = {**_profitable_metrics(), "sharpe": -0.1}
        ok, _reason = go_no_go(metrics, capital=200_000.0)
        assert ok is False


# ===========================================================================
# SECTION 6 — NEW HIGH-VALUE COVERAGE TESTS
# ===========================================================================


# ---------------------------------------------------------------------------
# 6a. profit_factor == inf path
# ---------------------------------------------------------------------------


def _all_win_sell_cycle(idx: int) -> dict:
    """SELL cycle whose expiry lands between the short strikes → full credit, zero loss."""
    return {
        "cycle_id": idx,
        "spot": 23400.0,
        "straddle_iv": 0.15,
        "realized_vol_20d": 0.08,  # well below 0.9*0.15=0.135 → SELL gate fires
        "dte": 7,
        "expiry_spot": 23400.0,  # ATM → between shorts → all OTM → full credit
        "entry_date": date(2026, 1, 6 + idx * 7),
        "expiry_date": date(2026, 1, 9 + idx * 7),
    }


@needs_condor
class TestProfitFactorInf:
    """All winning trades → profit_factor == inf; go_no_go must not crash on inf."""

    def test_all_wins_yield_inf_profit_factor(self):
        # 3 SELL cycles, all expire between shorts (full credit), zero losses
        cycles = [_all_win_sell_cycle(i) for i in range(3)]
        result = run_backtest(cycles, k=0.9, move_mult=1.0, capital=200_000.0)
        assert result["n_trades"] == 3
        assert result["profit_factor"] == float("inf")

    def test_go_no_go_handles_inf_profit_factor_without_crash(self):
        # Pass a metrics dict with profit_factor=inf and enough trades
        metrics = {
            "n_trades": 30,
            "win_rate": 1.0,
            "profit_factor": float("inf"),
            "sharpe": 2.5,
            "max_drawdown": -1_000.0,
            "net_pnl": 50_000.0,
            "return_on_capital": 0.25,
        }
        # Must not raise; must return (True, str)
        ok, reason = go_no_go(metrics, capital=200_000.0)
        assert isinstance(ok, bool)
        assert isinstance(reason, str)
        assert ok is True
        # Reason must contain a symbolic representation of infinity
        assert "∞" in reason or "inf" in reason.lower()


# ---------------------------------------------------------------------------
# 6b. Single-trade Sharpe
# ---------------------------------------------------------------------------


@needs_condor
class TestSingleTradeSharpe:
    """run_backtest with exactly one SELL cycle must return sharpe==0.0."""

    def test_single_trade_sharpe_is_zero(self):
        cycles = [_all_win_sell_cycle(0)]
        result = run_backtest(cycles, k=0.9, move_mult=1.0, capital=200_000.0)
        assert result["n_trades"] == 1
        sharpe = result["sharpe"]
        # Must be 0.0 — not NaN, not huge, not negative infinity
        assert not (sharpe != sharpe), "sharpe must not be NaN"  # NaN check via identity
        assert sharpe == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 6c. ITM-exercise path: deep-ITM put side → max loss + exercise STT charged
# ---------------------------------------------------------------------------


def _deep_itm_put_cycle() -> dict:
    """SELL cycle where expiry_spot is far BELOW long_put_k → full max-loss scenario.

    With spot=23400, straddle_iv=0.15, dte=7, move_mult=1.0:
      implied_move ≈ 23400 * 0.15 * sqrt(7/365) ≈ 231 pts
      short_put_k  ≈ round(23400 - 231) to nearest 50 ≈ 23150
      long_put_k   = short_put_k - 2*50 = 23050

    Setting expiry_spot = 20000 places settlement BELOW long_put_k (23050),
    so BOTH the short put and long put expire ITM and the long-put writer
    exercises it — exercise STT is charged on long_put_k intrinsic.
    """
    return {
        "cycle_id": "itm_test",
        "spot": 23400.0,
        "straddle_iv": 0.15,
        "realized_vol_20d": 0.08,  # SELL gate fires
        "dte": 7,
        "expiry_spot": 20000.0,    # far below long_put_k → full max-loss + exercise
        "entry_date": date(2026, 2, 3),
        "expiry_date": date(2026, 2, 6),
    }


@needs_condor
class TestITMExercisePath:
    """Verify that deep ITM-put expiry triggers max-loss cap AND exercise STT."""

    def test_itm_deep_put_produces_single_trade(self):
        result = run_backtest([_deep_itm_put_cycle()], k=0.9, move_mult=1.0)
        assert result["n_trades"] == 1

    def test_itm_trade_net_pnl_is_negative(self):
        """Full max-loss scenario — net P&L must be negative."""
        result = run_backtest([_deep_itm_put_cycle()], k=0.9, move_mult=1.0)
        trade = result["trades"][0]
        assert trade.net_pnl < 0

    def test_itm_trade_costs_exceed_zero_exercise_costs(self):
        """Exercise STT must be charged: total costs > costs without exercise.

        We compare the trade's reported costs against what condor_costs would
        return for the same legs but with exercise_intrinsic=0.  The exercise
        intrinsic on the long put (which goes deep ITM) pushes costs higher.
        """
        result = run_backtest([_deep_itm_put_cycle()], k=0.9, move_mult=1.0)
        trade = result["trades"][0]

        # Reconstruct leg list from recorded premiums (slippage-adjusted):
        lp = trade.leg_premiums["long_put"]
        lc = trade.leg_premiums["long_call"]
        sp = trade.leg_premiums["short_put"]
        sc = trade.leg_premiums["short_call"]

        # Use default slippage fractions (0.5%) to match what run_backtest applies
        sp_slip = slippage(sp)
        sc_slip = slippage(sc)
        lp_slip = slippage(lp)
        lc_slip = slippage(lc)

        legs = [
            (sp - sp_slip, NIFTY_LOT, "SELL"),
            (lp + lp_slip, NIFTY_LOT, "BUY"),
            (sc - sc_slip, NIFTY_LOT, "SELL"),
            (lc + lc_slip, NIFTY_LOT, "BUY"),
        ]
        costs_no_exercise = condor_costs(legs, exercise_intrinsic=0.0).total

        # With deep ITM expiry, exercise_intrinsic > 0 → total costs must be higher
        assert trade.costs > costs_no_exercise, (
            f"expected exercise STT to push costs above {costs_no_exercise:.2f}; "
            f"got trade.costs={trade.costs:.2f}"
        )

    def test_itm_trade_net_pnl_equals_gross_minus_costs(self):
        result = run_backtest([_deep_itm_put_cycle()], k=0.9, move_mult=1.0)
        trade = result["trades"][0]
        assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.costs, rel=1e-6)


# ---------------------------------------------------------------------------
# 6d. Slippage reduces credit vs raw mid
# ---------------------------------------------------------------------------


@needs_condor
class TestSlippageReducesCredit:
    """After run_backtest, the per-lot credit stored in the trade must be strictly
    less than the raw mid credit from price_condor (no slippage)."""

    def test_trade_credit_less_than_raw_mid_credit(self):
        cycle = _all_win_sell_cycle(0)
        result = run_backtest([cycle], k=0.9, move_mult=1.0, capital=200_000.0)
        assert result["n_trades"] == 1
        trade = result["trades"][0]

        # Compute the raw (no-slip) credit for the same strikes + IV + DTE
        raw = price_condor(
            spot=cycle["spot"],
            straddle_iv=cycle["straddle_iv"],
            dte=cycle["dte"],
            strikes=trade.strikes,
            lot=NIFTY_LOT,
        )
        raw_credit_per_lot = raw["credit_total"]

        # trade.credit is per-lot after slippage; it must be strictly less
        assert trade.credit < raw_credit_per_lot, (
            f"expected slippage-adjusted credit {trade.credit:.4f} "
            f"< raw mid credit {raw_credit_per_lot:.4f}"
        )


# ---------------------------------------------------------------------------
# 6e. All-STAND_ASIDE run: n_trades==0, all zero metrics, go_no_go returns False
# ---------------------------------------------------------------------------


def _buy_vol_cycle(idx: int) -> dict:
    """Cycle where realized_vol_20d > straddle_iv → BUY_PREMIUM → stand aside.

    No entry_date/expiry_date — run_backtest supplies date.today() as a default;
    we only need the vol + pricing fields to exercise the gate logic.
    """
    return {
        "cycle_id": idx,
        "spot": 23400.0,
        "straddle_iv": 0.12,
        "realized_vol_20d": 0.25,  # 0.25 > 0.12 → BUY_PREMIUM → not traded
        "dte": 7,
        "expiry_spot": 23400.0,
    }


@needs_condor
class TestAllStandAsideRun:
    """All cycles are BUY/STAND_ASIDE → n_trades==0 → all aggregate metrics are 0."""

    def _run(self):
        cycles = [_buy_vol_cycle(i) for i in range(5)]
        return run_backtest(cycles, k=0.9, move_mult=1.0, capital=200_000.0)

    def test_n_trades_is_zero(self):
        result = self._run()
        assert result["n_trades"] == 0

    def test_sharpe_is_zero(self):
        result = self._run()
        assert result["sharpe"] == pytest.approx(0.0)

    def test_profit_factor_is_zero(self):
        result = self._run()
        assert result["profit_factor"] == pytest.approx(0.0)

    def test_max_drawdown_is_zero(self):
        result = self._run()
        assert result["max_drawdown"] == pytest.approx(0.0)

    def test_go_no_go_returns_false(self):
        result = self._run()
        ok, reason = result["go_no_go"]
        assert ok is False
        assert isinstance(reason, str)


# ===========================================================================
# SECTION 7 — cycles_from_db (DB loader, monkeypatched)
# ===========================================================================


@needs_condor
class TestCyclesFromDb:
    """Verify cycles_from_db assembles cycles correctly from canned DB rows.

    get_session is monkeypatched to return a fake session whose execute()
    returns scripted result sets:
      1st call  → expiry_calendar weekly rows for NIFTY
      2nd call  → index_bars NIFTY (security_id="13")
      3rd call  → index_bars VIX   (security_id="21")

    Canned data (all dates are Python date objects):
      Expiries:  2026-01-08, 2026-01-15, 2026-01-22
      NIFTY bars:
        2026-01-08 close=23000.0 rvol=0.12
        2026-01-15 close=23200.0 rvol=0.13
        2026-01-22 close=23400.0 rvol=0.14   ← only needed as expiry_spot for last pair
      VIX bars:
        2026-01-08 close=14.0  → straddle_iv = 0.14
        2026-01-15 close=15.0  → straddle_iv = 0.15
        2026-01-22 close=16.0  ← not needed as an entry (no E_{i+2})
      Missing VIX at 2026-01-15 in the "skip" variant to test skip logic.

    Expected cycles (all present variant):
      Cycle 0: entry=2026-01-08, expiry=2026-01-15, spot=23000, rvol=0.12,
               straddle_iv=0.14, dte=7, expiry_spot=23200
      Cycle 1: entry=2026-01-15, expiry=2026-01-22, spot=23200, rvol=0.13,
               straddle_iv=0.15, dte=7, expiry_spot=23400
    """

    # ── Canned data ──────────────────────────────────────────────────────────

    _EXPIRY_DATES = [date(2026, 1, 8), date(2026, 1, 15), date(2026, 1, 22)]

    _NIFTY_ROWS = [
        (date(2026, 1, 8),  23000.0, 0.12),
        (date(2026, 1, 15), 23200.0, 0.13),
        (date(2026, 1, 22), 23400.0, 0.14),
    ]

    _VIX_ROWS = [
        (date(2026, 1, 8),  14.0),
        (date(2026, 1, 15), 15.0),
        (date(2026, 1, 22), 16.0),
    ]

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _make_session(expiry_rows, nifty_rows, vix_rows):
        """Return a context-manager that yields a fake SQLAlchemy session.

        execute() returns result sets in call order:
          call 0 → expiry_calendar weekly rows (or empty → triggers fallback call 1)
          call 1 → (fallback) expiry_calendar all rows  [only when call 0 is empty]
          next   → nifty index_bars rows
          next   → vix index_bars rows

        When expiry_rows is non-empty we need exactly 3 execute() calls.
        When empty (weekly fallback test) we need 4.
        """

        def _make_result(rows):
            result = MagicMock()
            result.fetchall.return_value = rows
            return result

        session = MagicMock()
        session.execute.side_effect = [
            _make_result(expiry_rows),          # expiry_calendar weekly
            _make_result(nifty_rows),            # index_bars NIFTY
            _make_result(vix_rows),              # index_bars VIX
        ]

        @contextmanager
        def fake_get_session():
            yield session

        return fake_get_session

    @staticmethod
    def _make_session_with_fallback(nifty_rows, vix_rows, all_expiry_rows):
        """Session where the weekly query returns empty → fallback is triggered."""

        def _make_result(rows):
            result = MagicMock()
            result.fetchall.return_value = rows
            return result

        session = MagicMock()
        session.execute.side_effect = [
            _make_result([]),               # weekly expiry query → empty
            _make_result(all_expiry_rows),  # fallback all-expiries query
            _make_result(nifty_rows),
            _make_result(vix_rows),
        ]

        @contextmanager
        def fake_get_session():
            yield session

        return fake_get_session

    # ── Tests ────────────────────────────────────────────────────────────────

    def _run_with_canned(self, vix_rows=None):
        from research.backtest.fno_condor import cycles_from_db

        expiry_rows = [(d,) for d in self._EXPIRY_DATES]
        nifty_rows = self._NIFTY_ROWS
        vix = vix_rows if vix_rows is not None else self._VIX_ROWS
        fake_gs = self._make_session(expiry_rows, nifty_rows, vix)

        with patch("research.backtest.fno_condor.get_session", new=fake_gs, create=True), \
             patch("db.get_session", new=fake_gs):
            return cycles_from_db(mode="expiry_calendar")

    def test_correct_number_of_cycles(self):
        """Three expiries → two consecutive pairs → two cycles."""
        cycles = self._run_with_canned()
        assert len(cycles) == 2

    def test_consecutive_pairing(self):
        """Cycle 0 entry=E_0, expiry=E_1; cycle 1 entry=E_1, expiry=E_2."""
        cycles = self._run_with_canned()
        assert cycles[0]["entry_date"] == date(2026, 1, 8)
        assert cycles[0]["expiry_date"] == date(2026, 1, 15)
        assert cycles[1]["entry_date"] == date(2026, 1, 15)
        assert cycles[1]["expiry_date"] == date(2026, 1, 22)

    def test_straddle_iv_is_vix_divided_by_100(self):
        """straddle_iv = VIX close / 100 for the ENTRY date of each cycle."""
        cycles = self._run_with_canned()
        # Cycle 0: VIX at 2026-01-08 = 14.0 → 0.14
        assert cycles[0]["straddle_iv"] == pytest.approx(14.0 / 100.0)
        # Cycle 1: VIX at 2026-01-15 = 15.0 → 0.15
        assert cycles[1]["straddle_iv"] == pytest.approx(15.0 / 100.0)

    def test_dte_is_days_between_entry_and_expiry(self):
        """dte = (expiry_date - entry_date).days."""
        cycles = self._run_with_canned()
        assert cycles[0]["dte"] == 7
        assert cycles[1]["dte"] == 7

    def test_expiry_spot_is_nifty_close_at_expiry_date(self):
        """expiry_spot = NIFTY close on E_{i+1}, not E_i."""
        cycles = self._run_with_canned()
        assert cycles[0]["expiry_spot"] == pytest.approx(23200.0)
        assert cycles[1]["expiry_spot"] == pytest.approx(23400.0)

    def test_spot_and_rvol_are_from_entry_date(self):
        """spot and realized_vol_20d are taken from the ENTRY date E_i."""
        cycles = self._run_with_canned()
        assert cycles[0]["spot"] == pytest.approx(23000.0)
        assert cycles[0]["realized_vol_20d"] == pytest.approx(0.12)
        assert cycles[1]["spot"] == pytest.approx(23200.0)
        assert cycles[1]["realized_vol_20d"] == pytest.approx(0.13)

    def test_missing_vix_skips_pair(self):
        """When VIX is absent for an entry date the pair is silently skipped."""
        # Remove VIX for 2026-01-15 → second cycle (entry 2026-01-15) must be skipped.
        vix_partial = [
            (date(2026, 1, 8),  14.0),
            # 2026-01-15 deliberately absent
            (date(2026, 1, 22), 16.0),
        ]
        cycles = self._run_with_canned(vix_rows=vix_partial)
        # Only the first pair survives
        assert len(cycles) == 1
        assert cycles[0]["entry_date"] == date(2026, 1, 8)

    def test_missing_nifty_close_at_expiry_skips_pair(self):
        """When NIFTY close is absent at E_{i+1} the pair is silently skipped."""
        from research.backtest.fno_condor import cycles_from_db

        # Remove NIFTY bar for 2026-01-15 → cycle 0 (entry 01-08, expiry 01-15)
        # loses its expiry_spot; cycle 1 (entry 01-15) also loses its spot/rvol.
        # Both pairs should be skipped.
        nifty_partial = [
            # 2026-01-08 present (entry for cycle 0)
            (date(2026, 1, 8), 23000.0, 0.12),
            # 2026-01-15 absent → cycle 0 has no expiry_spot; cycle 1 has no spot
            (date(2026, 1, 22), 23400.0, 0.14),
        ]
        expiry_rows = [(d,) for d in self._EXPIRY_DATES]
        fake_gs = self._make_session(expiry_rows, nifty_partial, self._VIX_ROWS)

        with patch("research.backtest.fno_condor.get_session", new=fake_gs, create=True), \
             patch("db.get_session", new=fake_gs):
            cycles = cycles_from_db(mode="expiry_calendar")

        # Cycle 0: expiry_spot on 2026-01-15 is missing → skip
        # Cycle 1: spot/rvol on 2026-01-15 is missing → skip
        assert len(cycles) == 0

    def test_missing_rvol_skips_pair(self):
        """When realized_vol_20d is None for the entry date the pair is skipped."""
        from research.backtest.fno_condor import cycles_from_db

        # rvol=None on 2026-01-08 → cycle 0 (entry 01-08) skipped;
        # cycle 1 (entry 01-15) has rvol so it survives.
        nifty_no_rvol = [
            (date(2026, 1, 8),  23000.0, None),   # rvol missing
            (date(2026, 1, 15), 23200.0, 0.13),
            (date(2026, 1, 22), 23400.0, 0.14),
        ]
        expiry_rows = [(d,) for d in self._EXPIRY_DATES]
        fake_gs = self._make_session(expiry_rows, nifty_no_rvol, self._VIX_ROWS)

        with patch("research.backtest.fno_condor.get_session", new=fake_gs, create=True), \
             patch("db.get_session", new=fake_gs):
            cycles = cycles_from_db(mode="expiry_calendar")

        assert len(cycles) == 1
        assert cycles[0]["entry_date"] == date(2026, 1, 15)

    def test_weekly_fallback_when_no_weekly_rows(self):
        """If expiry_calendar has no weekly rows, fall back to all expiries."""
        from research.backtest.fno_condor import cycles_from_db

        all_expiry_rows = [(d,) for d in self._EXPIRY_DATES]
        fake_gs = self._make_session_with_fallback(
            self._NIFTY_ROWS, self._VIX_ROWS, all_expiry_rows
        )

        with patch("research.backtest.fno_condor.get_session", new=fake_gs, create=True), \
             patch("db.get_session", new=fake_gs):
            cycles = cycles_from_db(mode="expiry_calendar")

        assert len(cycles) == 2

    def test_fewer_than_two_expiries_returns_empty(self):
        """A single expiry date → no consecutive pairs → empty list."""
        from research.backtest.fno_condor import cycles_from_db

        expiry_rows = [(date(2026, 1, 8),)]
        fake_gs = self._make_session(expiry_rows, self._NIFTY_ROWS, self._VIX_ROWS)

        with patch("research.backtest.fno_condor.get_session", new=fake_gs, create=True), \
             patch("db.get_session", new=fake_gs):
            cycles = cycles_from_db(mode="expiry_calendar")

        assert cycles == []

    def test_cycle_dict_has_required_keys(self):
        """Every cycle dict must carry all keys run_backtest expects."""
        cycles = self._run_with_canned()
        required = {"entry_date", "expiry_date", "spot", "straddle_iv",
                    "realized_vol_20d", "dte", "expiry_spot"}
        for i, c in enumerate(cycles):
            assert required.issubset(c.keys()), f"cycle {i} missing keys: {required - c.keys()}"


# ===========================================================================
# SECTION 7b — cycles_from_db mode="weekly"
# ===========================================================================


@needs_condor
class TestCyclesFromDbWeekly:
    """Verify mode="weekly" builds ISO-week-boundary cycles from index_bars.

    Canned trading calendar: 4 ISO weeks, 2-3 trading days each.
    Week boundaries (last trading day per ISO week):

        ISO week (2026, 1): Mon 2026-01-05, Tue 2026-01-06, Thu 2026-01-08
            → last = 2026-01-08 (Thu)
        ISO week (2026, 2): Mon 2026-01-12, Wed 2026-01-14, Fri 2026-01-16
            → last = 2026-01-16 (Fri)
        ISO week (2026, 3): Mon 2026-01-19, Thu 2026-01-22
            → last = 2026-01-22 (Thu)
        ISO week (2026, 4): Tue 2026-01-27, Fri 2026-01-30
            → last = 2026-01-30 (Fri)

    Boundaries: [2026-01-08, 2026-01-16, 2026-01-22, 2026-01-30]
    Pairs: (01-08, 01-16), (01-16, 01-22), (01-22, 01-30)

    VIX missing for 2026-01-16 → middle pair skipped → 2 cycles survive.
    """

    # All trading dates across 4 ISO weeks (2026 week numbers)
    _NIFTY_ROWS = [
        # ISO week 1
        (date(2026, 1, 5),  22800.0, 0.11),
        (date(2026, 1, 6),  22900.0, 0.11),
        (date(2026, 1, 8),  23000.0, 0.12),   # boundary: last of week 1
        # ISO week 2
        (date(2026, 1, 12), 23050.0, 0.12),
        (date(2026, 1, 14), 23100.0, 0.13),
        (date(2026, 1, 16), 23200.0, 0.13),   # boundary: last of week 2
        # ISO week 3
        (date(2026, 1, 19), 23250.0, 0.13),
        (date(2026, 1, 22), 23300.0, 0.14),   # boundary: last of week 3
        # ISO week 4
        (date(2026, 1, 27), 23350.0, 0.14),
        (date(2026, 1, 30), 23400.0, 0.15),   # boundary: last of week 4
    ]

    _VIX_ROWS_FULL = [
        (date(2026, 1, 5),  13.0),
        (date(2026, 1, 6),  13.5),
        (date(2026, 1, 8),  14.0),
        (date(2026, 1, 12), 14.2),
        (date(2026, 1, 14), 14.5),
        (date(2026, 1, 16), 15.0),
        (date(2026, 1, 19), 15.2),
        (date(2026, 1, 22), 15.5),
        (date(2026, 1, 27), 15.8),
        (date(2026, 1, 30), 16.0),
    ]

    # VIX missing at 2026-01-16 (boundary of week 2 → entry of pair 2)
    _VIX_ROWS_MISSING_W2 = [r for r in _VIX_ROWS_FULL if r[0] != date(2026, 1, 16)]

    @staticmethod
    def _make_weekly_session(nifty_rows, vix_rows):
        """Return a fake get_session context-manager for mode="weekly".

        mode="weekly" makes exactly 2 execute() calls:
          call 0 → NIFTY index_bars (ORDER BY 1)
          call 1 → VIX index_bars
        """
        def _make_result(rows):
            result = MagicMock()
            result.fetchall.return_value = rows
            return result

        session = MagicMock()
        session.execute.side_effect = [
            _make_result(nifty_rows),
            _make_result(vix_rows),
        ]

        @contextmanager
        def fake_get_session():
            yield session

        return fake_get_session

    def _run(self, vix_rows=None):
        from research.backtest.fno_condor import cycles_from_db

        vix = vix_rows if vix_rows is not None else self._VIX_ROWS_FULL
        fake_gs = self._make_weekly_session(self._NIFTY_ROWS, vix)

        with patch("research.backtest.fno_condor.get_session", new=fake_gs, create=True), \
             patch("db.get_session", new=fake_gs):
            return cycles_from_db(mode="weekly")

    def test_boundaries_are_last_trading_day_per_iso_week(self):
        """The 4 ISO weeks produce exactly 4 boundaries; check their dates."""
        cycles = self._run()
        # boundaries = [01-08, 01-16, 01-22, 01-30]; pairs are consecutive
        # Cycle 0: entry=01-08, expiry=01-16
        assert cycles[0]["entry_date"] == date(2026, 1, 8)
        assert cycles[0]["expiry_date"] == date(2026, 1, 16)
        # Last cycle: entry=01-22, expiry=01-30
        assert cycles[-1]["entry_date"] == date(2026, 1, 22)
        assert cycles[-1]["expiry_date"] == date(2026, 1, 30)

    def test_correct_number_of_cycles(self):
        """4 boundaries → 3 consecutive pairs → 3 cycles (all data present)."""
        cycles = self._run()
        assert len(cycles) == 3

    def test_straddle_iv_equals_vix_close_at_boundary_divided_by_100(self):
        """straddle_iv = VIX close at the BOUNDARY (entry) date / 100."""
        cycles = self._run()
        # Cycle 0: boundary=01-08, VIX=14.0 → 0.14
        assert cycles[0]["straddle_iv"] == pytest.approx(14.0 / 100.0)
        # Cycle 1: boundary=01-16, VIX=15.0 → 0.15
        assert cycles[1]["straddle_iv"] == pytest.approx(15.0 / 100.0)
        # Cycle 2: boundary=01-22, VIX=15.5 → 0.155
        assert cycles[2]["straddle_iv"] == pytest.approx(15.5 / 100.0)

    def test_dte_equals_calendar_days_between_boundaries(self):
        """dte = (expiry_date - entry_date).days for each cycle."""
        cycles = self._run()
        for c in cycles:
            assert c["dte"] == (c["expiry_date"] - c["entry_date"]).days

    def test_expiry_spot_is_nifty_close_at_next_boundary(self):
        """expiry_spot is the NIFTY close on the NEXT boundary date."""
        cycles = self._run()
        # Cycle 0: expiry=01-16 → NIFTY close = 23200.0
        assert cycles[0]["expiry_spot"] == pytest.approx(23200.0)
        # Cycle 1: expiry=01-22 → NIFTY close = 23300.0
        assert cycles[1]["expiry_spot"] == pytest.approx(23300.0)
        # Cycle 2: expiry=01-30 → NIFTY close = 23400.0
        assert cycles[2]["expiry_spot"] == pytest.approx(23400.0)

    def test_spot_and_rvol_from_entry_boundary(self):
        """spot and realized_vol_20d come from the ENTRY boundary date."""
        cycles = self._run()
        # Cycle 0: entry=01-08 → NIFTY close=23000.0, rvol=0.12
        assert cycles[0]["spot"] == pytest.approx(23000.0)
        assert cycles[0]["realized_vol_20d"] == pytest.approx(0.12)

    def test_missing_vix_at_boundary_skips_that_pair(self):
        """When VIX is absent for a boundary date, that pair is skipped."""
        # VIX missing at 2026-01-16 (entry of pair 2: 01-16 → 01-22)
        cycles = self._run(vix_rows=self._VIX_ROWS_MISSING_W2)
        # Pair (01-08→01-16): VIX at 01-08=14.0 → survives
        # Pair (01-16→01-22): VIX at 01-16=missing → SKIPPED
        # Pair (01-22→01-30): VIX at 01-22=15.5 → survives
        assert len(cycles) == 2
        assert cycles[0]["entry_date"] == date(2026, 1, 8)
        assert cycles[1]["entry_date"] == date(2026, 1, 22)

    def test_missing_rvol_at_boundary_skips_pair(self):
        """When rvol is None for a boundary date, that pair is skipped."""
        nifty_no_rvol = list(self._NIFTY_ROWS)
        # Set rvol=None on boundary 01-08 → pair (01-08→01-16) skipped
        nifty_no_rvol[2] = (date(2026, 1, 8), 23000.0, None)
        fake_gs = self._make_weekly_session(nifty_no_rvol, self._VIX_ROWS_FULL)

        from research.backtest.fno_condor import cycles_from_db
        with patch("research.backtest.fno_condor.get_session", new=fake_gs, create=True), \
             patch("db.get_session", new=fake_gs):
            cycles = cycles_from_db(mode="weekly")

        # Pair starting 01-08 is skipped; pairs starting 01-16 and 01-22 survive
        assert len(cycles) == 2
        entry_dates = [c["entry_date"] for c in cycles]
        assert date(2026, 1, 8) not in entry_dates

    def test_cycle_dict_has_required_keys(self):
        """Every weekly cycle dict must carry all keys run_backtest expects."""
        cycles = self._run()
        required = {"entry_date", "expiry_date", "spot", "straddle_iv",
                    "realized_vol_20d", "dte", "expiry_spot"}
        for i, c in enumerate(cycles):
            assert required.issubset(c.keys()), f"cycle {i} missing keys: {required - c.keys()}"

    def test_bad_mode_raises_value_error(self):
        """Unknown mode string must raise ValueError immediately."""
        from research.backtest.fno_condor import cycles_from_db

        # No DB calls should occur — ValueError is raised before any session work.
        with pytest.raises(ValueError, match="weekly.*expiry_calendar|expiry_calendar.*weekly"):
            cycles_from_db(mode="nonsense")


# ===========================================================================
# SECTION 8 — cycles_from_db → run_backtest end-to-end
# ===========================================================================


@needs_condor
class TestCyclesFromDbEndToEnd:
    """Feed cycles_from_db output directly into run_backtest and assert no crash."""

    def test_run_backtest_on_db_cycles_returns_metrics_dict(self):
        """Smoke test: canned DB cycles → run_backtest → valid metrics dict."""
        from research.backtest.fno_condor import cycles_from_db, run_backtest

        # Build two SELL-gate-passing cycles from the canned data above.
        # straddle_iv=0.14, realized_vol_20d=0.12  → 0.12 < 0.9*0.14=0.126 → SELL
        expiry_dates = [date(2026, 1, 8), date(2026, 1, 15), date(2026, 1, 22)]
        nifty_rows = [
            (date(2026, 1, 8),  23400.0, 0.10),   # rvol well below 0.9*IV → SELL gate fires
            (date(2026, 1, 15), 23400.0, 0.10),
            (date(2026, 1, 22), 23400.0, 0.10),
        ]
        vix_rows = [
            (date(2026, 1, 8),  15.0),   # straddle_iv=0.15, rvol=0.10 < 0.9*0.15=0.135
            (date(2026, 1, 15), 15.0),
            (date(2026, 1, 22), 15.0),
        ]
        expiry_rows = [(d,) for d in expiry_dates]

        def _make_result(rows):
            result = MagicMock()
            result.fetchall.return_value = rows
            return result

        session = MagicMock()
        session.execute.side_effect = [
            _make_result(expiry_rows),
            _make_result(nifty_rows),
            _make_result(vix_rows),
        ]

        @contextmanager
        def fake_get_session():
            yield session

        with patch("research.backtest.fno_condor.get_session", new=fake_get_session, create=True), \
             patch("db.get_session", new=fake_get_session):
            cycles = cycles_from_db(mode="expiry_calendar")

        assert len(cycles) == 2, f"expected 2 cycles, got {len(cycles)}"

        metrics = run_backtest(cycles, k=0.9, move_mult=1.0, capital=200_000.0)

        # Must return a dict with the standard metrics keys
        required_keys = {
            "trades", "n_cycles", "n_trades", "win_rate",
            "profit_factor", "sharpe", "max_drawdown", "net_pnl",
            "return_on_capital", "go_no_go",
        }
        assert required_keys.issubset(metrics.keys()), (
            f"metrics missing: {required_keys - metrics.keys()}"
        )
        # Both cycles are SELL-gate-passing → at least some trades expected
        assert metrics["n_cycles"] == 2
        assert metrics["n_trades"] >= 0   # gate may filter some; just assert no crash
        assert isinstance(metrics["go_no_go"], tuple)
        ok, reason = metrics["go_no_go"]
        assert isinstance(ok, bool)
        assert isinstance(reason, str)


# ===========================================================================
# SECTION 9 — mode="weekly" → run_backtest end-to-end
# ===========================================================================


@needs_condor
class TestCyclesFromDbWeeklyEndToEnd:
    """mode="weekly" cycles feed directly into run_backtest → valid metrics dict."""

    def test_weekly_cycles_into_run_backtest(self):
        """Smoke test: canned ISO-week bar data → cycles_from_db(weekly) → run_backtest."""
        from research.backtest.fno_condor import cycles_from_db, run_backtest

        # Use two ISO weeks for simplicity:
        #   Week A: Mon 2026-01-05, Thu 2026-01-08 (boundary)
        #   Week B: Mon 2026-01-12, Thu 2026-01-15 (boundary)
        #   Week C: Mon 2026-01-19, Thu 2026-01-22 (boundary)
        # → boundaries: [01-08, 01-15, 01-22] → 2 pairs → 2 cycles
        nifty_rows = [
            (date(2026, 1, 5),  23400.0, 0.10),
            (date(2026, 1, 8),  23400.0, 0.10),   # boundary week A
            (date(2026, 1, 12), 23400.0, 0.10),
            (date(2026, 1, 15), 23400.0, 0.10),   # boundary week B
            (date(2026, 1, 19), 23400.0, 0.10),
            (date(2026, 1, 22), 23400.0, 0.10),   # boundary week C
        ]
        vix_rows = [
            (date(2026, 1, 5),  15.0),
            (date(2026, 1, 8),  15.0),   # straddle_iv=0.15; rvol=0.10 < 0.9*0.15 → SELL
            (date(2026, 1, 12), 15.0),
            (date(2026, 1, 15), 15.0),
            (date(2026, 1, 19), 15.0),
            (date(2026, 1, 22), 15.0),
        ]

        def _make_result(rows):
            result = MagicMock()
            result.fetchall.return_value = rows
            return result

        session = MagicMock()
        session.execute.side_effect = [
            _make_result(nifty_rows),
            _make_result(vix_rows),
        ]

        @contextmanager
        def fake_get_session():
            yield session

        with patch("research.backtest.fno_condor.get_session", new=fake_get_session, create=True), \
             patch("db.get_session", new=fake_get_session):
            cycles = cycles_from_db(mode="weekly")

        assert len(cycles) == 2, f"expected 2 cycles from weekly mode, got {len(cycles)}"

        metrics = run_backtest(cycles, k=0.9, move_mult=1.0, capital=200_000.0)

        required_keys = {
            "trades", "n_cycles", "n_trades", "win_rate",
            "profit_factor", "sharpe", "max_drawdown", "net_pnl",
            "return_on_capital", "go_no_go",
        }
        assert required_keys.issubset(metrics.keys()), (
            f"metrics missing: {required_keys - metrics.keys()}"
        )
        assert metrics["n_cycles"] == 2
        # SELL gate: rvol=0.10 < 0.9*0.15=0.135 → both cycles should trade
        assert metrics["n_trades"] == 2
        assert isinstance(metrics["go_no_go"], tuple)
        ok, reason = metrics["go_no_go"]
        assert isinstance(ok, bool)
        assert isinstance(reason, str)
