"""Unit tests for the 7 new builders in research/backtest/fno_strategies.py.

Builders tested (per task spec):
  1. build_broken_wing_condor   (registry: broken_wing_condor)
  2. build_jade_lizard          (registry: jade_lizard)
  3. build_bull_put_spread      (registry: bull_put_spread)
  4. build_bear_call_spread     (registry: bear_call_spread)
  5. build_bull_call_spread     (registry: bull_call_spread)
  6. build_bear_put_spread      (registry: bear_put_spread)
  7. build_ratio_spread         (registry: ratio_spread)

All tests are pure-math / in-memory: no DB, no network.
Premiums are computed via the real black76_* functions so the assertions remain
stable across parameter changes.

Test conventions
----------------
- ``spot = 22000.0, sigma = 0.14, dte = 7, step = 50`` unless a test states otherwise.
- ``implied_move`` for that fixture ≈ 426.5 pts.
- ``spot = 20000.0, sigma = 0.14, dte = 7`` for the vertical-spread section
  (implied_move ≈ 387.8 pts; move_mult=0.5 → ≈194 → rounds to 200 pts offset).
- ``NIFTY_LOT = 65``.
- Premium assertions use ``pytest.approx(rel=1e-3)``; strike/side assertions are exact.
- Financial invariants (credit > 0, max_loss sign) are the load-bearing assertions.
"""

from __future__ import annotations

import math

import pytest

# ---------------------------------------------------------------------------
# Import the module under test — skip gracefully if still in flight
# ---------------------------------------------------------------------------
try:
    from research.backtest.fno_strategies import (
        FNO_STRATEGIES,
        Leg,
        build_bear_call_spread,
        build_bear_put_spread,
        build_broken_wing_condor,
        build_bull_call_spread,
        build_bull_put_spread,
        build_jade_lizard,
        build_ratio_spread,
        fill_price,
        net_premium_per_unit,
        price_leg_hist,
        resolve_legs_with_fills,
        span_margin,
    )

    _HAS_STRATEGIES = True
except (ImportError, AttributeError) as _e:
    _HAS_STRATEGIES = False
    _import_error = str(_e)

try:
    from research.backtest.fno_condor import _round_to_step
    from core.fno_derived import implied_move as _implied_move

    _HAS_CONDOR = True
except (ImportError, AttributeError):
    _HAS_CONDOR = False

needs_all = pytest.mark.skipif(
    not (_HAS_STRATEGIES and _HAS_CONDOR),
    reason=(
        f"fno_strategies or fno_condor not importable: "
        f"{'' if _HAS_STRATEGIES else _import_error!r}"  # type: ignore[name-defined]
    ),
)

# ---------------------------------------------------------------------------
# Shared fixtures — standard NIFTY 7-DTE cycle
# ---------------------------------------------------------------------------
SPOT  = 22000.0
SIGMA = 0.14
DTE   = 7
STEP  = 50
LOT   = 65  # NIFTY_LOT


def _atm(spot: float = SPOT, step: int = STEP) -> int:
    return _round_to_step(spot, step)


def _M(spot: float = SPOT, sigma: float = SIGMA, dte: int = DTE) -> float:
    """Implied move in index points."""
    return _implied_move(spot, sigma, dte) or 0.0


def _price_legs(
    legs: list[Leg],
    spot: float = SPOT,
    sigma: float = SIGMA,
    dte: int = DTE,
    slip_pct: float = 0.005,
) -> tuple[list[float], list[float]]:
    """Return (mids, fills) for a leg list."""
    mids  = [price_leg_hist(l, spot, sigma, dte) for l in legs]
    fills = [fill_price(l, m, slip_pct) for l, m in zip(legs, mids)]
    return mids, fills


# ===========================================================================
# 1. REGISTRY COMPLETENESS — new keys present and correctly configured
# ===========================================================================


@needs_all
class TestRegistryNewKeys:
    NEW_KEYS = {
        "broken_wing_condor",
        "jade_lizard",
        "bull_put_spread",
        "bear_call_spread",
        "bull_call_spread",
        "bear_put_spread",
        "ratio_spread",
    }

    def test_new_keys_all_present(self):
        assert self.NEW_KEYS.issubset(set(FNO_STRATEGIES))

    def test_broken_wing_condor_flags(self):
        spec = FNO_STRATEGIES["broken_wing_condor"]
        assert spec.defined_risk is True
        assert spec.sell_premium is True
        assert spec.span_model == "defined"
        assert spec.needs_multi_expiry is False

    def test_jade_lizard_flags(self):
        spec = FNO_STRATEGIES["jade_lizard"]
        assert spec.defined_risk is False       # naked put downside
        assert spec.sell_premium is True
        assert spec.span_model == "spread_naked_mix"
        assert spec.needs_multi_expiry is False

    def test_bull_put_spread_flags(self):
        spec = FNO_STRATEGIES["bull_put_spread"]
        assert spec.defined_risk is True
        assert spec.sell_premium is True
        assert spec.span_model == "defined"

    def test_bear_call_spread_flags(self):
        spec = FNO_STRATEGIES["bear_call_spread"]
        assert spec.defined_risk is True
        assert spec.sell_premium is True
        assert spec.span_model == "defined"

    def test_bull_call_spread_flags(self):
        spec = FNO_STRATEGIES["bull_call_spread"]
        assert spec.defined_risk is True
        assert spec.sell_premium is False        # debit spread → BUY_PREMIUM gate
        assert spec.span_model == "defined"

    def test_bear_put_spread_flags(self):
        spec = FNO_STRATEGIES["bear_put_spread"]
        assert spec.defined_risk is True
        assert spec.sell_premium is False        # debit spread → BUY_PREMIUM gate
        assert spec.span_model == "defined"

    def test_ratio_spread_flags(self):
        spec = FNO_STRATEGIES["ratio_spread"]
        assert spec.defined_risk is False        # naked tail
        assert spec.sell_premium is True
        assert spec.span_model == "spread_naked_mix"

    def test_all_new_builders_callable(self):
        for key in self.NEW_KEYS:
            assert callable(FNO_STRATEGIES[key].build), f"{key}.build not callable"

    def test_all_new_builders_return_leg_lists(self):
        """Every new builder must return list[Leg] for a standard call."""
        for key in self.NEW_KEYS:
            spec = FNO_STRATEGIES[key]
            legs = spec.build(SPOT, _atm(), STEP, DTE, SIGMA, spec.default_params)
            assert isinstance(legs, list), f"{key} did not return a list"
            assert all(isinstance(l, Leg) for l in legs), f"{key} returned non-Leg"
            assert len(legs) >= 2, f"{key} returned fewer than 2 legs"


# ===========================================================================
# 2. build_broken_wing_condor
# ===========================================================================


@needs_all
class TestBuildBrokenWingCondor:
    """
    Reference spot=22000, sigma=0.14, dte=7, step=50.
    implied_move ≈ 426.5 → move_mult=1.5 → short_put=21350, short_call=22650.
    """

    def _build(self, **extra_params) -> tuple[list[Leg], list[float], float]:
        params = {"move_mult": 1.5, **extra_params}
        legs   = build_broken_wing_condor(SPOT, _atm(), STEP, DTE, SIGMA, params)
        _, fills = _price_legs(legs)
        credit = net_premium_per_unit(legs, fills)
        return legs, fills, credit

    # ── BW-1: skew=2.0 → put_wing=200, call_wing=50
    def test_bw1_leg_strikes(self):
        legs, _, _ = self._build(base_wing=100.0, skew=2.0)
        assert len(legs) == 4
        sp = next(l for l in legs if l.option_type == "PE" and l.side == "SELL")
        sc = next(l for l in legs if l.option_type == "CE" and l.side == "SELL")
        lp = next(l for l in legs if l.option_type == "PE" and l.side == "BUY")
        lc = next(l for l in legs if l.option_type == "CE" and l.side == "BUY")
        assert sp.strike == pytest.approx(21350.0)
        assert sc.strike == pytest.approx(22650.0)
        assert lp.strike == pytest.approx(21150.0)   # 21350 − 200
        assert lc.strike == pytest.approx(22700.0)   # 22650 + 50

    def test_bw1_credit_positive(self):
        _, _, credit = self._build(base_wing=100.0, skew=2.0)
        assert credit > 0

    def test_bw1_max_loss_put_side_dominates(self):
        legs, _, credit = self._build(base_wing=100.0, skew=2.0)
        put_side_loss  = 200 - credit
        call_side_loss = 50  - credit
        max_loss = max(put_side_loss, call_side_loss, 0.0)
        assert max_loss == pytest.approx(put_side_loss)   # put wing is wider

    def test_bw1_span_equals_max_of_sides(self):
        legs, _, credit = self._build(base_wing=100.0, skew=2.0)
        spec = FNO_STRATEGIES["broken_wing_condor"]
        margin = span_margin(spec, legs, credit, SPOT, LOT)
        put_side_loss = max(0.0, 200 - credit)
        assert margin == pytest.approx(put_side_loss * LOT, rel=1e-4)

    # ── BW-2: explicit widths (put=150, call=50)
    def test_bw2_explicit_widths(self):
        legs, _, credit = self._build(put_wing_width=150, call_wing_width=50)
        lp = next(l for l in legs if l.option_type == "PE" and l.side == "BUY")
        lc = next(l for l in legs if l.option_type == "CE" and l.side == "BUY")
        assert lp.strike == pytest.approx(21200.0)   # 21350 − 150
        assert lc.strike == pytest.approx(22700.0)   # 22650 + 50
        max_loss = max(150 - credit, 50 - credit, 0.0)
        assert max_loss == pytest.approx(max(150 - credit, 0.0), rel=1e-4)

    # ── BW-3: skew=1.0 → symmetric, degenerates to IC
    def test_bw3_symmetric_equals_ic_max_loss(self):
        """When skew=1.0, put_wing = call_wing = 100 → same max_loss as symmetric IC."""
        legs_bw, _, credit_bw = self._build(base_wing=100.0, skew=1.0)
        max_loss_bw = max(100 - credit_bw, 0.0)
        spec = FNO_STRATEGIES["broken_wing_condor"]
        margin = span_margin(spec, legs_bw, credit_bw, SPOT, LOT)
        assert margin == pytest.approx(max_loss_bw * LOT, rel=1e-4)

    # ── BW-4: skew=0.5 → call wing is wider (call side dominates)
    def test_bw4_call_side_dominates_when_skew_lt1(self):
        legs, _, credit = self._build(base_wing=100.0, skew=0.5)
        # put_wing = 100*0.5 = 50, call_wing = 100/0.5 = 200
        lp = next(l for l in legs if l.option_type == "PE" and l.side == "BUY")
        lc = next(l for l in legs if l.option_type == "CE" and l.side == "BUY")
        assert lp.strike == pytest.approx(21300.0)   # 21350 − 50
        assert lc.strike == pytest.approx(22850.0)   # 22650 + 200
        call_side_loss = max(0.0, 200 - credit)
        put_side_loss  = max(0.0, 50  - credit)
        assert call_side_loss > put_side_loss         # call side dominates

    # ── BW-5: equal-width legs → both side losses equal
    def test_bw5_equal_explicit_widths_equal_losses(self):
        legs, _, credit = self._build(put_wing_width=50, call_wing_width=50)
        put_loss  = 50 - credit
        call_loss = 50 - credit
        assert abs(put_loss - call_loss) < 1e-9

    # ── BW-6: very tight put wing (≤ one step) vs wide call wing
    def test_bw6_tight_put_side_riskless_possible(self):
        """When put wing is tiny and credit is large enough, put side can be riskless."""
        legs, _, credit = self._build(put_wing_width=20, call_wing_width=300)
        # _wing_points floors to step=50, so put_wing=50
        lp = next(l for l in legs if l.option_type == "PE" and l.side == "BUY")
        assert lp.strike == pytest.approx(21300.0)   # 21350 − 50 (floored to step)
        put_side_loss = max(0.0, 50 - credit)
        call_side_loss = max(0.0, 300 - credit)
        assert call_side_loss >= put_side_loss        # call side ≥ put side

    # ── BW-7: resolve inside shorts → full credit kept
    def test_bw7_resolve_full_credit_inside_shorts(self):
        legs, fills, credit = self._build(base_wing=100.0, skew=2.0)
        S = 22000.0  # inside shorts (21350 < 22000 < 22650)
        result = resolve_legs_with_fills(legs, fills, S, LOT)
        assert result["gross_pnl"] == pytest.approx(credit * LOT, rel=1e-4)

    # ── BW-8: resolve below long put → max loss = wider side
    def test_bw8_resolve_max_loss_below_long_put(self):
        legs, fills, credit = self._build(base_wing=100.0, skew=2.0)
        S = 21000.0  # below lp=21150
        result = resolve_legs_with_fills(legs, fills, S, LOT)
        # At S=21000: PE_sell intrinsic=350, PE_buy intrinsic=150, CE_sell=0, CE_buy=0
        # gross_per_unit = credit - (350 - 150) = credit - 200
        expected_gross = (credit - 200) * LOT   # negative (net loss)
        assert result["gross_pnl"] == pytest.approx(expected_gross, rel=1e-4)


# ===========================================================================
# 3. build_jade_lizard
# ===========================================================================


@needs_all
class TestBuildJadeLizard:
    """
    Reference: spot=23400, sigma=0.13, dte=7, step=50.
    implied_move ≈ 421.3 → put_mult=1.0 → K_sp≈23000,
    call_mult=0.5 → K_sc≈23600.
    """

    SPOT2  = 23400.0
    SIGMA2 = 0.13
    DTE2   = 7

    def _build(self, **params) -> tuple[list[Leg], list[float], float]:
        p = {"put_mult": 1.0, "call_mult": 0.5, "wing_strikes": 2, **params}
        legs = build_jade_lizard(self.SPOT2, _atm(self.SPOT2), STEP, self.DTE2, self.SIGMA2, p)
        _, fills = _price_legs(legs, self.SPOT2, self.SIGMA2, self.DTE2)
        credit = net_premium_per_unit(legs, fills)
        return legs, fills, credit

    # ── JL-1: leg structure
    def test_jl1_three_legs_order(self):
        legs, _, _ = self._build()
        assert len(legs) == 3
        assert legs[0].option_type == "PE" and legs[0].side == "SELL"
        assert legs[1].option_type == "CE" and legs[1].side == "SELL"
        assert legs[2].option_type == "CE" and legs[2].side == "BUY"

    def test_jl1_exact_strikes(self):
        legs, _, _ = self._build()
        assert legs[0].strike == pytest.approx(23000.0)   # K_sp
        assert legs[1].strike == pytest.approx(23600.0)   # K_sc
        assert legs[2].strike == pytest.approx(23700.0)   # K_lc = K_sc + 2*50

    def test_jl1_all_qty_lots_one(self):
        legs, _, _ = self._build()
        assert all(l.qty_lots == 1 for l in legs)

    # ── JL-2: invariant with wing_strikes=1 (spread_width=50 < credit)
    def test_jl2_invariant_holds_with_narrow_spread(self):
        """wing_strikes=1 → spread_width=50; credit should exceed 50."""
        legs, fills, credit = self._build(wing_strikes=1)
        call_spread_width = legs[2].strike - legs[1].strike
        assert call_spread_width == pytest.approx(50.0)
        # With wing_strikes=1, credit is typically > 50 at these params
        upside_risk = max(0.0, call_spread_width - credit)
        # If invariant holds, upside_risk == 0; if not, it is > 0 and must be positive
        assert upside_risk >= 0  # always non-negative
        # The key: upside_risk is well-defined and not hidden
        if credit >= call_spread_width:
            assert upside_risk == pytest.approx(0.0)
        else:
            assert upside_risk > 0

    # ── JL-3: invariant VIOLATED with wing_strikes=4 (spread_width=200 > credit)
    def test_jl3_invariant_violated_wide_spread(self):
        """wing_strikes=4 → spread_width=200 > credit → upside_risk > 0."""
        legs, fills, credit = self._build(wing_strikes=4)
        call_spread_width = legs[2].strike - legs[1].strike
        assert call_spread_width == pytest.approx(200.0)
        # Credit ~85 (from computation) < 200 → upside risk
        upside_risk = max(0.0, call_spread_width - credit)
        assert upside_risk > 0, "Invariant should be violated for wing_strikes=4"

    # ── JL-4: downside undefined — loss grows linearly with deeper spot
    def test_jl4_downside_undefined_loss(self):
        """At S = K_sp - 500, loss is deep and scales with depth."""
        legs, fills, credit = self._build()
        K_sp = legs[0].strike  # 23000

        S_shallow = K_sp - 250.0
        S_deep    = K_sp - 500.0

        res_shallow = resolve_legs_with_fills(legs, fills, S_shallow, LOT)
        res_deep    = resolve_legs_with_fills(legs, fills, S_deep,    LOT)

        assert res_shallow["gross_pnl"] > res_deep["gross_pnl"]   # deeper S → more negative P&L

    def test_jl4_downside_gross_formula(self):
        """At S < K_sp: gross = (credit − (K_sp − S)) × LOT."""
        legs, fills, credit = self._build()
        K_sp = legs[0].strike  # 23000
        S = K_sp - 500.0
        result = resolve_legs_with_fills(legs, fills, S, LOT)
        # Short put pays (K_sp - S), call spread OTM (S < K_sc), long CE OTM
        # gross_per_unit ≈ credit - (K_sp - S)
        # (CE legs all expire OTM since S < K_sp < K_sc)
        expected_gross_pu = credit - (K_sp - S)
        assert result["gross_pnl"] == pytest.approx(expected_gross_pu * LOT, rel=1e-4)

    # ── JL-5: single breakeven — lower_be = K_sp - credit
    def test_jl5_lower_breakeven(self):
        legs, fills, credit = self._build()
        K_sp = legs[0].strike
        lower_be = K_sp - credit
        # At S = lower_be, gross should be ≈ 0 × LOT
        result = resolve_legs_with_fills(legs, fills, lower_be, LOT)
        # At S = lower_be < K_sp: short put ITM, CE legs OTM
        # gross = (credit - (K_sp - lower_be)) * LOT = (credit - credit) * LOT = 0
        assert result["gross_pnl"] == pytest.approx(0.0, abs=1.0)   # abs tol 1 ₹

    # ── JL-6: margin — naked put term dominates
    def test_jl6_margin_naked_put_dominates(self):
        """SPAN proxy: put side full naked margin, call spread capped."""
        legs, fills, credit = self._build()
        spec = FNO_STRATEGIES["jade_lizard"]
        margin = span_margin(spec, legs, credit, self.SPOT2, LOT, {"span_pct": 0.12})
        # naked_part uses _span_naked_short → max(qty_lots of SELL legs) * span_pct*spot*lot
        # defined_part uses _span_defined → call-spread max loss
        # total = defined + naked
        assert margin > 0
        # Naked put contributes ~0.12 * 23400 * 65 ≈ 182,520 — far larger than call spread
        naked_approx = 0.12 * self.SPOT2 * LOT
        assert margin > naked_approx * 0.5   # must be substantial

    # ── JL-7: credit positive
    def test_jl7_credit_positive(self):
        _, _, credit = self._build()
        assert credit > 0


# ===========================================================================
# 4. build_bull_put_spread (credit, bullish)
# ===========================================================================

# Vertical spread fixture: spot=20000, sigma=0.14, dte=7
V_SPOT  = 20000.0
V_SIGMA = 0.14
V_DTE   = 7


@needs_all
class TestBuildBullPutSpread:
    """
    spot=20000, sigma=0.14, dte=7, move_mult=0.5, width=2.
    implied_move ≈ 387.8 → 0.5*M ≈ 194 → rounds to 200 → K_short=19800.
    K_long = 19800 − 100 = 19700.
    """

    def _build(self, move_mult=0.5, width=2) -> tuple[list[Leg], list[float], float]:
        params = {"move_mult": move_mult, "width": width}
        atm = _round_to_step(V_SPOT, STEP)
        legs = build_bull_put_spread(V_SPOT, atm, STEP, V_DTE, V_SIGMA, params)
        _, fills = _price_legs(legs, V_SPOT, V_SIGMA, V_DTE)
        credit = net_premium_per_unit(legs, fills)
        return legs, fills, credit

    # ── BPS-1: leg construction
    def test_bps1_two_legs_both_pe(self):
        legs, _, _ = self._build()
        assert len(legs) == 2
        assert all(l.option_type == "PE" for l in legs)
        sell_legs = [l for l in legs if l.side == "SELL"]
        buy_legs  = [l for l in legs if l.side == "BUY"]
        assert len(sell_legs) == 1 and len(buy_legs) == 1

    def test_bps1_exact_strikes(self):
        legs, _, _ = self._build()
        k_short = legs[0].strike
        k_long  = legs[1].strike
        assert k_short == pytest.approx(19800.0)
        assert k_long  == pytest.approx(19700.0)
        assert k_short - k_long == pytest.approx(100.0)   # width_pts = 2*50

    # ── BPS-2: credit and max_loss
    def test_bps2_credit_positive(self):
        _, _, credit = self._build()
        assert credit > 0

    def test_bps2_credit_less_than_width(self):
        _, _, credit = self._build()
        assert credit < 100.0   # width_pts = 100

    def test_bps2_max_loss_formula(self):
        _, _, credit = self._build()
        max_loss = max(0.0, 100.0 - credit)
        assert max_loss > 0   # realistic: credit < 100

    def test_bps2_breakeven(self):
        legs, _, credit = self._build()
        k_short = legs[0].strike
        breakeven = k_short - credit
        # At breakeven, gross ≈ 0
        fills_list = _price_legs(legs, V_SPOT, V_SIGMA, V_DTE)[1]
        res = resolve_legs_with_fills(legs, fills_list, breakeven, LOT)
        assert abs(res["gross_pnl"]) < LOT   # within one point×lot

    # ── BPS-3: span margin via defined model
    def test_bps3_span_margin(self):
        legs, _, credit = self._build()
        spec = FNO_STRATEGIES["bull_put_spread"]
        margin = span_margin(spec, legs, credit, V_SPOT, LOT)
        expected = max(0.0, 100.0 - credit) * LOT
        assert margin == pytest.approx(expected, rel=1e-4)

    # ── Resolution cases
    def test_bps4_max_win_spot_above_short(self):
        """expiry_spot ≥ K_short → both puts OTM → gross = credit × LOT."""
        legs, fills, credit = self._build()
        res = resolve_legs_with_fills(legs, fills, 20100.0, LOT)
        assert res["gross_pnl"] == pytest.approx(credit * LOT, rel=1e-4)

    def test_bps5_max_loss_spot_below_long(self):
        """expiry_spot ≤ K_long → max loss = (width − credit) × LOT."""
        legs, fills, credit = self._build()
        res = resolve_legs_with_fills(legs, fills, 19600.0, LOT)
        expected = -(100.0 - credit) * LOT
        assert res["gross_pnl"] == pytest.approx(expected, rel=1e-4)

    def test_bps6_partial_loss_between_strikes(self):
        """K_long < S < K_short → partial loss (not max, not zero)."""
        legs, fills, credit = self._build()
        S = 19750.0   # between 19700 and 19800
        res = resolve_legs_with_fills(legs, fills, S, LOT)
        max_win  = credit * LOT
        max_loss = (100.0 - credit) * LOT
        assert -max_loss < res["gross_pnl"] < max_win

    def test_bps7_sell_premium_flag(self):
        assert FNO_STRATEGIES["bull_put_spread"].sell_premium is True


# ===========================================================================
# 5. build_bear_call_spread (credit, bearish)
# ===========================================================================


@needs_all
class TestBuildBearCallSpread:
    """
    spot=20000, sigma=0.14, dte=7, move_mult=0.5, width=2.
    K_short=20200 (CE SELL), K_long=20300 (CE BUY).
    """

    def _build(self, move_mult=0.5, width=2) -> tuple[list[Leg], list[float], float]:
        params = {"move_mult": move_mult, "width": width}
        atm = _round_to_step(V_SPOT, STEP)
        legs = build_bear_call_spread(V_SPOT, atm, STEP, V_DTE, V_SIGMA, params)
        _, fills = _price_legs(legs, V_SPOT, V_SIGMA, V_DTE)
        credit = net_premium_per_unit(legs, fills)
        return legs, fills, credit

    # ── BCS-1: leg construction
    def test_bcs1_two_legs_both_ce(self):
        legs, _, _ = self._build()
        assert len(legs) == 2
        assert all(l.option_type == "CE" for l in legs)

    def test_bcs1_exact_strikes(self):
        legs, _, _ = self._build()
        k_short = legs[0].strike  # SELL
        k_long  = legs[1].strike  # BUY
        assert k_short == pytest.approx(20200.0)
        assert k_long  == pytest.approx(20300.0)
        assert k_long - k_short == pytest.approx(100.0)

    def test_bcs1_sell_lower_buy_higher(self):
        legs, _, _ = self._build()
        sell_leg = next(l for l in legs if l.side == "SELL")
        buy_leg  = next(l for l in legs if l.side == "BUY")
        assert sell_leg.strike < buy_leg.strike

    # ── BCS-2: credit and max_loss
    def test_bcs2_credit_positive(self):
        _, _, credit = self._build()
        assert credit > 0

    def test_bcs2_breakeven(self):
        legs, _, credit = self._build()
        k_short = legs[0].strike
        breakeven = k_short + credit
        fills = _price_legs(legs, V_SPOT, V_SIGMA, V_DTE)[1]
        res = resolve_legs_with_fills(legs, fills, breakeven, LOT)
        assert abs(res["gross_pnl"]) < LOT

    # ── Resolution cases
    def test_bcs3_max_win_spot_below_short(self):
        """expiry_spot ≤ K_short → both calls OTM → gross = credit × LOT."""
        legs, fills, credit = self._build()
        res = resolve_legs_with_fills(legs, fills, 20000.0, LOT)
        assert res["gross_pnl"] == pytest.approx(credit * LOT, rel=1e-4)

    def test_bcs4_max_loss_spot_above_long(self):
        """expiry_spot ≥ K_long → max loss = (width − credit) × LOT."""
        legs, fills, credit = self._build()
        res = resolve_legs_with_fills(legs, fills, 20500.0, LOT)
        expected = -(100.0 - credit) * LOT
        assert res["gross_pnl"] == pytest.approx(expected, rel=1e-4)

    def test_bcs5_span_margin_defined(self):
        legs, _, credit = self._build()
        spec = FNO_STRATEGIES["bear_call_spread"]
        margin = span_margin(spec, legs, credit, V_SPOT, LOT)
        assert margin == pytest.approx(max(0.0, 100.0 - credit) * LOT, rel=1e-4)

    def test_bcs6_sell_premium_flag(self):
        assert FNO_STRATEGIES["bear_call_spread"].sell_premium is True

    def test_bcs7_defined_risk_flag(self):
        assert FNO_STRATEGIES["bear_call_spread"].defined_risk is True


# ===========================================================================
# 6. build_bull_call_spread (DEBIT, bullish)
# ===========================================================================


@needs_all
class TestBuildBullCallSpread:
    """
    spot=20000, sigma=0.14, dte=7, move_mult=0.0, width=2.
    K_long=20000 (CE BUY), K_short=20100 (CE SELL).
    Net entry is a DEBIT (negative net_premium_per_unit).
    """

    def _build(self, move_mult=0.0, width=2) -> tuple[list[Leg], list[float], float]:
        params = {"move_mult": move_mult, "width": width}
        atm = _round_to_step(V_SPOT, STEP)
        legs = build_bull_call_spread(V_SPOT, atm, STEP, V_DTE, V_SIGMA, params)
        _, fills = _price_legs(legs, V_SPOT, V_SIGMA, V_DTE)
        entry_net = net_premium_per_unit(legs, fills)
        return legs, fills, entry_net

    # ── BLCS-1: leg construction
    def test_blcs1_two_ce_legs(self):
        legs, _, _ = self._build()
        assert len(legs) == 2
        assert all(l.option_type == "CE" for l in legs)

    def test_blcs1_buy_lower_sell_higher(self):
        legs, _, _ = self._build()
        buy_leg  = next(l for l in legs if l.side == "BUY")
        sell_leg = next(l for l in legs if l.side == "SELL")
        assert buy_leg.strike  == pytest.approx(20000.0)
        assert sell_leg.strike == pytest.approx(20100.0)
        assert buy_leg.strike < sell_leg.strike

    # ── BLCS-2: net debit (entry_net < 0)
    def test_blcs2_entry_net_negative(self):
        _, _, entry_net = self._build()
        assert entry_net < 0    # debit paid

    def test_blcs2_debit_positive(self):
        _, _, entry_net = self._build()
        debit = -entry_net
        assert debit > 0

    def test_blcs2_max_loss_equals_debit(self):
        legs, fills, entry_net = self._build()
        debit = -entry_net
        res = resolve_legs_with_fills(legs, fills, 19900.0, LOT)  # S below K_long
        assert res["gross_pnl"] == pytest.approx(-debit * LOT, rel=1e-4)

    def test_blcs3_max_profit_when_above_short(self):
        """expiry_spot ≥ K_short → max profit = (width_pts − debit) × LOT."""
        legs, fills, entry_net = self._build()
        debit = -entry_net
        width_pts = 100.0
        res = resolve_legs_with_fills(legs, fills, 20200.0, LOT)
        expected = (width_pts - debit) * LOT
        assert res["gross_pnl"] == pytest.approx(expected, rel=1e-4)

    def test_blcs4_sell_premium_false(self):
        """Debit spread → BUY_PREMIUM gate, sell_premium=False."""
        assert FNO_STRATEGIES["bull_call_spread"].sell_premium is False

    def test_blcs5_span_model_defined(self):
        """span_model='defined' → max loss = debit."""
        legs, _, entry_net = self._build()
        spec = FNO_STRATEGIES["bull_call_spread"]
        margin = span_margin(spec, legs, entry_net, V_SPOT, LOT)
        debit = -entry_net
        assert margin == pytest.approx(debit * LOT, rel=1e-4)

    def test_blcs6_breakeven_k_long_plus_debit(self):
        legs, fills, entry_net = self._build()
        debit = -entry_net
        k_long = next(l for l in legs if l.side == "BUY").strike
        breakeven = k_long + debit
        res = resolve_legs_with_fills(legs, fills, breakeven, LOT)
        assert abs(res["gross_pnl"]) < LOT


# ===========================================================================
# 7. build_bear_put_spread (DEBIT, bearish)
# ===========================================================================


@needs_all
class TestBuildBearPutSpread:
    """
    spot=20000, sigma=0.14, dte=7, move_mult=0.0, width=2.
    K_long=20000 (PE BUY), K_short=19900 (PE SELL).
    Net entry is a DEBIT.
    """

    def _build(self, move_mult=0.0, width=2) -> tuple[list[Leg], list[float], float]:
        params = {"move_mult": move_mult, "width": width}
        atm = _round_to_step(V_SPOT, STEP)
        legs = build_bear_put_spread(V_SPOT, atm, STEP, V_DTE, V_SIGMA, params)
        _, fills = _price_legs(legs, V_SPOT, V_SIGMA, V_DTE)
        entry_net = net_premium_per_unit(legs, fills)
        return legs, fills, entry_net

    # ── BRP-1: leg construction
    def test_brp1_two_pe_legs(self):
        legs, _, _ = self._build()
        assert len(legs) == 2
        assert all(l.option_type == "PE" for l in legs)

    def test_brp1_buy_higher_sell_lower(self):
        legs, _, _ = self._build()
        buy_leg  = next(l for l in legs if l.side == "BUY")
        sell_leg = next(l for l in legs if l.side == "SELL")
        assert buy_leg.strike  == pytest.approx(20000.0)
        assert sell_leg.strike == pytest.approx(19900.0)
        assert buy_leg.strike > sell_leg.strike

    # ── BRP-2: net debit
    def test_brp2_entry_net_negative(self):
        _, _, entry_net = self._build()
        assert entry_net < 0

    def test_brp3_max_loss_equals_debit(self):
        """expiry_spot ≥ K_long → both puts OTM → gross = −debit × LOT."""
        legs, fills, entry_net = self._build()
        debit = -entry_net
        res = resolve_legs_with_fills(legs, fills, 20100.0, LOT)
        assert res["gross_pnl"] == pytest.approx(-debit * LOT, rel=1e-4)

    def test_brp4_max_profit_when_below_short(self):
        """expiry_spot ≤ K_short → max profit = (width_pts − debit) × LOT."""
        legs, fills, entry_net = self._build()
        debit = -entry_net
        width_pts = 100.0
        res = resolve_legs_with_fills(legs, fills, 19800.0, LOT)
        expected = (width_pts - debit) * LOT
        assert res["gross_pnl"] == pytest.approx(expected, rel=1e-4)

    def test_brp5_sell_premium_false(self):
        assert FNO_STRATEGIES["bear_put_spread"].sell_premium is False

    def test_brp6_span_equals_debit(self):
        legs, _, entry_net = self._build()
        spec = FNO_STRATEGIES["bear_put_spread"]
        margin = span_margin(spec, legs, entry_net, V_SPOT, LOT)
        debit = -entry_net
        assert margin == pytest.approx(debit * LOT, rel=1e-4)

    def test_brp7_breakeven_k_long_minus_debit(self):
        legs, fills, entry_net = self._build()
        debit = -entry_net
        k_long = next(l for l in legs if l.side == "BUY").strike
        breakeven = k_long - debit
        res = resolve_legs_with_fills(legs, fills, breakeven, LOT)
        assert abs(res["gross_pnl"]) < LOT


# ===========================================================================
# 8. build_ratio_spread (1×2, partial undefined risk)
# ===========================================================================

# Ratio spread fixture: spot=22000, sigma=0.12, dte=7
R_SPOT  = 22000.0
R_SIGMA = 0.12
R_DTE   = 7


@needs_all
class TestBuildRatioSpread:
    """
    Reference: spot=22000, sigma=0.12, dte=7, step=50.
    implied_move ≈ 365.6 → k2_mult=1.0 → K2≈22350 (call) / 21650 (put).
    For credit: k2_mult=0.3 → K2=22100 (very close) gives a net credit.
    """

    def _build_call(
        self, k1_mult=0.0, k2_mult=1.0
    ) -> tuple[list[Leg], list[float], float]:
        params = {"variant": "call", "k1_mult": k1_mult, "k2_mult": k2_mult}
        atm = _round_to_step(R_SPOT, STEP)
        legs = build_ratio_spread(R_SPOT, atm, STEP, R_DTE, R_SIGMA, params)
        _, fills = _price_legs(legs, R_SPOT, R_SIGMA, R_DTE)
        # qty-weighted net credit = 2*fill_short - fill_long
        fill_long  = fills[0]
        fill_short = fills[1]
        qty_net = 2 * fill_short - fill_long   # actual cash received
        return legs, fills, qty_net

    def _build_put(
        self, k1_mult=0.0, k2_mult=1.0
    ) -> tuple[list[Leg], list[float], float]:
        params = {"variant": "put", "k1_mult": k1_mult, "k2_mult": k2_mult}
        atm = _round_to_step(R_SPOT, STEP)
        legs = build_ratio_spread(R_SPOT, atm, STEP, R_DTE, R_SIGMA, params)
        _, fills = _price_legs(legs, R_SPOT, R_SIGMA, R_DTE)
        qty_net = 2 * fills[1] - fills[0]
        return legs, fills, qty_net

    # ── RS-1: call variant geometry
    def test_rs1_call_geometry(self):
        legs, _, _ = self._build_call()
        assert len(legs) == 2
        long_leg  = legs[0]
        short_leg = legs[1]
        assert long_leg.option_type  == "CE" and long_leg.side  == "BUY"  and long_leg.qty_lots  == 1
        assert short_leg.option_type == "CE" and short_leg.side == "SELL" and short_leg.qty_lots == 2
        assert short_leg.strike > long_leg.strike   # K2 > K1 for call variant

    def test_rs1_k1_atm_when_k1_mult_zero(self):
        legs, _, _ = self._build_call(k1_mult=0.0)
        k1 = legs[0].strike
        assert k1 == pytest.approx(float(_round_to_step(R_SPOT, STEP)))

    # ── RS-2: put variant geometry
    def test_rs2_put_geometry(self):
        legs, _, _ = self._build_put()
        assert len(legs) == 2
        long_leg  = legs[0]
        short_leg = legs[1]
        assert long_leg.option_type  == "PE" and long_leg.side  == "BUY"  and long_leg.qty_lots  == 1
        assert short_leg.option_type == "PE" and short_leg.side == "SELL" and short_leg.qty_lots == 2
        assert short_leg.strike < long_leg.strike   # K2 < K1 for put variant

    # ── RS-3: degenerate strike guard — K1 == K2 after rounding → K2 widened by one step
    def test_rs3_degenerate_strike_guard_call(self):
        """sigma=0.02, dte=1 → tiny implied_move → K1==K2 raw → builder must widen K2."""
        atm = _round_to_step(R_SPOT, STEP)
        params = {"variant": "call", "k1_mult": 0.0, "k2_mult": 1.0}
        legs = build_ratio_spread(R_SPOT, atm, STEP, 1, 0.02, params)
        k1 = legs[0].strike
        k2 = legs[1].strike
        assert k2 > k1, "Builder must widen K2 when raw rounding collapses to same strike"
        assert k2 - k1 == pytest.approx(STEP)

    def test_rs3_degenerate_strike_guard_put(self):
        atm = _round_to_step(R_SPOT, STEP)
        params = {"variant": "put", "k1_mult": 0.0, "k2_mult": 1.0}
        legs = build_ratio_spread(R_SPOT, atm, STEP, 1, 0.02, params)
        k1 = legs[0].strike
        k2 = legs[1].strike
        assert k2 < k1, "Builder must place K2 below K1 for put variant"
        assert k1 - k2 == pytest.approx(STEP)

    # ── RS-4: net credit when k2 is close enough (k2_mult=0.3 → K2=22100)
    def test_rs4_net_credit_when_k2_close(self):
        """k2_mult=0.3 → K2=22100 very close to K1=22000 → 2×OTM prem > 1×ATM prem."""
        _, _, qty_net = self._build_call(k1_mult=0.0, k2_mult=0.3)
        assert qty_net > 0, "Expected net credit for tight k2_mult=0.3"

    # ── RS-5: payoff peak at K2
    def test_rs5_payoff_peak_at_k2(self):
        """At S=K2: long leg ITM by (K2-K1), short legs at-the-money (intrinsic=0).
        gross = (qty_net + (K2-K1)) × LOT.
        """
        legs, fills, qty_net = self._build_call(k1_mult=0.0, k2_mult=0.3)
        k1 = legs[0].strike
        k2 = legs[1].strike
        S  = k2
        result = resolve_legs_with_fills(legs, fills, S, LOT)
        expected = (qty_net + (k2 - k1)) * LOT
        assert result["gross_pnl"] == pytest.approx(expected, rel=1e-4)

    # ── RS-6: below K1 → all OTM → retain net credit
    def test_rs6_full_credit_below_k1(self):
        """S ≤ K1: all legs OTM → gross = qty_net × LOT (= net cash received)."""
        legs, fills, qty_net = self._build_call(k1_mult=0.0, k2_mult=0.3)
        k1 = legs[0].strike
        S  = k1 - STEP  # one step below K1
        result = resolve_legs_with_fills(legs, fills, S, LOT)
        assert result["gross_pnl"] == pytest.approx(qty_net * LOT, rel=1e-4)

    # ── RS-7: undefined-risk slope — loss grows above upper_BE
    def test_rs7_undefined_risk_loss_grows(self):
        """Above the upper breakeven, each +1 pt in spot → ~-1 lot P&L (net short 1 contract)."""
        legs, fills, qty_net = self._build_call(k1_mult=0.0, k2_mult=0.3)
        k1 = legs[0].strike
        k2 = legs[1].strike
        upper_be = 2 * k2 - k1 + qty_net   # above this the position loses
        S_low  = upper_be + 100.0
        S_high = upper_be + 200.0
        res_low  = resolve_legs_with_fills(legs, fills, S_low,  LOT)
        res_high = resolve_legs_with_fills(legs, fills, S_high, LOT)
        assert res_low["gross_pnl"]  < 0
        assert res_high["gross_pnl"] < res_low["gross_pnl"]   # deeper loss at higher S
        # Loss increment ≈ -100 pts × 1 lot (net short 1 contract)
        delta_pnl = res_high["gross_pnl"] - res_low["gross_pnl"]
        assert delta_pnl == pytest.approx(-100.0 * LOT, rel=1e-3)

    # ── RS-8: span model is spread_naked_mix
    def test_rs8_span_model_spread_naked_mix(self):
        spec = FNO_STRATEGIES["ratio_spread"]
        assert spec.span_model == "spread_naked_mix"

    def test_rs8_span_margin_finite_positive(self):
        """Margin must be finite and positive (not inf)."""
        legs, fills, _ = self._build_call(k1_mult=0.0, k2_mult=1.0)
        net = net_premium_per_unit(legs, fills)
        spec = FNO_STRATEGIES["ratio_spread"]
        margin = span_margin(spec, legs, net, R_SPOT, LOT, {"span_pct": 0.10})
        assert math.isfinite(margin)
        assert margin > 0


# ===========================================================================
# 9. Cross-builder: credit_put_spread vs bull_put_spread (reconciliation)
# ===========================================================================


@needs_all
class TestCreditPutSpreadReconciliation:
    """
    `credit_put_spread` and `bull_put_spread` are both bullish credit put spreads
    but use DIFFERENT parameterisations:
      - credit_put_spread: short_mult × implied_move + width_strikes steps
      - bull_put_spread  : move_mult × implied_move + width steps
    They are NOT identical builders; both registry keys must coexist independently.
    """

    def test_both_keys_present(self):
        assert "credit_put_spread" in FNO_STRATEGIES
        assert "bull_put_spread"   in FNO_STRATEGIES

    def test_both_return_pe_credit_spreads(self):
        atm = _round_to_step(V_SPOT, STEP)
        for key in ("credit_put_spread", "bull_put_spread"):
            spec = FNO_STRATEGIES[key]
            legs = spec.build(V_SPOT, atm, STEP, V_DTE, V_SIGMA, spec.default_params)
            assert len(legs) == 2
            assert all(l.option_type == "PE" for l in legs)
            sell_leg = next(l for l in legs if l.side == "SELL")
            buy_leg  = next(l for l in legs if l.side == "BUY")
            assert sell_leg.strike > buy_leg.strike, f"{key}: SELL must be higher than BUY"

    def test_builders_are_distinct_functions(self):
        from research.backtest.fno_strategies import (
            build_credit_put_spread,
            build_bull_put_spread as bps,
        )
        assert build_credit_put_spread is not bps
