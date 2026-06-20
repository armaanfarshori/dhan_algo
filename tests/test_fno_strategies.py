"""Unit tests for research/backtest/fno_strategies.py — multi-leg options engine.

All tests are pure-math / in-memory: no DB, no network.
Inputs are synthesised; premiums validated with the real black76_* functions.

Test map (§10 of _ENGINE.md):
  T1  — Leg.signed
  T2  — build_short_straddle
  T3  — build_iron_condor ≡ build_condor
  T4  — leg_intrinsic
  T5  — net_premium_per_unit sign convention
  T6  — resolve_legs short straddle max-win
  T7  — resolve_legs short straddle loss
  T8  — resolve_legs_with_fills unequal qty (ratio spread)
  T9  — condor equivalence (resolve_legs_with_fills ≈ resolve_condor)
  T10 — span_margin defined (condor + debit)
  T11 — span_margin naked_short (straddle convention)
  T12 — run_strategy_backtest gate routing + ROM keys
  T13 — multi-expiry refusal (calendar_spread)
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Module under test — skip gracefully if the module itself is still in flight
# ---------------------------------------------------------------------------
try:
    from research.backtest.fno_strategies import (
        FNO_STRATEGIES,
        Leg,
        StrategySpec,
        build_credit_put_spread,
        build_iron_condor,
        build_iron_fly,
        build_long_straddle,
        build_short_straddle,
        build_short_strangle,
        fill_price,
        leg_intrinsic,
        net_premium_per_unit,
        price_leg_hist,
        resolve_legs,
        resolve_legs_with_fills,
        run_strategy_backtest,
        span_margin,
    )

    _HAS_STRATEGIES = True
except (ImportError, AttributeError) as _e:
    _HAS_STRATEGIES = False
    _import_error = str(_e)

needs_strategies = pytest.mark.skipif(
    not _HAS_STRATEGIES,
    reason=f"fno_strategies not importable: {'' if _HAS_STRATEGIES else _import_error!r}",  # type: ignore[name-defined]
)

# Also import condor primitives for equivalence tests
try:
    from research.backtest.fno_condor import (
        black76_call,
        black76_put,
        build_condor,
        resolve_condor,
    )
    from research.backtest.fno_costs import NIFTY_LOT

    _HAS_CONDOR = True
except (ImportError, AttributeError):
    _HAS_CONDOR = False

needs_condor = pytest.mark.skipif(
    not _HAS_CONDOR,
    reason="fno_condor not importable",
)


# ===========================================================================
# T1 — Leg.signed
# ===========================================================================


@needs_strategies
class TestLegSigned:
    def test_sell_ce_positive(self):
        assert Leg("CE", "SELL", 24000).signed() == +1

    def test_sell_pe_positive(self):
        assert Leg("PE", "SELL", 24000).signed() == +1

    def test_buy_ce_negative(self):
        assert Leg("CE", "BUY", 24000).signed() == -1

    def test_buy_pe_negative(self):
        assert Leg("PE", "BUY", 24000).signed() == -1

    def test_frozen_dataclass(self):
        """Leg is frozen — mutation must raise."""
        leg = Leg("CE", "SELL", 24000, 1)
        with pytest.raises((AttributeError, TypeError)):
            leg.strike = 24100  # type: ignore[misc]

    def test_default_qty_lots(self):
        leg = Leg("CE", "SELL", 24000)
        assert leg.qty_lots == 1


# ===========================================================================
# T2 — build_short_straddle
# ===========================================================================


@needs_strategies
class TestBuildShortStraddle:
    def test_two_legs_both_sell_atm(self):
        spot = 24013.0
        atm = 24000
        legs = build_short_straddle(spot, atm, 50, 7, 0.15, {})
        assert len(legs) == 2
        assert all(leg.side == "SELL" for leg in legs)
        assert all(leg.strike == 24000 for leg in legs)

    def test_types_ce_pe(self):
        legs = build_short_straddle(24013.0, 24000, 50, 7, 0.15, {})
        types = {leg.option_type for leg in legs}
        assert types == {"CE", "PE"}

    def test_qty_lots_default_one(self):
        legs = build_short_straddle(24000.0, 24000, 50, 7, 0.15, {})
        assert all(leg.qty_lots == 1 for leg in legs)


# ===========================================================================
# T3 — build_iron_condor ≡ build_condor
# ===========================================================================


@pytest.mark.skipif(
    not (_HAS_STRATEGIES and _HAS_CONDOR),
    reason="requires both fno_strategies and fno_condor",
)
class TestBuildIronCondorEquivalence:
    """build_iron_condor must produce strikes identical to build_condor."""

    def test_strike_equivalence(self):
        spot = 24000.0
        step = 50
        move_mult = 1.5
        wing_strikes = 2
        # M ≈ implied_move(24000, 0.15, 7) but build_iron_condor uses it internally
        # We derive expected strikes via build_condor directly
        import math

        M = spot * 0.15 * math.sqrt(7 / 365)
        ref = build_condor(spot, M, move_mult=move_mult, wing_strikes=wing_strikes, step=step)

        legs = build_iron_condor(spot, int(spot), step, 7, 0.15, {
            "move_mult": move_mult,
            "wing_strikes": wing_strikes,
        })

        # Collect strikes by role
        pe_sell = [l for l in legs if l.option_type == "PE" and l.side == "SELL"]
        pe_buy  = [l for l in legs if l.option_type == "PE" and l.side == "BUY"]
        ce_sell = [l for l in legs if l.option_type == "CE" and l.side == "SELL"]
        ce_buy  = [l for l in legs if l.option_type == "CE" and l.side == "BUY"]

        assert len(pe_sell) == 1 and pe_sell[0].strike == ref["short_put_k"]
        assert len(pe_buy)  == 1 and pe_buy[0].strike  == ref["long_put_k"]
        assert len(ce_sell) == 1 and ce_sell[0].strike == ref["short_call_k"]
        assert len(ce_buy)  == 1 and ce_buy[0].strike  == ref["long_call_k"]

    def test_four_legs(self):
        legs = build_iron_condor(24000.0, 24000, 50, 7, 0.15, {})
        assert len(legs) == 4


# ===========================================================================
# T4 — leg_intrinsic
# ===========================================================================


@needs_strategies
class TestLegIntrinsic:
    def test_ce_itm(self):
        leg = Leg("CE", "SELL", 24000.0)
        assert leg_intrinsic(leg, 24250.0) == pytest.approx(250.0)

    def test_ce_otm(self):
        leg = Leg("CE", "SELL", 24000.0)
        assert leg_intrinsic(leg, 23800.0) == pytest.approx(0.0)

    def test_pe_otm_above(self):
        leg = Leg("PE", "SELL", 24000.0)
        assert leg_intrinsic(leg, 24250.0) == pytest.approx(0.0)

    def test_pe_itm(self):
        leg = Leg("PE", "SELL", 24000.0)
        assert leg_intrinsic(leg, 23800.0) == pytest.approx(200.0)

    def test_ce_exactly_atm(self):
        leg = Leg("CE", "SELL", 24000.0)
        assert leg_intrinsic(leg, 24000.0) == pytest.approx(0.0)

    def test_pe_exactly_atm(self):
        leg = Leg("PE", "SELL", 24000.0)
        assert leg_intrinsic(leg, 24000.0) == pytest.approx(0.0)

    def test_intrinsic_always_nonneg(self):
        for S in [20000, 24000, 28000]:
            assert leg_intrinsic(Leg("CE", "BUY", 24000.0), S) >= 0
            assert leg_intrinsic(Leg("PE", "BUY", 24000.0), S) >= 0


# ===========================================================================
# T5 — net_premium_per_unit sign convention
# ===========================================================================


@needs_strategies
class TestNetPremiumSign:
    def test_short_straddle_credit(self):
        """SELL CE + SELL PE → net positive (credit received)."""
        legs = [Leg("CE", "SELL", 24000.0), Leg("PE", "SELL", 24000.0)]
        fills = [120.0, 110.0]
        result = net_premium_per_unit(legs, fills)
        assert result == pytest.approx(+230.0)

    def test_long_straddle_debit(self):
        """BUY CE + BUY PE → net negative (debit paid)."""
        legs = [Leg("CE", "BUY", 24000.0), Leg("PE", "BUY", 24000.0)]
        fills = [120.0, 110.0]
        result = net_premium_per_unit(legs, fills)
        assert result == pytest.approx(-230.0)

    def test_iron_condor_is_net_credit(self):
        """A well-priced iron condor should produce positive net premium."""
        legs = [
            Leg("PE", "SELL", 23700.0),
            Leg("PE", "BUY",  23600.0),
            Leg("CE", "SELL", 24300.0),
            Leg("CE", "BUY",  24400.0),
        ]
        fills = [80.0, 40.0, 80.0, 40.0]  # shorts > longs
        result = net_premium_per_unit(legs, fills)
        # (+80 - 40) + (+80 - 40) = 80
        assert result == pytest.approx(80.0)


# ===========================================================================
# T6 — resolve_legs short straddle max-win (pin scenario)
# ===========================================================================


@needs_strategies
class TestResolveLegsMaxWin:
    def test_pin_both_expire_worthless(self):
        """Shorts @24000, S=24000 (pin) → both legs OTM → keep full credit."""
        legs = [Leg("CE", "SELL", 24000.0), Leg("PE", "SELL", 24000.0)]
        entry_net = 230.0  # credit per unit
        # resolve_legs uses the 1-lot-per-leg convention
        result = resolve_legs(legs, entry_net, 24000.0, lot=NIFTY_LOT)
        expected_gross = 230.0 * NIFTY_LOT  # 14950
        assert result["gross_pnl"] == pytest.approx(expected_gross)

    def test_pin_gross_equals_credit(self):
        lot = 65
        legs = [Leg("CE", "SELL", 24000.0), Leg("PE", "SELL", 24000.0)]
        entry_net = 230.0
        result = resolve_legs(legs, entry_net, 24000.0, lot=lot)
        assert result["gross_pnl"] == pytest.approx(entry_net * lot)


# ===========================================================================
# T7 — resolve_legs short straddle with loss
# ===========================================================================


@needs_strategies
class TestResolveLegsLoss:
    def test_straddle_ce_itm_at_expiry(self):
        """S=24500 → CE intrinsic 500, PE intrinsic 0.
        gross_per_unit = 230 − (SELL_CE×500 + SELL_PE×0) = 230 − 500 = −270
        gross_pnl = −270 × 65 = −17550.
        """
        legs = [Leg("CE", "SELL", 24000.0), Leg("PE", "SELL", 24000.0)]
        entry_net = 230.0
        result = resolve_legs(legs, entry_net, 24500.0, lot=NIFTY_LOT)
        # gross_per_unit = 230 - (+1*500 + +1*0) = -270
        expected = -270.0 * NIFTY_LOT
        assert result["gross_pnl"] == pytest.approx(expected)

    def test_straddle_pe_itm_at_expiry(self):
        """S=23700 → PE intrinsic 300, CE intrinsic 0."""
        legs = [Leg("CE", "SELL", 24000.0), Leg("PE", "SELL", 24000.0)]
        entry_net = 230.0
        result = resolve_legs(legs, entry_net, 23700.0, lot=NIFTY_LOT)
        # gross_per_unit = 230 - (+1*0 + +1*300) = -70
        expected = -70.0 * NIFTY_LOT
        assert result["gross_pnl"] == pytest.approx(expected)


# ===========================================================================
# T8 — resolve_legs_with_fills unequal qty (ratio spread)
# ===========================================================================


@needs_strategies
class TestResolveLegsUnequalQty:
    """Ratio spread: SELL 1×CE@24000, BUY 2×CE@24200. S=24500."""

    def test_ratio_spread_leg_by_leg_weighting(self):
        """
        SELL 1 lot CE@24000: fill=300, intrinsic=500
          leg PnL = (+1) * (300 − 500) * 1 * 65 = −200 * 65 = −13000

        BUY 2 lots CE@24200: fill=180, intrinsic=300
          leg PnL = (−1) * (180 − 300) * 2 * 65 = (−1)*(−120)*130 = +15600

        Total gross = −13000 + 15600 = +2600
        """
        legs = [
            Leg("CE", "SELL", 24000.0, qty_lots=1),
            Leg("CE", "BUY",  24200.0, qty_lots=2),
        ]
        fills = [300.0, 180.0]
        S = 24500.0
        lot = 65

        result = resolve_legs_with_fills(legs, fills, S, lot=lot)

        short_pnl = (+1) * (300.0 - 500.0) * 1 * lot  # -13000
        long_pnl  = (-1) * (180.0 - 300.0) * 2 * lot  # +15600
        expected = short_pnl + long_pnl

        assert result["gross_pnl"] == pytest.approx(expected)

    def test_ratio_spread_values(self):
        legs = [
            Leg("CE", "SELL", 24000.0, qty_lots=1),
            Leg("CE", "BUY",  24200.0, qty_lots=2),
        ]
        fills = [300.0, 180.0]
        result = resolve_legs_with_fills(legs, fills, 24500.0, lot=65)
        assert result["gross_pnl"] == pytest.approx(2600.0)


# ===========================================================================
# T9 — Condor equivalence: resolve_legs_with_fills ≈ resolve_condor
# ===========================================================================


@pytest.mark.skipif(
    not (_HAS_STRATEGIES and _HAS_CONDOR),
    reason="requires both fno_strategies and fno_condor",
)
class TestCondorEquivalence:
    """resolve_legs_with_fills on the 4 iron-condor legs (all qty_lots=1)
    must equal resolve_condor['gross_pnl'] to within float tolerance."""

    def _make_condor_legs_and_strikes(self, spot=24000.0, sigma=0.15, dte=7):
        import math

        M = spot * sigma * math.sqrt(dte / 365)
        strikes = build_condor(spot, M, move_mult=1.5, wing_strikes=2, step=50)
        T = dte / 365.0
        sp = black76_put(spot,  strikes["short_put_k"],  T, sigma)
        lp = black76_put(spot,  strikes["long_put_k"],   T, sigma)
        sc = black76_call(spot, strikes["short_call_k"], T, sigma)
        lc = black76_call(spot, strikes["long_call_k"],  T, sigma)

        # Credit per unit (no slippage for the equivalence test)
        credit = (sp + sc) - (lp + lc)

        legs = [
            Leg("PE", "SELL", float(strikes["short_put_k"]), 1),
            Leg("PE", "BUY",  float(strikes["long_put_k"]),  1),
            Leg("CE", "SELL", float(strikes["short_call_k"]), 1),
            Leg("CE", "BUY",  float(strikes["long_call_k"]),  1),
        ]
        fills = [sp, lp, sc, lc]  # mid (no slippage for purity)
        return legs, fills, strikes, credit

    @pytest.mark.parametrize("expiry_spot", [22000, 23700, 24000, 24300, 26000])
    def test_gross_pnl_matches_resolve_condor(self, expiry_spot):
        spot = 24000.0
        legs, fills, strikes, credit = self._make_condor_legs_and_strikes(spot)

        new_result = resolve_legs_with_fills(legs, fills, float(expiry_spot), lot=NIFTY_LOT)
        old_result = resolve_condor(strikes, credit, float(expiry_spot), lot=NIFTY_LOT)

        assert new_result["gross_pnl"] == pytest.approx(
            old_result["gross_pnl"], rel=1e-6
        ), (
            f"At expiry_spot={expiry_spot}: "
            f"new={new_result['gross_pnl']:.4f} old={old_result['gross_pnl']:.4f}"
        )


# ===========================================================================
# T10 — span_margin defined (condor + debit)
# ===========================================================================


@needs_strategies
class TestSpanMarginDefined:
    def _condor_spec(self) -> StrategySpec:
        return FNO_STRATEGIES["iron_condor"]

    def test_credit_condor_wing_100pts(self):
        """Wing width 100 pts, entry credit 40/unit → max_loss = 60/unit → 60×65 = 3900."""
        spec = self._condor_spec()
        legs = [
            Leg("PE", "SELL", 23700.0, 1),
            Leg("PE", "BUY",  23600.0, 1),
            Leg("CE", "SELL", 24300.0, 1),
            Leg("CE", "BUY",  24400.0, 1),
        ]
        entry_net = 40.0  # credit
        margin = span_margin(spec, legs, entry_net, spot=24000.0, lot=65)
        # wing_width = max(|24400 - 24300|, |23700 - 23600|) = 100
        # max_loss_per_unit = max(0, 100 - 40) = 60
        # span = 60 * 65 = 3900
        assert margin == pytest.approx(3900.0)

    def test_debit_fly(self):
        """Long fly (debit): net_debit = 30/unit → span = 30×65 = 1950."""
        spec = FNO_STRATEGIES["long_straddle"]  # use any defined-risk spec
        legs = [
            Leg("CE", "BUY", 24000.0, 1),
            Leg("PE", "BUY", 24000.0, 1),
        ]
        entry_net = -30.0  # debit
        margin = span_margin(spec, legs, entry_net, spot=24000.0, lot=65)
        assert margin == pytest.approx(30.0 * 65)

    def test_credit_exactly_at_wing(self):
        """If credit == wing_width, max_loss = 0 → span = 0 (unusual edge case)."""
        spec = self._condor_spec()
        legs = [
            Leg("PE", "SELL", 23700.0, 1),
            Leg("PE", "BUY",  23600.0, 1),
            Leg("CE", "SELL", 24300.0, 1),
            Leg("CE", "BUY",  24400.0, 1),
        ]
        entry_net = 100.0  # equals wing width
        margin = span_margin(spec, legs, entry_net, spot=24000.0, lot=65)
        assert margin == pytest.approx(0.0)


# ===========================================================================
# T11 — span_margin naked_short (net-uncovered-lots convention)
# ===========================================================================


@needs_strategies
class TestSpanMarginNakedShort:
    """Short straddle: 1×CE + 1×PE both SELL.

    Convention (fix #4): naked lot count = NET uncovered shorts per option_type
    = max(0, Σ SELL qty − Σ BUY qty) for that type, summed across types.

    For a straddle (no BUY legs): CE net=1, PE net=1, total naked=2.
    span = 0.12 × 24000 × 65 × 2 = 374400.

    For a strangle with 2 lots each: CE net=2, PE net=2, total naked=4.
    span = 0.12 × 24000 × 65 × 4 = 748800.
    """

    def test_straddle_combined_block(self):
        """Straddle: net uncovered = CE(1-0) + PE(1-0) = 2 naked lots."""
        spec = FNO_STRATEGIES["short_straddle"]
        legs = [
            Leg("CE", "SELL", 24000.0, 1),
            Leg("PE", "SELL", 24000.0, 1),
        ]
        entry_net = 230.0
        margin = span_margin(
            spec, legs, entry_net, spot=24000.0, lot=65,
            params={"span_pct": 0.12},
        )
        expected = 0.12 * 24000.0 * 65 * 2  # 2 net uncovered lots = 374400
        assert margin == pytest.approx(expected)

    def test_straddle_default_span_pct(self):
        """Default span_pct from StrategySpec.default_params — same net-uncovered logic."""
        spec = FNO_STRATEGIES["short_straddle"]
        legs = [
            Leg("CE", "SELL", 24000.0, 1),
            Leg("PE", "SELL", 24000.0, 1),
        ]
        # No explicit params — uses spec.default_params["span_pct"] = 0.12
        margin = span_margin(spec, legs, 230.0, spot=24000.0, lot=65)
        assert margin == pytest.approx(0.12 * 24000.0 * 65 * 2)

    def test_strangle_two_lots(self):
        """Strangle with 2 lots per short leg → net uncovered = 2+2=4 → span×4."""
        spec = FNO_STRATEGIES["short_strangle"]
        legs = [
            Leg("CE", "SELL", 24300.0, 2),
            Leg("PE", "SELL", 23700.0, 2),
        ]
        margin = span_margin(
            spec, legs, 150.0, spot=24000.0, lot=65, params={"span_pct": 0.12}
        )
        expected = 0.12 * 24000.0 * 65 * 4  # 4 net uncovered lots = 748800
        assert margin == pytest.approx(expected)


# ===========================================================================
# T12 — run_strategy_backtest gate routing + ROM keys
# ===========================================================================


@needs_strategies
class TestRunStrategyBacktest:
    """Uses 3 synthetic cycles with controlled gate outcomes.

    Day-count fix #2: the gate rebases the √252 realized vol onto the calendar
    (365) basis (× √(252/365) ≈ 0.8307) BEFORE comparing to the √365 implied
    vol. All gate boundaries below are stated on the REBASED realized vol.
    iv=0.15 → SELL when rebased_rv < 0.135, BUY when rebased_rv > 0.15.

    rv=0.10 → rebased 0.0831 < 0.135               → SELL_PREMIUM
    rv=0.20 → rebased 0.1661 > 0.15                → BUY_PREMIUM
    rv=0.17 → rebased 0.1412 ∈ [0.135, 0.15]       → STAND_ASIDE
    """

    @staticmethod
    def _make_cycle(rv: float, iv: float, spot=24000.0, dte=7, expiry_spot=24050.0) -> dict[str, Any]:
        return {
            "entry_date":       date(2025, 1, 2),
            "expiry_date":      date(2025, 1, 9),
            "spot":             spot,
            "straddle_iv":      iv,
            "realized_vol_20d": rv,
            "dte":              dte,
            "expiry_spot":      expiry_spot,
        }

    @property
    def _three_cycles(self) -> list[dict[str, Any]]:
        return [
            self._make_cycle(rv=0.10, iv=0.15),   # SELL_PREMIUM
            self._make_cycle(rv=0.20, iv=0.15),   # BUY_PREMIUM
            self._make_cycle(rv=0.17, iv=0.15),   # STAND_ASIDE
        ]

    def test_iron_condor_n_trades_one(self):
        """sell_premium → only the SELL_PREMIUM cycle trades → n_trades == 1."""
        spec = FNO_STRATEGIES["iron_condor"]
        metrics = run_strategy_backtest(spec, self._three_cycles, k=0.9)
        assert metrics["n_trades"] == 1

    def test_iron_condor_has_rom_and_mean_span(self):
        spec = FNO_STRATEGIES["iron_condor"]
        metrics = run_strategy_backtest(spec, self._three_cycles, k=0.9)
        assert "return_on_margin" in metrics
        assert "mean_span" in metrics

    def test_iron_condor_go_no_go_present(self):
        spec = FNO_STRATEGIES["iron_condor"]
        metrics = run_strategy_backtest(spec, self._three_cycles, k=0.9)
        assert "go_no_go" in metrics
        go, reason = metrics["go_no_go"]
        assert isinstance(go, bool)
        assert isinstance(reason, str)

    def test_long_straddle_n_trades_one(self):
        """sell_premium=False → only the BUY_PREMIUM cycle trades → n_trades == 1."""
        spec = FNO_STRATEGIES["long_straddle"]
        metrics = run_strategy_backtest(spec, self._three_cycles, k=0.9)
        assert metrics["n_trades"] == 1

    def test_strategy_key_in_metrics(self):
        spec = FNO_STRATEGIES["iron_condor"]
        metrics = run_strategy_backtest(spec, self._three_cycles, k=0.9)
        assert metrics["strategy"] == "iron_condor"

    def test_sharpe_is_oos_keys_present(self):
        spec = FNO_STRATEGIES["iron_condor"]
        metrics = run_strategy_backtest(spec, self._three_cycles, k=0.9)
        assert "sharpe_is" in metrics
        assert "sharpe_oos" in metrics

    def test_n_cycles_equals_input(self):
        spec = FNO_STRATEGIES["iron_condor"]
        metrics = run_strategy_backtest(spec, self._three_cycles, k=0.9)
        assert metrics["n_cycles"] == 3

    # ── Gate A/B toggle (gated vs ungated) ────────────────────────────────
    def test_gate_default_is_vol(self):
        """No gate arg → vol-gated → iron_condor trades only the SELL cycle."""
        spec = FNO_STRATEGIES["iron_condor"]
        m = run_strategy_backtest(spec, self._three_cycles, k=0.9)
        assert m["n_trades"] == 1
        assert m["gate"] == "vol"

    def test_gate_none_takes_all_cycles(self):
        """gate='none' → ungated → every valid cycle trades (3), not just 1."""
        spec = FNO_STRATEGIES["iron_condor"]
        m = run_strategy_backtest(spec, self._three_cycles, k=0.9, gate="none")
        assert m["n_trades"] == 3
        assert m["gate"] == "none"

    def test_ungated_ge_gated_across_strategies(self):
        """Ungated n_trades >= gated n_trades for every strategy (gate only filters)."""
        for name in ("iron_condor", "short_strangle", "bull_put_spread", "long_straddle"):
            spec = FNO_STRATEGIES[name]
            gated = run_strategy_backtest(spec, self._three_cycles, k=0.9, gate="vol")
            ungated = run_strategy_backtest(spec, self._three_cycles, k=0.9, gate="none")
            assert ungated["n_trades"] >= gated["n_trades"], name
            assert ungated["n_trades"] == 3, name  # all valid cycles taken

    def test_zero_trades_has_gate_key(self):
        """Zero-trades result still carries the 'gate' key (JSON-archive safe)."""
        spec = FNO_STRATEGIES["iron_condor"]  # sell_premium
        cycles = [self._make_cycle(rv=0.20, iv=0.15),   # BUY_PREMIUM → skipped
                  self._make_cycle(rv=0.17, iv=0.15)]   # STAND_ASIDE → skipped
        m = run_strategy_backtest(spec, cycles, k=0.9, gate="vol")
        assert m["n_trades"] == 0
        assert m["gate"] == "vol"

    def test_zero_trades_undefined_risk_keeps_disclaimer(self):
        """Undefined-risk strategy with 0 trades still carries the tail-blind disclaimer."""
        spec = FNO_STRATEGIES["short_straddle"]  # undefined-risk, sell_premium
        cycles = [self._make_cycle(rv=0.20, iv=0.15)]  # BUY_PREMIUM → 0 trades
        m = run_strategy_backtest(spec, cycles, k=0.9, gate="vol")
        assert m["n_trades"] == 0
        assert m["gate"] == "vol"
        _, reason = m["go_no_go"]
        assert "UNDEFINED-RISK" in reason

    def test_calendar_refusal_has_gate_key(self):
        """Multi-expiry refusal path also carries the 'gate' key."""
        spec = FNO_STRATEGIES["calendar_spread"]
        m = run_strategy_backtest(spec, self._three_cycles, k=0.9, gate="none")
        assert m["n_trades"] == 0
        assert m["gate"] == "none"

    def test_mean_span_positive_when_trades(self):
        spec = FNO_STRATEGIES["iron_condor"]
        metrics = run_strategy_backtest(spec, self._three_cycles, k=0.9)
        if metrics["n_trades"] > 0:
            assert metrics["mean_span"] > 0.0

    def test_return_on_margin_zero_when_no_trades(self):
        """All cycles gated out → n_trades=0 → return_on_margin=0."""
        spec = FNO_STRATEGIES["iron_condor"]
        # All cycles will be STAND_ASIDE
        cycles = [self._make_cycle(rv=0.17, iv=0.15)]  # rebased 0.1412 ∈ [0.135,0.15] → STAND_ASIDE
        metrics = run_strategy_backtest(spec, cycles, k=0.9)
        assert metrics["return_on_margin"] == 0.0


# ===========================================================================
# T13 — Multi-expiry refusal (calendar_spread)
# ===========================================================================


@needs_strategies
class TestMultiExpiryRefusal:
    """calendar_spread has needs_multi_expiry=True → refused in historical mode."""

    @staticmethod
    def _make_cycle() -> dict[str, Any]:
        return {
            "entry_date":       date(2025, 1, 2),
            "expiry_date":      date(2025, 1, 9),
            "spot":             24000.0,
            "straddle_iv":      0.15,
            "realized_vol_20d": 0.10,
            "dte":              7,
            "expiry_spot":      24050.0,
        }

    def test_returns_zero_trades(self):
        spec = FNO_STRATEGIES["calendar_spread"]
        cycles = [self._make_cycle(), self._make_cycle()]
        metrics = run_strategy_backtest(spec, cycles)
        assert metrics["n_trades"] == 0

    def test_go_no_go_is_false(self):
        spec = FNO_STRATEGIES["calendar_spread"]
        metrics = run_strategy_backtest(spec, [self._make_cycle()])
        go, reason = metrics["go_no_go"]
        assert go is False

    def test_reason_mentions_term_structure(self):
        spec = FNO_STRATEGIES["calendar_spread"]
        metrics = run_strategy_backtest(spec, [self._make_cycle()])
        _, reason = metrics["go_no_go"]
        assert "term structure" in reason.lower() or "forward-paper" in reason.lower()

    def test_does_not_raise(self):
        """Must return, not raise."""
        spec = FNO_STRATEGIES["calendar_spread"]
        result = run_strategy_backtest(spec, [self._make_cycle()])
        assert isinstance(result, dict)

    def test_return_on_margin_zero(self):
        spec = FNO_STRATEGIES["calendar_spread"]
        metrics = run_strategy_backtest(spec, [self._make_cycle()])
        assert metrics["return_on_margin"] == 0.0


# ===========================================================================
# Extra: price_leg_hist uses real black76 functions
# ===========================================================================


@pytest.mark.skipif(
    not (_HAS_STRATEGIES and _HAS_CONDOR),
    reason="requires both fno_strategies and fno_condor",
)
class TestPriceLegHist:
    def test_ce_matches_black76_call(self):
        leg = Leg("CE", "SELL", 24000.0)
        spot, sigma, dte = 24000.0, 0.15, 7
        T = dte / 365.0
        expected = black76_call(spot, 24000.0, T, sigma)
        assert price_leg_hist(leg, spot, sigma, dte) == pytest.approx(expected, rel=1e-6)

    def test_pe_matches_black76_put(self):
        leg = Leg("PE", "BUY", 23800.0)
        spot, sigma, dte = 24000.0, 0.15, 7
        T = dte / 365.0
        expected = black76_put(spot, 23800.0, T, sigma)
        assert price_leg_hist(leg, spot, sigma, dte) == pytest.approx(expected, rel=1e-6)

    def test_sell_fill_lower_than_mid(self):
        """SELL leg fill (adverse) < mid."""
        leg = Leg("CE", "SELL", 24000.0)
        mid = price_leg_hist(leg, 24000.0, 0.15, 7)
        filled = fill_price(leg, mid)
        assert filled < mid

    def test_buy_fill_higher_than_mid(self):
        """BUY leg fill (adverse) > mid."""
        leg = Leg("CE", "BUY", 24000.0)
        mid = price_leg_hist(leg, 24000.0, 0.15, 7)
        filled = fill_price(leg, mid)
        assert filled > mid


# ===========================================================================
# Extra: StrategySpec interface contract
# ===========================================================================


@needs_strategies
class TestStrategySpecInterface:
    def test_all_strategies_in_registry(self):
        # Original 7 keys (always present)
        original_keys = {
            "short_straddle", "short_strangle", "iron_condor",
            "iron_fly", "credit_put_spread", "long_straddle", "calendar_spread",
        }
        # Phase-0 expansion: 7 new builders added in feat/fno-options-strategies
        new_keys = {
            "broken_wing_condor", "jade_lizard",
            "bull_put_spread", "bear_call_spread",
            "bull_call_spread", "bear_put_spread",
            "ratio_spread",
        }
        assert original_keys.issubset(set(FNO_STRATEGIES.keys()))
        assert new_keys.issubset(set(FNO_STRATEGIES.keys()))

    def test_calendar_spread_needs_multi_expiry(self):
        assert FNO_STRATEGIES["calendar_spread"].needs_multi_expiry is True

    def test_all_others_single_expiry(self):
        for name, spec in FNO_STRATEGIES.items():
            if name != "calendar_spread":
                assert spec.needs_multi_expiry is False, f"{name} unexpectedly needs_multi_expiry"

    def test_sell_premium_flags(self):
        """Assert sell_premium for ALL 14 registry keys (fix #12: full coverage)."""
        sell = {
            "short_straddle", "short_strangle", "iron_condor", "iron_fly",
            "credit_put_spread", "calendar_spread",
            # Phase-0 expansion
            "broken_wing_condor", "jade_lizard", "bull_put_spread",
            "bear_call_spread", "ratio_spread",
        }
        buy = {
            "long_straddle",
            # Debit spreads → BUY_PREMIUM gate
            "bull_call_spread", "bear_put_spread",
        }
        assert sell | buy == set(FNO_STRATEGIES), (
            "sell_premium_flags test does not cover all registry keys"
        )
        for name in sell:
            assert FNO_STRATEGIES[name].sell_premium is True, (
                f"{name} should have sell_premium=True"
            )
        for name in buy:
            assert FNO_STRATEGIES[name].sell_premium is False, (
                f"{name} should have sell_premium=False"
            )

    def test_defined_risk_flags(self):
        """Assert defined_risk for ALL 14 registry keys (fix #12: full coverage)."""
        undefined = {
            "short_straddle", "short_strangle",
            # Partial/naked undefined-risk strategies
            "jade_lizard", "ratio_spread",
        }
        defined = {
            "iron_condor", "iron_fly", "credit_put_spread",
            "long_straddle", "calendar_spread",
            # Phase-0 expansion (all defined-risk)
            "broken_wing_condor", "bull_put_spread", "bear_call_spread",
            "bull_call_spread", "bear_put_spread",
        }
        assert undefined | defined == set(FNO_STRATEGIES), (
            "defined_risk_flags test does not cover all registry keys"
        )
        for name in undefined:
            assert FNO_STRATEGIES[name].defined_risk is False, (
                f"{name} should have defined_risk=False"
            )
        for name in defined:
            assert FNO_STRATEGIES[name].defined_risk is True, (
                f"{name} should have defined_risk=True"
            )

    def test_builder_callable(self):
        for name, spec in FNO_STRATEGIES.items():
            assert callable(spec.build), f"{name}.build is not callable"

    def test_builder_signature_returns_list_of_legs(self):
        """Builders must return list[Leg] for a standard call."""
        spot, atm, step, dte, sigma = 24000.0, 24000, 50, 7, 0.15
        for name, spec in FNO_STRATEGIES.items():
            if spec.needs_multi_expiry:
                continue  # placeholder builder — still returns list[Leg]
            legs = spec.build(spot, atm, step, dte, sigma, spec.default_params)
            assert isinstance(legs, list), f"{name} builder did not return a list"
            for leg in legs:
                assert isinstance(leg, Leg), f"{name} builder returned non-Leg in list"


# ===========================================================================
# T14 — Resolution / P&L tests for 4 under-covered builders (fix #13)
# Exact ₹ via pytest.approx — computed from real black76_* pricing
# ===========================================================================


@pytest.mark.skipif(
    not (_HAS_STRATEGIES and _HAS_CONDOR),
    reason="requires both fno_strategies and fno_condor",
)
class TestIronFlyResolution:
    """Iron fly: short ATM straddle + long wings (wing_strikes=4 → ±200 pts).

    spot=24000, sigma=0.15, dte=7, step=50.
    Shorts: CE@24000 + PE@24000.  Longs: CE@24200 + PE@23800.
    Wing width = 200 pts.
    """

    SPOT  = 24000.0
    SIGMA = 0.15
    DTE   = 7
    STEP  = 50
    LOT   = 65
    ATM   = 24000
    WING  = 200  # wing_strikes=4 × step=50

    def _build(self):
        legs = build_iron_fly(self.SPOT, self.ATM, self.STEP, self.DTE, self.SIGMA,
                              {"wing_strikes": 4})
        fills = [fill_price(l, price_leg_hist(l, self.SPOT, self.SIGMA, self.DTE))
                 for l in legs]
        entry_net = net_premium_per_unit(legs, fills)
        return legs, fills, entry_net

    def test_if1_geometry(self):
        """Shorts at ATM; longs symmetric at ATM ± WING."""
        legs, _, _ = self._build()
        assert len(legs) == 4
        ce_sells = [l for l in legs if l.option_type == "CE" and l.side == "SELL"]
        pe_sells = [l for l in legs if l.option_type == "PE" and l.side == "SELL"]
        ce_buys  = [l for l in legs if l.option_type == "CE" and l.side == "BUY"]
        pe_buys  = [l for l in legs if l.option_type == "PE" and l.side == "BUY"]
        assert len(ce_sells) == 1 and ce_sells[0].strike == pytest.approx(self.ATM)
        assert len(pe_sells) == 1 and pe_sells[0].strike == pytest.approx(self.ATM)
        assert len(ce_buys)  == 1 and ce_buys[0].strike  == pytest.approx(self.ATM + self.WING)
        assert len(pe_buys)  == 1 and pe_buys[0].strike  == pytest.approx(self.ATM - self.WING)

    def test_if2_max_win_atm_pin(self):
        """S = ATM: all shorts expire at-the-money (zero intrinsic), long wings OTM.
        Gross = entry_net × LOT."""
        legs, fills, entry_net = self._build()
        result = resolve_legs_with_fills(legs, fills, self.SPOT, self.LOT)
        assert result["gross_pnl"] == pytest.approx(entry_net * self.LOT, rel=1e-4)

    def test_if3_max_loss_below_long_put(self):
        """S = long_put_k − 1 step: max loss = (wing_width − entry_net) × LOT."""
        legs, fills, entry_net = self._build()
        S = self.ATM - self.WING - self.STEP
        result = resolve_legs_with_fills(legs, fills, float(S), self.LOT)
        expected = -(self.WING - entry_net) * self.LOT
        assert result["gross_pnl"] == pytest.approx(expected, rel=1e-4)

    def test_if4_entry_net_credit(self):
        """Iron fly must produce a net credit (entry_net > 0)."""
        _, _, entry_net = self._build()
        assert entry_net > 0

    def test_if5_span_defined_model(self):
        """SPAN = max_loss = (wing_width − credit) × LOT."""
        legs, _, entry_net = self._build()
        spec = FNO_STRATEGIES["iron_fly"]
        margin = span_margin(spec, legs, entry_net, self.SPOT, self.LOT)
        expected = max(0.0, self.WING - entry_net) * self.LOT
        assert margin == pytest.approx(expected, rel=1e-4)


@pytest.mark.skipif(
    not (_HAS_STRATEGIES and _HAS_CONDOR),
    reason="requires both fno_strategies and fno_condor",
)
class TestShortStrangleResolution:
    """Short strangle: SELL CE @24500 + SELL PE @23500 (move_mult=1.0).

    spot=24000, sigma=0.15, dte=7.
    implied_move ≈ 498.55 → shorts at 24500 / 23500.
    """

    SPOT  = 24000.0
    SIGMA = 0.15
    DTE   = 7
    STEP  = 50
    LOT   = 65
    ATM   = 24000

    def _build(self):
        legs = build_short_strangle(self.SPOT, self.ATM, self.STEP, self.DTE, self.SIGMA,
                                    {"move_mult": 1.0})
        fills = [fill_price(l, price_leg_hist(l, self.SPOT, self.SIGMA, self.DTE))
                 for l in legs]
        entry_net = net_premium_per_unit(legs, fills)
        return legs, fills, entry_net

    def test_ss1_geometry(self):
        """CE short above ATM, PE short below ATM; both SELL; qty_lots=1."""
        legs, _, _ = self._build()
        assert len(legs) == 2
        ce_leg = next(l for l in legs if l.option_type == "CE")
        pe_leg = next(l for l in legs if l.option_type == "PE")
        assert ce_leg.side == "SELL" and pe_leg.side == "SELL"
        assert ce_leg.strike > self.SPOT
        assert pe_leg.strike < self.SPOT
        assert ce_leg.qty_lots == 1 and pe_leg.qty_lots == 1

    def test_ss2_max_win_inside_shorts(self):
        """S between put and call strike → both OTM → gross = entry_net × LOT."""
        legs, fills, entry_net = self._build()
        result = resolve_legs_with_fills(legs, fills, self.SPOT, self.LOT)
        assert result["gross_pnl"] == pytest.approx(entry_net * self.LOT, rel=1e-4)

    def test_ss3_call_loss_above_short_call(self):
        """S > short_call_k: CE intrinsic = (S − K_sc), PE intrinsic = 0.
        gross = (entry_net − (S − K_sc)) × LOT."""
        legs, fills, entry_net = self._build()
        ce_leg = next(l for l in legs if l.option_type == "CE")
        k_sc = ce_leg.strike  # 24500
        S = k_sc + 500.0  # 25000
        result = resolve_legs_with_fills(legs, fills, S, self.LOT)
        expected = (entry_net - (S - k_sc)) * self.LOT
        assert result["gross_pnl"] == pytest.approx(expected, rel=1e-4)

    def test_ss4_entry_net_positive(self):
        """Short strangle must be a net credit at entry."""
        _, _, entry_net = self._build()
        assert entry_net > 0

    def test_ss5_credit_less_than_atm_straddle(self):
        """OTM strangle credit < ATM straddle credit (options are cheaper OTM)."""
        _, _, entry_net_str = self._build()
        # ATM straddle: build separately
        atm_legs = build_short_straddle(self.SPOT, self.ATM, self.STEP, self.DTE, self.SIGMA, {})
        atm_fills = [fill_price(l, price_leg_hist(l, self.SPOT, self.SIGMA, self.DTE))
                     for l in atm_legs]
        entry_net_atm = net_premium_per_unit(atm_legs, atm_fills)
        assert entry_net_str < entry_net_atm


@pytest.mark.skipif(
    not (_HAS_STRATEGIES and _HAS_CONDOR),
    reason="requires both fno_strategies and fno_condor",
)
class TestCreditPutSpreadResolution:
    """Credit put spread: SELL PE @23500 + BUY PE @23400 (short_mult=1.0, width=2).

    spot=24000, sigma=0.15, dte=7.  Width = 2 × 50 = 100 pts.
    """

    SPOT  = 24000.0
    SIGMA = 0.15
    DTE   = 7
    STEP  = 50
    LOT   = 65
    ATM   = 24000
    WIDTH = 100  # 2 × step

    def _build(self):
        legs = build_credit_put_spread(
            self.SPOT, self.ATM, self.STEP, self.DTE, self.SIGMA,
            {"short_mult": 1.0, "width_strikes": 2},
        )
        fills = [fill_price(l, price_leg_hist(l, self.SPOT, self.SIGMA, self.DTE))
                 for l in legs]
        entry_net = net_premium_per_unit(legs, fills)
        return legs, fills, entry_net

    def test_cps1_geometry(self):
        """Both PE legs; SELL at higher strike, BUY at lower."""
        legs, _, _ = self._build()
        assert len(legs) == 2
        assert all(l.option_type == "PE" for l in legs)
        sell_leg = next(l for l in legs if l.side == "SELL")
        buy_leg  = next(l for l in legs if l.side == "BUY")
        assert sell_leg.strike > buy_leg.strike
        assert sell_leg.strike - buy_leg.strike == pytest.approx(self.WIDTH)

    def test_cps2_entry_net_positive(self):
        _, _, entry_net = self._build()
        assert entry_net > 0

    def test_cps3_max_win_above_short_put(self):
        """S >= short_put_k → both OTM → gross = entry_net × LOT."""
        legs, fills, entry_net = self._build()
        result = resolve_legs_with_fills(legs, fills, self.SPOT, self.LOT)
        assert result["gross_pnl"] == pytest.approx(entry_net * self.LOT, rel=1e-4)

    def test_cps4_max_loss_below_long_put(self):
        """S <= long_put_k → max loss = (width − credit) × LOT."""
        legs, fills, entry_net = self._build()
        buy_leg = next(l for l in legs if l.side == "BUY")
        S = buy_leg.strike - self.STEP  # well below long put
        result = resolve_legs_with_fills(legs, fills, float(S), self.LOT)
        expected = -(self.WIDTH - entry_net) * self.LOT
        assert result["gross_pnl"] == pytest.approx(expected, rel=1e-4)

    def test_cps5_max_loss_less_than_width(self):
        """Entry credit < width → max_loss = width − credit > 0."""
        _, _, entry_net = self._build()
        assert entry_net < self.WIDTH  # credit less than width (realistic)
        max_loss_per_unit = self.WIDTH - entry_net
        assert max_loss_per_unit > 0

    def test_cps6_span_margin_defined(self):
        """SPAN = max_loss × LOT."""
        legs, _, entry_net = self._build()
        spec = FNO_STRATEGIES["credit_put_spread"]
        margin = span_margin(spec, legs, entry_net, self.SPOT, self.LOT)
        expected = max(0.0, self.WIDTH - entry_net) * self.LOT
        assert margin == pytest.approx(expected, rel=1e-4)


@pytest.mark.skipif(
    not (_HAS_STRATEGIES and _HAS_CONDOR),
    reason="requires both fno_strategies and fno_condor",
)
class TestLongStraddleResolution:
    """Long straddle: BUY ATM CE + BUY ATM PE.

    spot=24000, sigma=0.15, dte=7.
    Net entry is a DEBIT (negative entry_net).
    """

    SPOT  = 24000.0
    SIGMA = 0.15
    DTE   = 7
    LOT   = 65
    ATM   = 24000
    STEP  = 50

    def _build(self):
        legs = build_long_straddle(self.SPOT, self.ATM, self.STEP, self.DTE, self.SIGMA, {})
        fills = [fill_price(l, price_leg_hist(l, self.SPOT, self.SIGMA, self.DTE))
                 for l in legs]
        entry_net = net_premium_per_unit(legs, fills)
        return legs, fills, entry_net

    def test_ls1_geometry(self):
        """Both BUY legs at ATM: 1 CE + 1 PE."""
        legs, _, _ = self._build()
        assert len(legs) == 2
        assert all(l.side == "BUY" for l in legs)
        types = {l.option_type for l in legs}
        assert types == {"CE", "PE"}
        assert all(l.strike == pytest.approx(self.ATM) for l in legs)

    def test_ls2_entry_net_negative(self):
        """Long straddle is a debit position."""
        _, _, entry_net = self._build()
        assert entry_net < 0

    def test_ls3_max_loss_at_pin(self):
        """S = ATM: both legs expire worthless; gross = −debit × LOT."""
        legs, fills, entry_net = self._build()
        debit = -entry_net
        result = resolve_legs_with_fills(legs, fills, self.SPOT, self.LOT)
        assert result["gross_pnl"] == pytest.approx(-debit * self.LOT, rel=1e-4)

    def test_ls4_profit_on_large_up_move(self):
        """S = ATM + 1000: CE deep ITM (1000 intrinsic), PE OTM.
        gross = (1000 − debit) × LOT."""
        legs, fills, entry_net = self._build()
        debit = -entry_net
        S = self.SPOT + 1000.0
        result = resolve_legs_with_fills(legs, fills, S, self.LOT)
        expected = (S - self.SPOT - debit) * self.LOT
        assert result["gross_pnl"] == pytest.approx(expected, rel=1e-4)

    def test_ls5_breakeven_above(self):
        """Upper breakeven = ATM + debit; gross ≈ 0 at that spot."""
        legs, fills, entry_net = self._build()
        debit = -entry_net
        S = self.SPOT + debit
        result = resolve_legs_with_fills(legs, fills, S, self.LOT)
        assert abs(result["gross_pnl"]) < self.LOT  # within one point×lot

    def test_ls6_profit_on_large_down_move(self):
        """S = ATM − 600: PE deep ITM (600 intrinsic), CE OTM.
        gross = (600 − debit) × LOT."""
        legs, fills, entry_net = self._build()
        debit = -entry_net
        S = self.SPOT - 600.0
        result = resolve_legs_with_fills(legs, fills, S, self.LOT)
        expected = (600.0 - debit) * self.LOT
        assert result["gross_pnl"] == pytest.approx(expected, rel=1e-4)


# ===========================================================================
# T15 — Strengthened run_strategy_backtest tests (fix #14)
# ===========================================================================


@needs_strategies
class TestRunStrategyBacktestStrengthened:
    """Strengthened assertions for the backtest runner."""

    @staticmethod
    def _make_cycle(rv: float, iv: float, spot=24000.0, dte=7, expiry_spot=24050.0) -> dict[str, Any]:
        return {
            "entry_date":       date(2025, 1, 2),
            "expiry_date":      date(2025, 1, 9),
            "spot":             spot,
            "straddle_iv":      iv,
            "realized_vol_20d": rv,
            "dte":              dte,
            "expiry_spot":      expiry_spot,
        }

    def test_n1_go_no_go_false(self):
        """n=1 trade → Sharpe undefined → go_no_go should be False (insufficient data)."""
        spec = FNO_STRATEGIES["iron_condor"]
        # Single SELL_PREMIUM cycle
        cycle = self._make_cycle(rv=0.10, iv=0.15)
        metrics = run_strategy_backtest(spec, [cycle], k=0.9)
        go, _ = metrics["go_no_go"]
        assert go is False, "Single-trade backtest should not produce a GO"

    def test_condor_net_pnl_nonzero(self):
        """With a real pricing cycle, net_pnl must be non-zero."""
        spec = FNO_STRATEGIES["iron_condor"]
        cycle = self._make_cycle(rv=0.10, iv=0.15, expiry_spot=24000.0)  # pin
        metrics = run_strategy_backtest(spec, [cycle], k=0.9)
        assert metrics["n_trades"] == 1
        assert metrics["net_pnl"] != 0.0, "net_pnl must be non-zero for a real pricing cycle"

    def test_undefined_risk_disclaimer_in_reason(self):
        """GO/NO-GO reason for a naked strategy must contain the undefined-risk disclaimer."""
        spec = FNO_STRATEGIES["short_straddle"]
        cycle = self._make_cycle(rv=0.10, iv=0.15)
        metrics = run_strategy_backtest(spec, [cycle], k=0.9)
        _, reason = metrics["go_no_go"]
        assert "UNDEFINED-RISK" in reason, (
            f"Expected 'UNDEFINED-RISK' in go_no_go reason for short_straddle, got: {reason!r}"
        )

    def test_undefined_risk_disclaimer_for_ratio_spread(self):
        """ratio_spread also gets the undefined-risk disclaimer."""
        spec = FNO_STRATEGIES["ratio_spread"]
        cycle = self._make_cycle(rv=0.10, iv=0.15)
        metrics = run_strategy_backtest(spec, [cycle], k=0.9)
        _, reason = metrics["go_no_go"]
        assert "UNDEFINED-RISK" in reason

    def test_jade_span_margin_structure(self):
        """Jade lizard SPAN = defined_part + naked_part (fix #14: pin via approx)."""
        from research.backtest.fno_strategies import (
            _span_defined, _span_naked_short,
        )
        spec = FNO_STRATEGIES["jade_lizard"]
        spot = 24000.0; sigma = 0.15; dte = 7; step = 50
        atm = 24000
        legs = spec.build(spot, atm, step, dte, sigma, spec.default_params)
        fills = [fill_price(l, price_leg_hist(l, spot, sigma, dte)) for l in legs]
        entry_net = net_premium_per_unit(legs, fills)
        total_margin = span_margin(spec, legs, entry_net, spot, lot=65,
                                   params=spec.default_params)
        defined_part = _span_defined(legs, entry_net, lot=65)
        naked_part   = _span_naked_short(legs, spot, lot=65,
                                          span_pct=spec.default_params.get("span_pct", 0.12))
        assert total_margin == pytest.approx(defined_part + naked_part, rel=1e-6)
        assert total_margin > 0

    def test_ratio_spread_span_margin_structure(self):
        """Ratio spread SPAN = defined_part + naked_part; naked_part = 1 uncovered lot."""
        from research.backtest.fno_strategies import (
            _span_defined, _span_naked_short,
        )
        spec = FNO_STRATEGIES["ratio_spread"]
        spot = 24000.0; sigma = 0.15; dte = 7; step = 50
        atm = 24000
        legs = spec.build(spot, atm, step, dte, sigma, spec.default_params)
        fills = [fill_price(l, price_leg_hist(l, spot, sigma, dte)) for l in legs]
        entry_net = net_premium_per_unit(legs, fills)
        total_margin = span_margin(spec, legs, entry_net, spot, lot=65,
                                   params=spec.default_params)
        defined_part = _span_defined(legs, entry_net, lot=65)
        span_pct = spec.default_params.get("span_pct", 0.10)
        naked_part = _span_naked_short(legs, spot, lot=65, span_pct=span_pct)
        assert total_margin == pytest.approx(defined_part + naked_part, rel=1e-6)
        # For BUY 1 / SELL 2: net uncovered = 1 lot → naked_part = span_pct * spot * lot * 1
        assert naked_part == pytest.approx(span_pct * spot * 65 * 1, rel=1e-6)

    def test_capital_zero_raises(self):
        """capital <= 0 must raise ValueError."""
        spec = FNO_STRATEGIES["iron_condor"]
        with pytest.raises(ValueError, match="capital must be > 0"):
            run_strategy_backtest(spec, [], capital=0)
