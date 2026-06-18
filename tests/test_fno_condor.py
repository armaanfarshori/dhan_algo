"""Tests for research/backtest/fno_costs.py and research/backtest/fno_condor.py.

fno_condor.py is being written in parallel; if it is not yet importable the
COSTS and BLACK-76 suites still run in isolation (the condor import is deferred
into each test that needs it via a module-level try/except + pytest.importorskip).
"""

from __future__ import annotations

from datetime import date

import pytest

# ---------------------------------------------------------------------------
# fno_costs — always available (already written)
# ---------------------------------------------------------------------------
from research.backtest.fno_costs import (
    BROKERAGE_PER_ORDER,
    GST_PCT,
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
        assert leg_turnover(100.0, 75) == pytest.approx(7500.0)

    def test_zero_premium(self):
        assert leg_turnover(0.0, 75) == pytest.approx(0.0)

    def test_fractional_premium(self):
        assert leg_turnover(12.5, 75) == pytest.approx(937.5)


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

    Legs (NIFTY, 1 lot = 75 units):
        short put  (SELL)  premium=50  qty=75  → turnover = 3750
        short call (SELL)  premium=45  qty=75  → turnover = 3375
        long put   (BUY)   premium=20  qty=75  → turnover = 1500
        long call  (BUY)   premium=18  qty=75  → turnover = 1350

    Derived:
        sell_turnover = 3750 + 3375  = 7125
        buy_turnover  = 1500 + 1350  = 2850
        total_turnover = 9975
        brokerage     = 20 * 4       = 80.00
        STT           = 0.0015 * 7125 = 10.6875
        exchange_fee  = 0.0003503 * 9975 ≈ 3.4942
        sebi_fee      = 0.000001 * 9975 = 0.009975
        stamp_duty    = 0.00003 * 2850  = 0.0855
        gst           = 0.18 * (80 + 3.4942... + 0.009975...) ≈ 15.03
        total         ≈ 80 + 10.69 + 3.49 + 0.01 + 0.09 + 15.03
    """

    _LEGS = [
        (50.0, 75, "SELL"),  # short put
        (45.0, 75, "SELL"),  # short call
        (20.0, 75, "BUY"),  # long put
        (18.0, 75, "BUY"),  # long call
    ]

    def _costs(self, **kw):
        return condor_costs(self._LEGS, **kw)

    # --- brokerage -----------------------------------------------------------

    def test_brokerage_flat_per_leg(self):
        c = self._costs()
        assert c.brokerage == pytest.approx(BROKERAGE_PER_ORDER * len(self._LEGS))

    # --- STT — SELL side only ------------------------------------------------

    def test_stt_sell_legs_only(self):
        sell_turnover = leg_turnover(50.0, 75) + leg_turnover(45.0, 75)  # 7125
        expected_stt = OPTION_STT_SELL_PCT * sell_turnover  # 10.6875
        c = self._costs()
        # condor_costs rounds stt to 2 dp; allow abs tolerance of ₹0.01
        assert c.stt == pytest.approx(expected_stt, abs=0.01)

    def test_buy_only_legs_produce_zero_stt(self):
        buy_only = [
            (20.0, 75, "BUY"),
            (18.0, 75, "BUY"),
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
        sell_turnover = leg_turnover(50.0, 75) + leg_turnover(45.0, 75)
        c = self._costs()
        assert c.stt == pytest.approx(OPTION_STT_SELL_PCT * sell_turnover, abs=0.01)

    # --- GST — on brokerage + exchange + SEBI --------------------------------

    def test_gst_formula(self):
        c = self._costs()
        gst_base = c.brokerage + c.exchange_fee + c.sebi_fee
        assert c.gst == pytest.approx(GST_PCT * gst_base, rel=1e-4)

    # --- total == sum of components ------------------------------------------

    def test_total_equals_sum_of_components(self):
        c = self._costs()
        parts_sum = c.brokerage + c.stt + c.exchange_fee + c.sebi_fee + c.stamp_duty + c.gst
        assert c.total == pytest.approx(parts_sum, rel=1e-4)

    # --- edge: single SELL leg -----------------------------------------------

    def test_single_sell_leg(self):
        legs = [(100.0, 75, "SELL")]
        c = condor_costs(legs)
        assert c.brokerage == pytest.approx(BROKERAGE_PER_ORDER)
        assert c.stt == pytest.approx(OPTION_STT_SELL_PCT * leg_turnover(100.0, 75))
        assert c.stamp_duty == pytest.approx(0.0)  # no BUY leg

    # --- bad side raises ------------------------------------------------------

    def test_bad_side_raises(self):
        with pytest.raises(ValueError, match="BUY.*SELL"):
            condor_costs([(50.0, 75, "SHORT")])


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
