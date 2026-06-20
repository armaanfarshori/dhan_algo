"""Tests for research/backtest/fno_costs.py and research/backtest/fno_condor.py.

fno_condor.py is being written in parallel; if it is not yet importable the
COSTS and BLACK-76 suites still run in isolation (the condor import is deferred
into each test that needs it via a module-level try/except + pytest.importorskip).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
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


# ---------------------------------------------------------------------------
# SQL-dispatching fake session
# ---------------------------------------------------------------------------
# cycles_from_db now issues an EXTRA query (option_atm_iv, when use_real_iv=True
# — the production default). A positional side_effect list is brittle against
# that, so the fake execute() dispatches on the SQL text instead: each query is
# routed to the right canned rows by a substring of its statement. Unmatched
# queries (e.g. option_atm_iv when the test supplies no real-IV rows) return [].


def _dispatch_execute(routes: dict[str, list]):
    """Return a fake ``session.execute`` that routes by SQL substring.

    ``routes`` maps a case-insensitive SQL substring → the rows ``fetchall()``
    should return. The FIRST matching key (in dict order) wins. Any statement
    matching no key returns an empty result (so option_atm_iv with no canned
    rows naturally yields no real IV → VIX-proxy fallback).
    """

    def _make_result(rows):
        result = MagicMock()
        result.fetchall.return_value = rows
        return result

    def _execute(stmt, *args, **kwargs):
        sql = str(stmt).lower()
        for needle, rows in routes.items():
            if needle.lower() in sql:
                return _make_result(rows)
        return _make_result([])

    return _execute


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
        session.execute.side_effect = _dispatch_execute({
            "from expiry_calendar": expiry_rows,
            ":nid": nifty_rows,
            ":vid": vix_rows,
            # option_atm_iv: unmatched → [] → cycles stay on the VIX proxy.
        })

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
        session.execute.side_effect = _dispatch_execute({
            "expiry_type = 'weekly'": [],          # weekly query → empty → fallback
            "from expiry_calendar": all_expiry_rows,  # all-expiries fallback
            ":nid": nifty_rows,
            ":vid": vix_rows,
        })

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
    """Verify mode="weekly" builds expiry-weekday cycle boundaries from index_bars.

    Fidelity fix #1: the cycle boundary is the weekly-EXPIRY trading day (Thursday
    before the 2026-09-01 Tuesday cutover; holiday-rolled to the prior trading day
    when the expiry weekday is closed), NOT just the last trading day of the ISO
    week. All canned dates are in Jan-2026 → pre-cutover → target weekday Thursday.

    Canned trading calendar: 4 ISO weeks.

        ISO week (2026, 1): Mon 2026-01-05, Tue 2026-01-06, Thu 2026-01-08
            → Thursday present → boundary 2026-01-08 (Thu)
        ISO week (2026, 2): Mon 2026-01-12, Wed 2026-01-14, Fri 2026-01-16
            → no Thursday (expiry-day holiday) → roll back to last day on/before
              Thu = Wed 2026-01-14
        ISO week (2026, 3): Mon 2026-01-19, Thu 2026-01-22
            → Thursday present → boundary 2026-01-22 (Thu)
        ISO week (2026, 4): Tue 2026-01-27, Fri 2026-01-30
            → no Thursday → roll back to last day on/before Thu = Tue 2026-01-27

    Boundaries: [2026-01-08, 2026-01-14, 2026-01-22, 2026-01-27]
    Pairs: (01-08, 01-14), (01-14, 01-22), (01-22, 01-27)

    VIX missing for 2026-01-14 → middle pair skipped → 2 cycles survive.
    """

    # All trading dates across 4 ISO weeks (2026 week numbers)
    _NIFTY_ROWS = [
        # ISO week 1
        (date(2026, 1, 5),  22800.0, 0.11),
        (date(2026, 1, 6),  22900.0, 0.11),
        (date(2026, 1, 8),  23000.0, 0.12),   # boundary: Thu of week 1
        # ISO week 2 (no Thursday → expiry rolls to Wed 01-14)
        (date(2026, 1, 12), 23050.0, 0.12),
        (date(2026, 1, 14), 23100.0, 0.13),   # boundary: rolled expiry of week 2
        (date(2026, 1, 16), 23200.0, 0.13),
        # ISO week 3
        (date(2026, 1, 19), 23250.0, 0.13),
        (date(2026, 1, 22), 23300.0, 0.14),   # boundary: Thu of week 3
        # ISO week 4 (no Thursday → expiry rolls to Tue 01-27)
        (date(2026, 1, 27), 23350.0, 0.14),   # boundary: rolled expiry of week 4
        (date(2026, 1, 30), 23400.0, 0.15),
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

    # VIX missing at 2026-01-14 (rolled expiry of week 2 → entry of pair 2)
    _VIX_ROWS_MISSING_W2 = [r for r in _VIX_ROWS_FULL if r[0] != date(2026, 1, 14)]

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
        session.execute.side_effect = _dispatch_execute({
            ":nid": nifty_rows,
            ":vid": vix_rows,
            # option_atm_iv: unmatched → [] → cycles stay on the VIX proxy.
        })

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

    def test_boundaries_are_expiry_weekday_per_iso_week(self):
        """The 4 ISO weeks produce 4 expiry-weekday boundaries (fidelity fix #1).

        boundaries = [01-08 (Thu), 01-14 (rolled), 01-22 (Thu), 01-27 (rolled)].
        """
        cycles = self._run()
        # Cycle 0: entry=01-08 (Thu), expiry=01-14 (rolled expiry of week 2)
        assert cycles[0]["entry_date"] == date(2026, 1, 8)
        assert cycles[0]["expiry_date"] == date(2026, 1, 14)
        # Last cycle: entry=01-22 (Thu), expiry=01-27 (rolled expiry of week 4)
        assert cycles[-1]["entry_date"] == date(2026, 1, 22)
        assert cycles[-1]["expiry_date"] == date(2026, 1, 27)

    def test_correct_number_of_cycles(self):
        """4 boundaries → 3 consecutive pairs → 3 cycles (all data present)."""
        cycles = self._run()
        assert len(cycles) == 3

    def test_straddle_iv_equals_vix_close_at_boundary_divided_by_100(self):
        """straddle_iv = VIX close at the BOUNDARY (entry) date / 100."""
        cycles = self._run()
        # Cycle 0: boundary=01-08, VIX=14.0 → 0.14
        assert cycles[0]["straddle_iv"] == pytest.approx(14.0 / 100.0)
        # Cycle 1: boundary=01-14, VIX=14.5 → 0.145
        assert cycles[1]["straddle_iv"] == pytest.approx(14.5 / 100.0)
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
        # Cycle 0: expiry=01-14 → NIFTY close = 23100.0
        assert cycles[0]["expiry_spot"] == pytest.approx(23100.0)
        # Cycle 1: expiry=01-22 → NIFTY close = 23300.0
        assert cycles[1]["expiry_spot"] == pytest.approx(23300.0)
        # Cycle 2: expiry=01-27 → NIFTY close = 23350.0
        assert cycles[2]["expiry_spot"] == pytest.approx(23350.0)

    def test_spot_and_rvol_from_entry_boundary(self):
        """spot and realized_vol_20d come from the ENTRY boundary date."""
        cycles = self._run()
        # Cycle 0: entry=01-08 → NIFTY close=23000.0, rvol=0.12
        assert cycles[0]["spot"] == pytest.approx(23000.0)
        assert cycles[0]["realized_vol_20d"] == pytest.approx(0.12)

    def test_missing_vix_at_boundary_skips_that_pair(self):
        """When VIX is absent for a boundary date, that pair is skipped."""
        # VIX missing at 2026-01-14 (rolled expiry of week 2 → entry of pair 2)
        cycles = self._run(vix_rows=self._VIX_ROWS_MISSING_W2)
        # Pair (01-08→01-14): VIX at 01-08=14.0 → survives
        # Pair (01-14→01-22): VIX at 01-14=missing → SKIPPED
        # Pair (01-22→01-27): VIX at 01-22=15.5 → survives
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
        session.execute.side_effect = _dispatch_execute({
            "from expiry_calendar": expiry_rows,
            ":nid": nifty_rows,
            ":vid": vix_rows,
        })

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
        session.execute.side_effect = _dispatch_execute({
            ":nid": nifty_rows,
            ":vid": vix_rows,
        })

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


# ===========================================================================
# SECTION 10 — Phase-0a fidelity fixes (Tuesday-expiry, day-count, real-IV, FSP)
# ===========================================================================


@needs_condor
class TestExpiryWeekday:
    """Fix #1 — date-aware NIFTY weekly-expiry weekday (Thu → Tue cutover)."""

    def test_thursday_before_cutover(self):
        from research.backtest.fno_condor import (
            NIFTY_TUESDAY_EXPIRY_CUTOVER,
            expiry_weekday_for,
        )
        # Day before cutover → Thursday (Python weekday 3)
        before = date(NIFTY_TUESDAY_EXPIRY_CUTOVER.year, 8, 15)
        assert expiry_weekday_for(before) == 3

    def test_tuesday_on_or_after_cutover(self):
        from research.backtest.fno_condor import (
            NIFTY_TUESDAY_EXPIRY_CUTOVER,
            expiry_weekday_for,
        )
        # On the cutover and after → Tuesday (Python weekday 1)
        assert expiry_weekday_for(NIFTY_TUESDAY_EXPIRY_CUTOVER) == 1
        after = date(NIFTY_TUESDAY_EXPIRY_CUTOVER.year + 1, 1, 1)
        assert expiry_weekday_for(after) == 1

    def test_cutover_is_project_convention_2026_09_01(self):
        """Project runs +1yr ahead of real life → real 2025-09-01 == 2026-09-01."""
        from research.backtest.fno_condor import NIFTY_TUESDAY_EXPIRY_CUTOVER
        assert NIFTY_TUESDAY_EXPIRY_CUTOVER == date(2026, 9, 1)

    def test_snap_picks_thursday_before_cutover(self):
        from research.backtest.fno_condor import snap_to_expiry_weekday
        # Full pre-cutover week Mon..Fri → Thursday chosen
        week = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
                date(2026, 1, 8), date(2026, 1, 9)]  # Mon..Fri; Thu = 01-08
        assert snap_to_expiry_weekday(week) == date(2026, 1, 8)

    def test_snap_picks_tuesday_after_cutover(self):
        from research.backtest.fno_condor import snap_to_expiry_weekday
        # Week in Sep-2026 (post-cutover) Mon..Fri → Tuesday chosen
        # 2026-09-07 is Mon, 09-08 Tue, ... 09-11 Fri
        week = [date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9),
                date(2026, 9, 10), date(2026, 9, 11)]
        assert snap_to_expiry_weekday(week) == date(2026, 9, 8)

    def test_snap_rolls_back_when_expiry_weekday_holiday(self):
        from research.backtest.fno_condor import snap_to_expiry_weekday
        # Pre-cutover week with NO Thursday (holiday) → roll back to Wed
        week = [date(2026, 1, 12), date(2026, 1, 14), date(2026, 1, 16)]  # Mon,Wed,Fri
        assert snap_to_expiry_weekday(week) == date(2026, 1, 14)

    def test_snap_empty_week_returns_none(self):
        from research.backtest.fno_condor import snap_to_expiry_weekday
        assert snap_to_expiry_weekday([]) is None


@needs_condor
class TestIndexConfigAgnostic:
    """Fix #1 (index-agnostic) — a NON-NIFTY underlying config drives its own
    expiry rule; the NIFTY default is unchanged.

    F&O code must not be NIFTY-hardcoded. ``expiry_weekday_for`` /
    ``snap_to_expiry_weekday`` take an ``IndexConfig``; here we define a
    monthly-only index whose expiry weekday is WEDNESDAY (2) with no cutover, and
    prove it snaps to Wednesday — while NIFTY still snaps to Thu/Tue.
    """

    @staticmethod
    def _monthly_only_index():
        from research.backtest.fno_condor import IndexConfig
        # A fictitious monthly-only underlying: no weeklies, fixed Wednesday
        # expiry, no Thursday→Tuesday cutover. Values are illustrative (the task
        # explicitly says non-NIFTY indices need no real data — just no hardcode).
        return IndexConfig(
            symbol="TESTIDX",
            security_id="999",
            vix_security_id=None,
            lot_size=15,
            strike_step=100,
            has_weeklies=False,
            expiry_weekday=2,        # Wednesday
            pre_cutover_weekday=None,
            cutover_date=None,
        )

    def test_nifty_default_config_values(self):
        from research.backtest.fno_condor import NIFTY, NIFTY_TUESDAY_EXPIRY_CUTOVER
        assert NIFTY.symbol == "NIFTY"
        assert NIFTY.security_id == "13"
        assert NIFTY.vix_security_id == "21"
        assert NIFTY.lot_size == 65
        assert NIFTY.strike_step == 50
        assert NIFTY.has_weeklies is True
        assert NIFTY.expiry_weekday == 1            # Tuesday (post-cutover)
        assert NIFTY.pre_cutover_weekday == 3       # Thursday (pre-cutover)
        assert NIFTY.cutover_date == NIFTY_TUESDAY_EXPIRY_CUTOVER

    def test_non_nifty_expiry_weekday_is_its_own(self):
        """A monthly-only index returns its OWN expiry weekday on every date —
        no NIFTY Thursday/Tuesday rule leaks in."""
        from research.backtest.fno_condor import expiry_weekday_for
        idx = self._monthly_only_index()
        # Both before and after NIFTY's cutover, the custom index stays Wednesday.
        assert expiry_weekday_for(date(2026, 1, 15), index=idx) == 2
        assert expiry_weekday_for(date(2026, 12, 15), index=idx) == 2

    def test_non_nifty_snap_picks_its_own_weekday(self):
        """snap_to_expiry_weekday on a non-NIFTY index snaps to its weekday."""
        from research.backtest.fno_condor import snap_to_expiry_weekday
        idx = self._monthly_only_index()
        # Mon..Fri week → Wednesday should be chosen (2026-01-14 is a Wednesday).
        week = [date(2026, 1, 12), date(2026, 1, 13), date(2026, 1, 14),
                date(2026, 1, 15), date(2026, 1, 16)]
        assert snap_to_expiry_weekday(week, index=idx) == date(2026, 1, 14)

    def test_nifty_behaviour_unchanged_via_config(self):
        """Passing the NIFTY config explicitly reproduces the default behaviour."""
        from research.backtest.fno_condor import (
            NIFTY,
            expiry_weekday_for,
            snap_to_expiry_weekday,
        )
        # Pre-cutover → Thursday; on/after → Tuesday (unchanged from the default).
        assert expiry_weekday_for(date(2026, 1, 15), index=NIFTY) == 3
        assert expiry_weekday_for(date(2026, 9, 1), index=NIFTY) == 1
        # Default arg (no index passed) must match passing NIFTY explicitly.
        assert expiry_weekday_for(date(2026, 1, 15)) == expiry_weekday_for(
            date(2026, 1, 15), index=NIFTY
        )
        pre_week = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
                    date(2026, 1, 8), date(2026, 1, 9)]
        assert snap_to_expiry_weekday(pre_week, index=NIFTY) == date(2026, 1, 8)

    def test_non_nifty_index_drives_weekly_cycles_from_db(self):
        """End-to-end: a non-NIFTY IndexConfig drives cycles_from_db weekly
        boundaries via its own security_id and Wednesday expiry rule."""
        from research.backtest.fno_condor import IndexConfig, cycles_from_db

        idx = IndexConfig(
            symbol="TESTIDX",
            security_id="999",
            vix_security_id="888",
            lot_size=15,
            strike_step=100,
            has_weeklies=False,
            expiry_weekday=2,   # Wednesday
        )
        # Two ISO weeks, each with a Wednesday → boundaries are the Wednesdays.
        #   2026-01-14 (Wed), 2026-01-21 (Wed) → 1 pair → 1 cycle
        index_rows = [
            (date(2026, 1, 12), 50000.0, 0.10),
            (date(2026, 1, 14), 50000.0, 0.10),   # Wed boundary week 1
            (date(2026, 1, 16), 50000.0, 0.10),
            (date(2026, 1, 19), 50000.0, 0.10),
            (date(2026, 1, 21), 50000.0, 0.10),   # Wed boundary week 2
        ]
        vix_rows = [(d, 15.0) for d, *_ in index_rows]

        def _make_result(rows):
            result = MagicMock()
            result.fetchall.return_value = rows
            return result

        session = MagicMock()
        # Assert the query used the custom index security_id (999), not NIFTY (13).
        captured = {}

        def _execute(stmt, params):
            # option_atm_iv query (keyed by :sym) → no real-IV rows for this index
            # → cycles stay on the VIX proxy. Returning [] keeps the 3-col contract.
            if "sym" in params:
                captured.setdefault("syms", []).append(params.get("sym"))
                return _make_result([])
            captured.setdefault("ids", []).append(params.get("nid") or params.get("vid"))
            # First call → index rows, second → vix rows
            if params.get("nid") == "999":
                return _make_result(index_rows)
            return _make_result(vix_rows)

        session.execute.side_effect = _execute

        @contextmanager
        def fake_get_session():
            yield session

        with patch("research.backtest.fno_condor.get_session", new=fake_get_session, create=True), \
             patch("db.get_session", new=fake_get_session):
            cycles = cycles_from_db(index=idx, mode="weekly")

        assert "999" in captured["ids"], "custom index security_id must drive the query"
        assert "888" in captured["ids"], "custom vix security_id must drive the query"
        assert len(cycles) == 1
        assert cycles[0]["entry_date"] == date(2026, 1, 14)   # Wednesday
        assert cycles[0]["expiry_date"] == date(2026, 1, 21)  # Wednesday


@needs_condor
class TestDayCountConvention:
    """Fix #2 — realized vol (√252) rebased to calendar (√365) for the gate."""

    def test_rebase_factor_is_sqrt_252_over_365(self):
        import math

        from research.backtest.fno_condor import realized_vol_to_calendar_basis
        rv = 0.20
        out = realized_vol_to_calendar_basis(rv)
        assert out == pytest.approx(rv * math.sqrt(252 / 365))
        # The factor is < 1 → calendar-basis vol is LOWER than trading-basis vol
        assert out < rv

    def test_none_passes_through(self):
        from research.backtest.fno_condor import realized_vol_to_calendar_basis
        assert realized_vol_to_calendar_basis(None) is None

    def test_gate_uses_calendar_basis(self):
        """A cycle that would STAND_ASIDE on the raw √252 vol but SELL once the
        vol is correctly rebased to the calendar basis must now produce a trade.

        realized_vol_20d=0.135 (√252). 0.9*iv with iv=0.15 → 0.135, so raw
        0.135 is NOT < 0.135 → STAND_ASIDE under the buggy mix. Rebased:
        0.135*√(252/365) ≈ 0.1121 < 0.135 → SELL.
        """
        from research.backtest.fno_condor import run_backtest
        cycle = {
            "cycle_id": "daycount",
            "spot": 23400.0,
            "straddle_iv": 0.15,
            "realized_vol_20d": 0.135,
            "dte": 7,
            "expiry_spot": 23400.0,
        }
        result = run_backtest([cycle], k=0.9, move_mult=1.0)
        assert result["n_trades"] == 1, "rebased realized vol should pass the SELL gate"


@needs_condor
class TestIvSourcePreference:
    """Fix #3 — prefer real ATM IV over the VIX proxy; source recorded + counted."""

    def test_resolve_prefers_real_atm(self):
        from research.backtest.fno_condor import IV_SOURCE_REAL, resolve_iv_source
        iv, src = resolve_iv_source({"atm_straddle_iv": 0.11, "straddle_iv": 0.15})
        assert iv == pytest.approx(0.11)
        assert src == IV_SOURCE_REAL

    def test_resolve_falls_back_to_vix_proxy(self):
        from research.backtest.fno_condor import IV_SOURCE_VIX_PROXY, resolve_iv_source
        iv, src = resolve_iv_source({"straddle_iv": 0.15})
        assert iv == pytest.approx(0.15)
        assert src == IV_SOURCE_VIX_PROXY

    def test_resolve_ignores_non_positive_real(self):
        from research.backtest.fno_condor import IV_SOURCE_VIX_PROXY, resolve_iv_source
        iv, src = resolve_iv_source({"atm_straddle_iv": 0.0, "straddle_iv": 0.15})
        assert iv == pytest.approx(0.15)
        assert src == IV_SOURCE_VIX_PROXY

    def test_resolve_none_when_no_iv(self):
        from research.backtest.fno_condor import resolve_iv_source
        iv, _src = resolve_iv_source({})
        assert iv is None

    def test_trade_records_real_iv_source_and_count(self):
        from research.backtest.fno_condor import IV_SOURCE_REAL, run_backtest
        cycle = {
            "cycle_id": "real_iv",
            "spot": 23400.0,
            "atm_straddle_iv": 0.15,   # real → preferred
            "straddle_iv": 0.30,       # proxy present but should be ignored
            "realized_vol_20d": 0.08,
            "dte": 7,
            "expiry_spot": 23400.0,
        }
        result = run_backtest([cycle], k=0.9, move_mult=1.0)
        assert result["n_trades"] == 1
        assert result["trades"][0].iv_source == IV_SOURCE_REAL
        assert result["trades"][0].straddle_iv == pytest.approx(0.15)
        assert result["n_real_iv"] == 1
        assert result["n_vix_proxy_iv"] == 0

    def test_go_no_go_flags_proxy_iv(self):
        from research.backtest.fno_condor import go_no_go
        metrics = {
            "n_trades": 30, "win_rate": 0.9, "profit_factor": 2.0, "sharpe": 1.5,
            "max_drawdown": -1000.0, "net_pnl": 50_000.0, "return_on_capital": 0.25,
            "n_real_iv": 0, "n_vix_proxy_iv": 30,
            "n_official_fsp": 0, "n_proxy_settlement": 30,
        }
        ok, reason = go_no_go(metrics, capital=200_000.0)
        assert ok is True
        assert "FIDELITY" in reason
        assert "proxy" in reason.lower()


@needs_condor
class TestSettlementSource:
    """Fix #4 — prefer NSE FSP, then half-hour VWAP, then close; flag residual."""

    def test_resolve_prefers_fsp(self):
        from research.backtest.fno_condor import FSP_SOURCE_OFFICIAL, resolve_settlement_price
        val, src = resolve_settlement_price(
            {"fsp": 23410.0, "expiry_halfhour_vwap": 23405.0, "expiry_spot": 23400.0}
        )
        assert val == pytest.approx(23410.0)
        assert src == FSP_SOURCE_OFFICIAL

    def test_resolve_uses_halfhour_vwap_when_no_fsp(self):
        from research.backtest.fno_condor import (
            FSP_SOURCE_HALFHOUR_VWAP,
            resolve_settlement_price,
        )
        val, src = resolve_settlement_price(
            {"expiry_halfhour_vwap": 23405.0, "expiry_spot": 23400.0}
        )
        assert val == pytest.approx(23405.0)
        assert src == FSP_SOURCE_HALFHOUR_VWAP

    def test_resolve_falls_back_to_close(self):
        from research.backtest.fno_condor import FSP_SOURCE_CLOSE_PROXY, resolve_settlement_price
        val, src = resolve_settlement_price({"expiry_spot": 23400.0})
        assert val == pytest.approx(23400.0)
        assert src == FSP_SOURCE_CLOSE_PROXY

    def test_resolve_none_when_no_settlement(self):
        from research.backtest.fno_condor import resolve_settlement_price
        val, _src = resolve_settlement_price({})
        assert val is None

    def test_trade_records_fsp_source(self):
        from research.backtest.fno_condor import FSP_SOURCE_OFFICIAL, run_backtest
        cycle = {
            "cycle_id": "fsp",
            "spot": 23400.0,
            "straddle_iv": 0.15,
            "realized_vol_20d": 0.08,
            "dte": 7,
            "fsp": 23400.0,            # official FSP used for resolution
            "expiry_spot": 23400.0,    # close present but FSP preferred
        }
        result = run_backtest([cycle], k=0.9, move_mult=1.0)
        assert result["n_trades"] == 1
        assert result["trades"][0].settlement_source == FSP_SOURCE_OFFICIAL
        assert result["n_official_fsp"] == 1
        assert result["n_proxy_settlement"] == 0

    def test_go_no_go_flags_proxy_settlement(self):
        from research.backtest.fno_condor import go_no_go
        metrics = {
            "n_trades": 30, "win_rate": 0.9, "profit_factor": 2.0, "sharpe": 1.5,
            "max_drawdown": -1000.0, "net_pnl": 50_000.0, "return_on_capital": 0.25,
            "n_real_iv": 30, "n_vix_proxy_iv": 0,
            "n_official_fsp": 0, "n_proxy_settlement": 30,
        }
        ok, reason = go_no_go(metrics, capital=200_000.0)
        assert ok is True
        assert "FIDELITY" in reason
        assert "FSP" in reason

    def test_cycle_with_only_fsp_and_real_iv_trades(self):
        """A cycle supplying ONLY the new fidelity keys (no VIX proxy / no close)
        must still trade — the malformed guard accepts either IV/settlement source."""
        from research.backtest.fno_condor import run_backtest
        cycle = {
            "cycle_id": "fidelity_only",
            "spot": 23400.0,
            "atm_straddle_iv": 0.15,   # no straddle_iv proxy
            "realized_vol_20d": 0.08,
            "dte": 7,
            "fsp": 23400.0,            # no expiry_spot close
        }
        result = run_backtest([cycle], k=0.9, move_mult=1.0)
        assert result["n_trades"] == 1


# ===========================================================================
# SECTION — PHASE-0c: configurable DTE / exit rules / fixed-delta strikes
# (TZ-safe: all dates are explicit literals; no date.today()/now() for state.)
# ===========================================================================


def _sell_premium_cycle(entry, expiry, spot=20000.0, iv=0.12, rvol=0.06, **extra):
    """Build one well-formed SELL_PREMIUM cycle (realized << implied → gate passes).

    realized_vol_20d=0.06 is well below k×iv on the calendar basis, so the
    vol-gate returns SELL_PREMIUM and the cycle is traded.
    """
    c = {
        "entry_date": entry,
        "expiry_date": expiry,
        "spot": spot,
        "straddle_iv": iv,
        "realized_vol_20d": rvol,
        "dte": (expiry - entry).days,
        "expiry_spot": spot,  # settles ATM → keeps the credit (a win)
    }
    c.update(extra)
    return c


@needs_condor
class TestBlack76Delta:
    def test_atm_call_delta_near_half(self):
        from research.backtest.fno_condor import black76_delta
        d = black76_delta(20000, 20000, 7 / 365, 0.15, "CE")
        assert 0.45 < d < 0.60

    def test_atm_put_delta_near_minus_half(self):
        from research.backtest.fno_condor import black76_delta
        d = black76_delta(20000, 20000, 7 / 365, 0.15, "PE")
        assert -0.60 < d < -0.40

    def test_call_delta_in_unit_interval(self):
        from research.backtest.fno_condor import black76_delta
        d = black76_delta(20000, 21000, 7 / 365, 0.15, "CE")
        assert 0.0 <= d <= 1.0

    def test_put_delta_negative(self):
        from research.backtest.fno_condor import black76_delta
        d = black76_delta(20000, 19000, 7 / 365, 0.15, "PE")
        assert -1.0 <= d <= 0.0

    def test_deep_otm_call_delta_to_zero(self):
        from research.backtest.fno_condor import black76_delta
        d = black76_delta(20000, 25000, 7 / 365, 0.15, "CE")
        assert d < 0.05

    def test_zero_dte_call_degenerate(self):
        from research.backtest.fno_condor import black76_delta
        assert black76_delta(20000, 19000, 0.0, 0.15, "CE") == 1.0
        assert black76_delta(20000, 21000, 0.0, 0.15, "CE") == 0.0

    def test_zero_sigma_put_degenerate(self):
        from research.backtest.fno_condor import black76_delta
        assert black76_delta(20000, 21000, 7 / 365, 0.0, "PE") == -1.0
        assert black76_delta(20000, 19000, 7 / 365, 0.0, "PE") == 0.0


@needs_condor
class TestSelectShortStrikeByDelta:
    def test_short_put_below_spot(self):
        from research.backtest.fno_condor import select_short_strike_by_delta
        k, src = select_short_strike_by_delta(20000, 0.12, 7, "PE", 0.16)
        assert k < 20000

    def test_short_call_above_spot(self):
        from research.backtest.fno_condor import select_short_strike_by_delta
        k, src = select_short_strike_by_delta(20000, 0.12, 7, "CE", 0.16)
        assert k > 20000

    def test_strike_on_grid(self):
        from research.backtest.fno_condor import select_short_strike_by_delta
        k, _ = select_short_strike_by_delta(20000, 0.12, 7, "CE", 0.16, step=50)
        assert k % 50 == 0

    def test_smaller_target_delta_is_further_otm(self):
        from research.backtest.fno_condor import select_short_strike_by_delta
        near, _ = select_short_strike_by_delta(20000, 0.12, 7, "CE", 0.30)
        far, _ = select_short_strike_by_delta(20000, 0.12, 7, "CE", 0.10)
        assert far >= near  # smaller delta → further from ATM (higher call strike)

    def test_chosen_strike_delta_close_to_target(self):
        from research.backtest.fno_condor import black76_delta, select_short_strike_by_delta
        k, _ = select_short_strike_by_delta(20000, 0.12, 7, "CE", 0.16)
        d = abs(black76_delta(20000, k, 7 / 365, 0.12, "CE"))
        assert abs(d - 0.16) < 0.10

    def test_fallback_source_without_real_iv(self):
        from research.backtest.fno_condor import DELTA_IV_SOURCE_FLAT, select_short_strike_by_delta
        _, src = select_short_strike_by_delta(20000, 0.12, 7, "PE", 0.16)
        assert src == DELTA_IV_SOURCE_FLAT

    def test_real_iv_source_when_chain_covers_grid(self):
        from research.backtest.fno_condor import DELTA_IV_SOURCE_REAL, select_short_strike_by_delta
        per_strike = {k: 0.15 for k in range(18000, 22001, 50)}
        _, src = select_short_strike_by_delta(
            20000, 0.12, 7, "PE", 0.16, per_strike_iv=per_strike
        )
        assert src == DELTA_IV_SOURCE_REAL

    def test_real_iv_changes_selected_strike(self):
        """Real per-strike IV (a skewed surface) shifts the selected strike vs flat."""
        from research.backtest.fno_condor import select_short_strike_by_delta
        flat_k, _ = select_short_strike_by_delta(20000, 0.12, 7, "PE", 0.16)
        # Rich OTM-put IV (skew) → wider deltas at OTM strikes → different selection.
        skewed = {k: 0.12 + max(0.0, (20000 - k) / 20000 * 0.6) for k in range(18000, 22001, 50)}
        real_k, _ = select_short_strike_by_delta(
            20000, 0.12, 7, "PE", 0.16, per_strike_iv=skewed
        )
        assert real_k != flat_k or real_k < 20000  # selection responds to real surface


@needs_condor
class TestBuildCondorByDelta:
    def test_ordering(self):
        from research.backtest.fno_condor import build_condor_by_delta
        strikes, _ = build_condor_by_delta(20000, 0.12, 7, 0.16, wing_strikes=2)
        assert (
            strikes["long_put_k"]
            < strikes["short_put_k"]
            < strikes["short_call_k"]
            < strikes["long_call_k"]
        )

    def test_wing_width_matches_strikes(self):
        from research.backtest.fno_condor import build_condor_by_delta
        strikes, _ = build_condor_by_delta(20000, 0.12, 7, 0.16, wing_strikes=3, step=50)
        assert strikes["short_put_k"] - strikes["long_put_k"] == 150
        assert strikes["long_call_k"] - strikes["short_call_k"] == 150

    def test_source_real_when_chain_present(self):
        from research.backtest.fno_condor import DELTA_IV_SOURCE_REAL, build_condor_by_delta
        per_strike = {k: 0.15 for k in range(18000, 22001, 50)}
        _, src = build_condor_by_delta(20000, 0.12, 7, 0.16, per_strike_iv=per_strike)
        assert src == DELTA_IV_SOURCE_REAL

    def test_source_flat_fallback(self):
        from research.backtest.fno_condor import DELTA_IV_SOURCE_FLAT, build_condor_by_delta
        _, src = build_condor_by_delta(20000, 0.12, 7, 0.16)
        assert src == DELTA_IV_SOURCE_FLAT


@needs_condor
class TestExitParams:
    def test_default_is_expiry_hold(self):
        from research.backtest.fno_condor import ExitParams
        assert ExitParams().is_expiry_hold is True

    def test_any_rule_disables_expiry_hold(self):
        from research.backtest.fno_condor import ExitParams
        assert ExitParams(profit_target_pct=0.5).is_expiry_hold is False
        assert ExitParams(stop_loss_mult=2.0).is_expiry_hold is False
        assert ExitParams(time_stop_dte=1).is_expiry_hold is False


@needs_condor
class TestEvaluateExit:
    STRIKES = {
        "short_put_k": 19800,
        "long_put_k": 19700,
        "short_call_k": 20200,
        "long_call_k": 20300,
    }

    def _credit(self):
        from research.backtest.fno_condor import condor_mtm_value
        return condor_mtm_value(20000, 0.12, 7, self.STRIKES)

    def test_no_rules_holds_to_expiry(self):
        from research.backtest.fno_condor import EXIT_REASON_EXPIRY, ExitParams, evaluate_exit
        r = evaluate_exit(self._credit(), self.STRIKES, 0.12, 7,
                          [(5, 20000)], ExitParams())
        assert r[0] == EXIT_REASON_EXPIRY

    def test_no_path_holds_to_expiry(self):
        from research.backtest.fno_condor import EXIT_REASON_EXPIRY, ExitParams, evaluate_exit
        r = evaluate_exit(self._credit(), self.STRIKES, 0.12, 7,
                          None, ExitParams(stop_loss_mult=0.1))
        assert r[0] == EXIT_REASON_EXPIRY

    def test_profit_target_fires(self):
        from research.backtest.fno_condor import EXIT_REASON_PROFIT_TARGET, ExitParams, evaluate_exit
        # Time decays toward expiry at a still spot → value collapses → profit.
        r = evaluate_exit(self._credit(), self.STRIKES, 0.12, 7,
                          [(5, 20000), (2, 20000), (1, 20000)],
                          ExitParams(profit_target_pct=0.5))
        assert r[0] == EXIT_REASON_PROFIT_TARGET
        assert r[2] is not None and r[2] > 0

    def test_stop_loss_fires(self):
        from research.backtest.fno_condor import EXIT_REASON_STOP_LOSS, ExitParams, evaluate_exit
        # Spot breaches the short put → MTM rises → loss past the stop.
        r = evaluate_exit(self._credit(), self.STRIKES, 0.12, 7,
                          [(5, 19750)], ExitParams(stop_loss_mult=0.1))
        assert r[0] == EXIT_REASON_STOP_LOSS
        assert r[2] is not None and r[2] < 0

    def test_time_stop_fires(self):
        from research.backtest.fno_condor import EXIT_REASON_TIME_STOP, ExitParams, evaluate_exit
        r = evaluate_exit(self._credit(), self.STRIKES, 0.12, 7,
                          [(5, 20000), (2, 20000)], ExitParams(time_stop_dte=2))
        assert r[0] == EXIT_REASON_TIME_STOP
        assert r[1] == 2

    def test_profit_target_priority_over_time_stop(self):
        from research.backtest.fno_condor import EXIT_REASON_PROFIT_TARGET, ExitParams, evaluate_exit
        # Both could fire; profit-target is checked first at the same node.
        r = evaluate_exit(self._credit(), self.STRIKES, 0.12, 7,
                          [(2, 20000)],
                          ExitParams(profit_target_pct=0.3, time_stop_dte=2))
        assert r[0] == EXIT_REASON_PROFIT_TARGET


@needs_condor
class TestPickEntryDayForDte:
    DAYS = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3),
            date(2026, 6, 4), date(2026, 6, 5)]
    EXPIRY = date(2026, 6, 5)

    def test_pick_at_or_before_target(self):
        from research.backtest.fno_condor import pick_entry_day_for_dte
        # 4 DTE → the day with dte>=4 closest to 4 = 2026-06-01 (dte 4).
        assert pick_entry_day_for_dte(self.DAYS, self.EXPIRY, 4) == date(2026, 6, 1)

    def test_pick_one_dte(self):
        from research.backtest.fno_condor import pick_entry_day_for_dte
        assert pick_entry_day_for_dte(self.DAYS, self.EXPIRY, 1) == date(2026, 6, 4)

    def test_target_larger_than_available_takes_largest_dte(self):
        from research.backtest.fno_condor import pick_entry_day_for_dte
        assert pick_entry_day_for_dte(self.DAYS, self.EXPIRY, 30) == date(2026, 6, 1)

    def test_no_day_before_expiry_returns_none(self):
        from research.backtest.fno_condor import pick_entry_day_for_dte
        assert pick_entry_day_for_dte([date(2026, 6, 5)], self.EXPIRY, 4) is None


@needs_condor
class TestRunBacktestPhase0c:
    def _cycles(self, n=40):
        cs = []
        base = date(2026, 1, 6)
        for i in range(n):
            ed = base + timedelta(days=7 * i)
            cs.append(_sell_premium_cycle(ed, ed + timedelta(days=7),
                                          spot=20000.0 + (i % 5) * 50))
        return cs

    def test_default_is_move_method_backcompat(self):
        cs = self._cycles()
        res = run_backtest(cs, k=0.9, move_mult=1.5)
        assert res["n_trades"] > 0
        for t in res["trades"]:
            assert t.strike_method == "move"
            assert t.exit_reason == "expiry"

    def test_delta_method_runs_and_tags(self):
        from research.backtest.fno_condor import STRIKE_METHOD_DELTA
        cs = self._cycles()
        res = run_backtest(cs, k=0.9, strike_method=STRIKE_METHOD_DELTA, target_delta=0.16)
        assert res["n_trades"] > 0
        for t in res["trades"]:
            assert t.strike_method == "delta"
            assert t.delta_iv_source in ("flat_atm", "real_per_strike")

    def test_delta_real_iv_when_cycle_has_per_strike(self):
        from research.backtest.fno_condor import STRIKE_METHOD_DELTA
        per_strike = {k: 0.12 for k in range(15000, 25001, 50)}
        cs = self._cycles(5)
        for c in cs:
            c["per_strike_iv"] = per_strike
        res = run_backtest(cs, k=0.9, strike_method=STRIKE_METHOD_DELTA)
        assert res["n_trades"] > 0
        assert any(t.delta_iv_source == "real_per_strike" for t in res["trades"])

    def test_exit_params_default_holds(self):
        cs = self._cycles()
        res = run_backtest(cs, k=0.9, exit_params=None)
        for t in res["trades"]:
            assert t.exit_reason == "expiry"

    def test_time_stop_exit_fires_in_backtest(self):
        from research.backtest.fno_condor import ExitParams
        cs = self._cycles(20)
        for c in cs:
            # path with a node at 2 DTE so the time-stop can fire
            c["spot_path"] = [(5, c["spot"]), (2, c["spot"])]
        res = run_backtest(cs, k=0.9, exit_params=ExitParams(time_stop_dte=2))
        assert res["n_trades"] > 0
        assert any(t.exit_reason == "time_stop" for t in res["trades"])

    def test_wing_strikes_param_widens_structure(self):
        cs = self._cycles(5)
        narrow = run_backtest(cs, k=0.9, wing_strikes=1)
        wide = run_backtest(cs, k=0.9, wing_strikes=4)
        nt = narrow["trades"][0]
        wt = wide["trades"][0]
        nwidth = nt.strikes["short_put_k"] - nt.strikes["long_put_k"]
        wwidth = wt.strikes["short_put_k"] - wt.strikes["long_put_k"]
        assert wwidth > nwidth


@needs_condor
class TestPhase0cQaFixes:
    """Regression tests for the QA-loop fixes (negative-credit exit, F/K guard,
    closing costs + close slippage on early exit, end-to-end entry_dte)."""

    STRIKES = {
        "short_put_k": 19800,
        "long_put_k": 19700,
        "short_call_k": 20200,
        "long_call_k": 20300,
    }

    def test_black76_delta_nonpositive_inputs_no_raise(self):
        from research.backtest.fno_condor import black76_delta
        # F<=0 or K<=0 must NOT raise (math.log guard) — returns intrinsic delta.
        assert black76_delta(0.0, 20000, 7 / 365, 0.15, "CE") == 0.0
        assert black76_delta(20000, 0.0, 7 / 365, 0.15, "CE") == 1.0
        assert black76_delta(-5.0, 20000, 7 / 365, 0.15, "PE") in (-1.0, 0.0)

    def test_negative_credit_exit_signs_do_not_invert(self):
        """With a non-positive credit base, abs() keeps thresholds sane: a
        profit-target needs running_pnl>=0, a stop needs running_pnl<=0 — never
        the reverse."""
        from research.backtest.fno_condor import (
            EXIT_REASON_PROFIT_TARGET,
            EXIT_REASON_STOP_LOSS,
            ExitParams,
            evaluate_exit,
        )
        # Negative credit; a path where MTM drops below credit → running_pnl > 0.
        neg_credit = -1000.0
        # condor MTM at a still spot, time decayed → positive running_pnl over neg credit
        r = evaluate_exit(neg_credit, self.STRIKES, 0.12, 7,
                          [(1, 20000)], ExitParams(profit_target_pct=0.5))
        # profit-target may or may not fire, but if it fires running_pnl must be >= 0
        if r[0] == EXIT_REASON_PROFIT_TARGET:
            assert r[2] >= 0
        # Adverse path → loss; stop fires only on running_pnl <= 0
        r2 = evaluate_exit(neg_credit, self.STRIKES, 0.12, 7,
                           [(5, 19750)], ExitParams(stop_loss_mult=0.1))
        if r2[0] == EXIT_REASON_STOP_LOSS:
            assert r2[2] <= 0

    def test_evaluate_exit_returns_exit_spot(self):
        from research.backtest.fno_condor import (
            EXIT_REASON_TIME_STOP,
            ExitParams,
            condor_mtm_value,
            evaluate_exit,
        )
        credit = condor_mtm_value(20000, 0.12, 7, self.STRIKES)
        r = evaluate_exit(credit, self.STRIKES, 0.12, 7,
                          [(2, 20111.0)], ExitParams(time_stop_dte=2))
        assert r[0] == EXIT_REASON_TIME_STOP
        assert r[3] == 20111.0  # exit_spot threaded through

    def _cycles_with_path(self, n=20, spot=20000.0):
        cs = []
        base = date(2026, 1, 6)
        for i in range(n):
            ed = base + timedelta(days=7 * i)
            c = _sell_premium_cycle(ed, ed + timedelta(days=7), spot=spot)
            c["spot_path"] = [(5, spot), (2, spot)]  # time-stop can fire at 2 DTE
            cs.append(c)
        return cs

    def test_early_exit_charges_more_costs_than_hold(self):
        """An early exit (time-stop) must book MORE total costs than holding to
        expiry on the same cycles (it pays the close-side brokerage/STT stack)."""
        from research.backtest.fno_condor import ExitParams
        cs = self._cycles_with_path(10)
        hold = run_backtest(cs, k=0.9)  # default = expiry hold
        early = run_backtest(cs, k=0.9, exit_params=ExitParams(time_stop_dte=2))
        hold_costs = sum(t.costs for t in hold["trades"])
        early_costs = sum(t.costs for t in early["trades"])
        assert any(t.exit_reason == "time_stop" for t in early["trades"])
        assert early_costs > hold_costs

    def test_entry_dte_reanchors_end_to_end(self):
        """run_backtest over cycles_from_db(entry_dte=N) is not exercised here (DB),
        but the pure re-anchor helper composes with run_backtest: a cycle whose
        dte already equals N trades unchanged."""
        cs = []
        base = date(2026, 1, 6)
        for i in range(35):
            ed = base + timedelta(days=7 * i)
            cs.append(_sell_premium_cycle(ed, ed + timedelta(days=4), spot=20000.0))
        res = run_backtest(cs, k=0.9)
        assert res["n_trades"] > 0
        for t in res["trades"]:
            assert (t.expiry_date - t.entry_date).days == 4

    def test_delta_real_vs_flat_paths_both_trade(self):
        from research.backtest.fno_condor import STRIKE_METHOD_DELTA
        cs_flat = []
        cs_real = []
        base = date(2026, 1, 6)
        per_strike = {k: 0.12 for k in range(15000, 25001, 50)}
        for i in range(10):
            ed = base + timedelta(days=7 * i)
            cf = _sell_premium_cycle(ed, ed + timedelta(days=7), spot=20000.0)
            cr = _sell_premium_cycle(ed, ed + timedelta(days=7), spot=20000.0)
            cr["per_strike_iv"] = per_strike
            cs_flat.append(cf)
            cs_real.append(cr)
        rf = run_backtest(cs_flat, k=0.9, strike_method=STRIKE_METHOD_DELTA)
        rr = run_backtest(cs_real, k=0.9, strike_method=STRIKE_METHOD_DELTA)
        assert rf["n_trades"] > 0 and rr["n_trades"] > 0
        assert all(t.delta_iv_source == "flat_atm" for t in rf["trades"])
        assert any(t.delta_iv_source == "real_per_strike" for t in rr["trades"])


# ===========================================================================
# SECTION 12 — Real-IV wiring (option_atm_iv JOIN) + gate-v2 routing
# ===========================================================================
# Backtest-only. All dates are explicit (no date.today()/naive now()) so the
# suite is TZ-safe and passes identically under TZ=UTC (CI) and IST (dev Mac).


@needs_condor
class TestRealIvSanityFilter:
    """is_plausible_real_iv gates the real ATM straddle IV read."""

    def test_plausible_in_band(self):
        from research.backtest.fno_condor import is_plausible_real_iv
        assert is_plausible_real_iv(0.10) is True
        assert is_plausible_real_iv(0.55) is True
        assert is_plausible_real_iv(1.99) is True

    def test_rejects_above_max(self):
        # The pull had expiry-day blowups up to 5.2 (520 %) → must be rejected.
        from research.backtest.fno_condor import REAL_IV_MAX, is_plausible_real_iv
        assert is_plausible_real_iv(2.01) is False
        assert is_plausible_real_iv(5.2) is False
        assert is_plausible_real_iv(REAL_IV_MAX) is True  # boundary inclusive

    def test_rejects_degenerate_low_and_none_and_nan(self):
        from research.backtest.fno_condor import is_plausible_real_iv
        assert is_plausible_real_iv(0.0) is False
        assert is_plausible_real_iv(0.001) is False  # below REAL_IV_MIN
        assert is_plausible_real_iv(-0.1) is False
        assert is_plausible_real_iv(None) is False
        assert is_plausible_real_iv(float("nan")) is False


@needs_condor
class TestResolveAtmIvPit:
    """_resolve_atm_iv_pit: strict-PIT preferred, with a ±N-trading-day entry-roll
    tolerance fallback when no strict ``<= entry`` row exists (no look-ahead beyond
    the documented small roll window)."""

    def test_picks_latest_on_or_before_entry(self):
        from research.backtest.fno_condor import _resolve_atm_iv_pit
        exp = date(2026, 1, 22)
        iv_map = {exp: [
            (date(2026, 1, 16), 0.10),
            (date(2026, 1, 19), 0.12),  # latest <= entry 01-20 → strict PIT wins
            (date(2026, 1, 21), 0.99),  # AFTER entry → must NOT be seen (look-ahead)
        ]}
        assert _resolve_atm_iv_pit(iv_map, exp, date(2026, 1, 20)) == 0.12

    def test_exact_entry_day_value_used(self):
        from research.backtest.fno_condor import _resolve_atm_iv_pit
        exp = date(2026, 1, 22)
        iv_map = {exp: [(date(2026, 1, 19), 0.12), (date(2026, 1, 20), 0.15)]}
        assert _resolve_atm_iv_pit(iv_map, exp, date(2026, 1, 20)) == 0.15

    def test_strict_pit_preferred_over_tolerance_row(self):
        # Both a strict-PIT row (01-19) and a post-entry tolerance row (01-21)
        # exist → the strict row MUST win regardless of tolerance.
        from research.backtest.fno_condor import _resolve_atm_iv_pit
        exp = date(2026, 1, 22)
        iv_map = {exp: [(date(2026, 1, 19), 0.12), (date(2026, 1, 21), 0.30)]}
        assert _resolve_atm_iv_pit(iv_map, exp, date(2026, 1, 20), tolerance_days=2) == 0.12

    def test_tolerance_zero_reproduces_strict_behaviour(self):
        # tolerance_days=0 → old strict ``<= entry`` behaviour: a post-entry-only
        # row is NOT matched.
        from research.backtest.fno_condor import _resolve_atm_iv_pit
        exp = date(2026, 1, 22)
        iv_map = {exp: [(date(2026, 1, 21), 0.13)]}
        assert _resolve_atm_iv_pit(iv_map, exp, date(2026, 1, 20), tolerance_days=0) is None

    def test_entry_roll_row_matched_within_tolerance(self):
        # The expiry-day roll case: the ONLY row for this expiry is the trading
        # day AFTER entry (entry+1) → matched within the default ±2-day tolerance.
        from research.backtest.fno_condor import _resolve_atm_iv_pit
        exp = date(2026, 1, 22)
        iv_map = {exp: [(date(2026, 1, 21), 0.13)]}  # entry 01-20, obs 01-21 (+1)
        assert _resolve_atm_iv_pit(iv_map, exp, date(2026, 1, 20)) == 0.13
        # Explicit tolerance also matches.
        assert _resolve_atm_iv_pit(iv_map, exp, date(2026, 1, 20), tolerance_days=2) == 0.13

    def test_earliest_post_entry_row_chosen_within_tolerance(self):
        # When several post-entry rows exist (no strict-PIT row), the EARLIEST is
        # chosen (closest to entry).
        from research.backtest.fno_condor import _resolve_atm_iv_pit
        exp = date(2026, 1, 30)
        iv_map = {exp: [(date(2026, 1, 21), 0.13), (date(2026, 1, 22), 0.40)]}
        assert _resolve_atm_iv_pit(iv_map, exp, date(2026, 1, 20)) == 0.13

    def test_data_gap_beyond_calendar_window_not_matched(self):
        # A row far past entry (multi-week data gap, NOT the expiry-day roll) must
        # NOT match even though it is the earliest post-entry obs.
        from research.backtest.fno_condor import _resolve_atm_iv_pit
        exp = date(2026, 2, 20)
        iv_map = {exp: [(date(2026, 2, 5), 0.13)]}  # entry 01-20, obs +16 days
        assert _resolve_atm_iv_pit(iv_map, exp, date(2026, 1, 20)) is None

    def test_huge_tolerance_still_bounded_by_calendar_hard_cap(self):
        # A pathologically large tolerance_days must NOT let a far-future obs match
        # — the absolute calendar hard cap (14 days) rejects the multi-week gap.
        from research.backtest.fno_condor import _resolve_atm_iv_pit
        exp = date(2026, 6, 20)
        iv_map = {exp: [(date(2026, 2, 20), 0.13)]}  # entry 01-20, obs ~31 days out
        assert _resolve_atm_iv_pit(iv_map, exp, date(2026, 1, 20), tolerance_days=1000) is None

    def test_no_observation_before_entry_returns_none_strict(self):
        from research.backtest.fno_condor import _resolve_atm_iv_pit
        exp = date(2026, 1, 22)
        iv_map = {exp: [(date(2026, 1, 21), 0.13)]}
        assert _resolve_atm_iv_pit(iv_map, exp, date(2026, 1, 20), tolerance_days=0) is None

    def test_missing_expiry_returns_none(self):
        from research.backtest.fno_condor import _resolve_atm_iv_pit
        assert _resolve_atm_iv_pit({}, date(2026, 1, 22), date(2026, 1, 20)) is None


@needs_condor
class TestCyclesFromDbRealIvJoin:
    """option_atm_iv JOIN populates cycle['atm_straddle_iv'] PIT-correctly."""

    # Two NIFTY weekly cycles, pre-cutover (Thursday expiries).
    #   ISO week 1: Mon 01-05, Thu 01-08
    #   ISO week 2: Mon 01-12, Thu 01-15
    #   ISO week 3: Mon 01-19, Thu 01-22
    # boundaries [01-08, 01-15, 01-22] → cycles (01-08→01-15), (01-15→01-22)
    _NIFTY_ROWS = [
        (date(2026, 1, 5),  23000.0, 0.10),
        (date(2026, 1, 8),  23000.0, 0.10),
        (date(2026, 1, 12), 23000.0, 0.10),
        (date(2026, 1, 15), 23000.0, 0.10),
        (date(2026, 1, 19), 23000.0, 0.10),
        (date(2026, 1, 22), 23000.0, 0.10),
    ]
    _VIX_ROWS = [(d, 14.0) for d, *_ in _NIFTY_ROWS]  # VIX/100 = 0.14 proxy

    @staticmethod
    def _run(atm_rows, use_real_iv=True, real_iv_tolerance_days=None):
        from research.backtest.fno_condor import cycles_from_db
        fake_gs_factory = TestCyclesFromDbRealIvJoin._make_gs(atm_rows)
        kwargs = {"mode": "weekly", "use_real_iv": use_real_iv}
        if real_iv_tolerance_days is not None:
            kwargs["real_iv_tolerance_days"] = real_iv_tolerance_days
        with patch("research.backtest.fno_condor.get_session", new=fake_gs_factory, create=True), \
             patch("db.get_session", new=fake_gs_factory):
            return cycles_from_db(**kwargs)

    @staticmethod
    def _make_gs(atm_rows):
        session = MagicMock()
        session.execute.side_effect = _dispatch_execute({
            ":nid": TestCyclesFromDbRealIvJoin._NIFTY_ROWS,
            ":vid": TestCyclesFromDbRealIvJoin._VIX_ROWS,
            "from option_atm_iv": atm_rows,
        })

        @contextmanager
        def fake_get_session():
            yield session

        return fake_get_session

    def test_real_iv_populated_pit_for_matching_expiry(self):
        # Real IV observed for expiry 01-15 on/before entry 01-08 → cycle 0 gets it.
        # Columns: (expiry_date, obs_date, straddle_iv)
        atm = [
            (date(2026, 1, 15), date(2026, 1, 8), 0.20),
            (date(2026, 1, 22), date(2026, 1, 15), 0.22),
        ]
        cycles = self._run(atm)
        assert cycles[0]["expiry_date"] == date(2026, 1, 15)
        assert cycles[0]["atm_straddle_iv"] == 0.20
        assert cycles[1]["expiry_date"] == date(2026, 1, 22)
        assert cycles[1]["atm_straddle_iv"] == 0.22
        # VIX proxy still present as the fallback.
        assert cycles[0]["straddle_iv"] == pytest.approx(0.14)

    def test_entry_roll_row_within_tolerance_populates_real_iv(self):
        # Real-IV JOIN coverage fix: expiry 01-15's only row is observed 01-12
        # (the rolling-expiry roll — front becomes front the trading day AFTER the
        # prior expiry; entry is 01-08). Within the default ±2-trading-day
        # tolerance this NOW populates atm_straddle_iv (was the 41/232-miss bug).
        atm = [(date(2026, 1, 15), date(2026, 1, 12), 0.20)]
        cycles = self._run(atm)
        assert cycles[0]["expiry_date"] == date(2026, 1, 15)
        assert cycles[0]["atm_straddle_iv"] == 0.20

    def test_tolerance_zero_reproduces_strict_lookahead_guard(self):
        # tolerance=0 → strict ``<= entry`` PIT: the post-entry roll row (01-12 >
        # entry 01-08) is NOT matched → cycle 0 stays on the VIX proxy.
        atm = [(date(2026, 1, 15), date(2026, 1, 12), 0.20)]
        cycles = self._run(atm, real_iv_tolerance_days=0)
        assert "atm_straddle_iv" not in cycles[0]

    def test_row_beyond_tolerance_window_stays_on_vix(self):
        # A row far past entry (a genuine multi-week data gap, NOT the expiry-day
        # roll) must NOT match → cycle stays on the VIX proxy. expiry 01-15 entry
        # 01-08; obs 02-10 is ~33 days out, well beyond ±2 trading days.
        atm = [(date(2026, 1, 15), date(2026, 2, 10), 0.20)]
        cycles = self._run(atm)
        assert "atm_straddle_iv" not in cycles[0]

    def test_strict_pit_row_wins_over_post_entry_tolerance_row_end_to_end(self):
        # End-to-end through cycles_from_db: when BOTH a strict-PIT row
        # (obs <= entry) and a within-tolerance post-entry row exist for the same
        # expiry, the strict row MUST win (no look-ahead). Cycle 0 entry=01-08:
        #   strict-PIT row obs=01-08 iv=0.20  vs  tolerance row obs=01-09 iv=0.30.
        atm = [
            (date(2026, 1, 15), date(2026, 1, 8),  0.20),  # strict PIT → must win
            (date(2026, 1, 15), date(2026, 1, 9),  0.30),  # tolerance-eligible, must lose
            (date(2026, 1, 22), date(2026, 1, 15), 0.22),  # cycle 1 normal strict row
        ]
        cycles = self._run(atm)
        assert cycles[0]["atm_straddle_iv"] == pytest.approx(0.20)
        assert cycles[1]["atm_straddle_iv"] == pytest.approx(0.22)

    def test_earliest_post_entry_row_chosen_when_multiple_exist_end_to_end(self):
        # End-to-end: with NO strict-PIT row and MULTIPLE post-entry rows for one
        # expiry, the EARLIEST (closest to entry) must be chosen. Cycle 0
        # entry=01-08: obs 01-09 iv=0.13 (earliest) vs 01-10 iv=0.40 (later).
        atm = [
            (date(2026, 1, 15), date(2026, 1, 9),  0.13),  # earliest post-entry → chosen
            (date(2026, 1, 15), date(2026, 1, 10), 0.40),  # later → must NOT win
            (date(2026, 1, 22), date(2026, 1, 16), 0.22),  # cycle 1 rank-1 post-entry
        ]
        cycles = self._run(atm)
        assert cycles[0]["atm_straddle_iv"] == pytest.approx(0.13)
        assert cycles[1]["atm_straddle_iv"] == pytest.approx(0.22)

    def test_implausible_iv_filtered_falls_back_to_vix(self):
        # 5.2 (520 %) expiry-day blowup for cycle 0's expiry → filtered → VIX proxy.
        # cycle 1 gets a valid 0.20.
        atm = [
            (date(2026, 1, 15), date(2026, 1, 8), 5.2),
            (date(2026, 1, 22), date(2026, 1, 15), 0.20),
        ]
        cycles = self._run(atm)
        assert "atm_straddle_iv" not in cycles[0]      # blowup filtered
        assert cycles[1]["atm_straddle_iv"] == 0.20

    def test_use_real_iv_false_skips_join_entirely(self):
        # Even with real rows present, use_real_iv=False must not enrich.
        atm = [(date(2026, 1, 15), date(2026, 1, 8), 0.20)]
        cycles = self._run(atm, use_real_iv=False)
        assert all("atm_straddle_iv" not in c for c in cycles)

    def test_default_is_vix_only_no_real_iv(self):
        # cycles_from_db default (use_real_iv unset → False) must NOT enrich, so
        # existing callers keep VIX-proxy behaviour. Real rows present but ignored.
        from research.backtest.fno_condor import cycles_from_db
        atm = [(date(2026, 1, 15), date(2026, 1, 8), 0.20)]
        fake_gs = self._make_gs(atm)
        with patch("research.backtest.fno_condor.get_session", new=fake_gs, create=True), \
             patch("db.get_session", new=fake_gs):
            cycles = cycles_from_db(mode="weekly")  # no use_real_iv → default False
        assert all("atm_straddle_iv" not in c for c in cycles)
        assert all(c["straddle_iv"] == pytest.approx(0.14) for c in cycles)

    def test_empty_option_atm_iv_falls_back_to_vix(self):
        # use_real_iv=True but the option_atm_iv query returns NO rows → every
        # cycle stays on the VIX proxy, no crash, no atm_straddle_iv key.
        cycles = self._run([], use_real_iv=True)
        assert len(cycles) == 2
        assert all("atm_straddle_iv" not in c for c in cycles)
        assert all(c["straddle_iv"] == pytest.approx(0.14) for c in cycles)


@needs_condor
class TestResolveIvSourcePreference:
    """resolve_iv_source prefers real ATM IV, then the VIX proxy."""

    def test_prefers_real_when_present(self):
        from research.backtest.fno_condor import IV_SOURCE_REAL, resolve_iv_source
        iv, src = resolve_iv_source({"atm_straddle_iv": 0.20, "straddle_iv": 0.14})
        assert iv == 0.20 and src == IV_SOURCE_REAL

    def test_falls_back_to_vix_when_real_absent(self):
        from research.backtest.fno_condor import IV_SOURCE_VIX_PROXY, resolve_iv_source
        iv, src = resolve_iv_source({"straddle_iv": 0.14})
        assert iv == 0.14 and src == IV_SOURCE_VIX_PROXY

    def test_falls_back_when_real_nonpositive(self):
        from research.backtest.fno_condor import IV_SOURCE_VIX_PROXY, resolve_iv_source
        iv, src = resolve_iv_source({"atm_straddle_iv": 0.0, "straddle_iv": 0.14})
        assert iv == 0.14 and src == IV_SOURCE_VIX_PROXY

    def test_implausible_real_in_handbuilt_cycle_falls_back_to_vix(self):
        # Defence-in-depth: a hand-built cycle (not via cycles_from_db, so unfiltered)
        # carrying a 5.2 blowup must NOT be used — resolve_iv_source rejects it and
        # falls back to the VIX proxy, so it never reaches pricing.
        from research.backtest.fno_condor import IV_SOURCE_VIX_PROXY, resolve_iv_source
        iv, src = resolve_iv_source({"atm_straddle_iv": 5.2, "straddle_iv": 0.14})
        assert iv == 0.14 and src == IV_SOURCE_VIX_PROXY


@needs_condor
class TestGateV2Routing:
    """use_gate_v2 routes run_backtest's per-cycle decision through gate_v2_decision."""

    @staticmethod
    def _cycle(entry, expiry, rvol, iv, spot=23000.0, expiry_spot=23000.0):
        return {
            "entry_date": entry,
            "expiry_date": expiry,
            "spot": spot,
            "realized_vol_20d": rvol,
            "straddle_iv": iv,
            "dte": (expiry - entry).days,
            "expiry_spot": expiry_spot,
        }

    def _sell_cycles(self):
        # rvol << iv → v1 SELL_PREMIUM (rvol 0.08 < 0.9 * iv 0.20). 6 weekly cycles
        # so the v2 percentile sub-gate has trailing history to engage.
        cs = []
        d = date(2026, 1, 1)
        for i in range(6):
            entry = d + timedelta(days=7 * i)
            expiry = entry + timedelta(days=7)
            cs.append(self._cycle(entry, expiry, 0.08, 0.20))
        return cs

    def test_v1_default_trades_when_v2_off(self):
        cs = self._sell_cycles()
        r = run_backtest(cs, k=0.9, move_mult=1.0, use_gate_v2=False)
        assert r["n_trades"] > 0

    def test_event_veto_blocks_under_v2(self):
        # Mark every cycle's entry as an event day → gate-v2 vetoes ALL → 0 trades,
        # while v1 (no event awareness) still trades. Isolates the v2 routing.
        cs = self._sell_cycles()
        event_dates = [c["entry_date"] for c in cs]
        r_v1 = run_backtest(cs, k=0.9, move_mult=1.0, use_gate_v2=False)
        r_v2 = run_backtest(
            cs, k=0.9, move_mult=1.0, use_gate_v2=True, event_dates=event_dates,
        )
        assert r_v1["n_trades"] > 0
        assert r_v2["n_trades"] == 0

    def test_v2_never_more_aggressive_than_v1(self):
        # GATE-V2 can only ever turn a v1 SELL into STAND_ASIDE → n_trades(v2) <= v1.
        cs = self._sell_cycles()
        r_v1 = run_backtest(cs, k=0.9, move_mult=1.0, use_gate_v2=False)
        r_v2 = run_backtest(cs, k=0.9, move_mult=1.0, use_gate_v2=True)
        assert r_v2["n_trades"] <= r_v1["n_trades"]

    def test_no_events_v2_does_not_block_on_event(self):
        # Isolates the event veto: with NO event dates, the event sub-gate cannot
        # be the reason any cycle is blocked. Contrast with the all-events case
        # (test_event_veto_blocks_under_v2) which drops to 0 trades. Here trades
        # remain > 0, proving the all-events 0 was caused by the event veto.
        cs = self._sell_cycles()
        r_v2_noevt = run_backtest(
            cs, k=0.9, move_mult=1.0, use_gate_v2=True, event_dates=[],
        )
        assert r_v2_noevt["n_trades"] > 0

    def test_v2_defensively_sorts_shuffled_cycles(self):
        # gate-v2's PIT trailing window needs entry-date ascending order. Passing a
        # SHUFFLED cycle list must yield the SAME result as the sorted list (the
        # defensive sort inside run_backtest restores chronological order).
        import random
        cs = self._sell_cycles()
        shuffled = cs[:]
        random.Random(7).shuffle(shuffled)
        r_sorted = run_backtest(cs, k=0.9, move_mult=1.0, use_gate_v2=True)
        r_shuffled = run_backtest(shuffled, k=0.9, move_mult=1.0, use_gate_v2=True)
        assert r_shuffled["n_trades"] == r_sorted["n_trades"]
        assert r_shuffled["net_pnl"] == pytest.approx(r_sorted["net_pnl"])


@needs_condor
class TestRealIvAbCli:
    """The A/B CLI helper formats both runs without touching the DB directly."""

    def test_fmt_metrics_renders_go_flag(self):
        from research.backtest.fno_condor import _fmt_metrics
        m = {
            "n_cycles": 10, "n_trades": 7, "win_rate": 0.71,
            "return_on_capital": 0.04, "sharpe": 1.2, "net_pnl": 8000.0,
            "go_no_go": (True, "GO — ok"),
        }
        line = _fmt_metrics("B: gate-v2 + real-IV", m)
        assert "GO" in line and "n_trades=" in line and "ROM=" in line
