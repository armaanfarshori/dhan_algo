"""Unit tests for research/backtest/fno_orchestrator.py — the F&O selection layer.

All tests are PURE: synthetic regimes / hand-built cycle dicts, no DB, no network.
The module's DB touches (``cycles_for_index`` + ``RegimeSidecar``) are NOT exercised
here — ``run`` takes cycles directly and tests inject synthetic trend / iv_rank.

Test map (specs 02, 03, 06):
  - Routing policy correctness: each regime → expected strategy + stand-aside (R0–R7),
    including R5/R6 disabled fall-through, R6 negative-skew, DTE exact boundaries.
  - Defined-risk whitelist: undefined-risk allowed-set raises at construction.
  - gate="none" dispatch: the chosen strategy is not double-gated by the engine.
  - A/B parity (gate="none" on both sides): orchestrator pinned to one strategy
    reproduces that strategy's standalone trades / ROM / sharpe_is / sharpe_oos.
  - OOS ROM headline (run_comparison): rom_oos on singles + best_single by OOS ROM.
  - regime None-handling, no-look-ahead, participation rate, pure sidecar helpers.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Module under test — skip gracefully if still in flight (mirror fno tests).
# ---------------------------------------------------------------------------
try:
    from research.backtest.fno_orchestrator import (
        DEFAULT_ALLOWED,
        GO_SET,
        INDEX_REGISTRY,
        FnoOrchestrator,
        IndexParams,
        RegimeRoutingPolicy,
        RegimeSignals,
        RoutingParams,
        VrpDefaultPolicy,
        iv_rank,
        regime_from_cycle,
        run_comparison,
        trend_slope,
        _rom_oos_from_trades,
    )
    from research.backtest.fno_strategies import (
        FNO_STRATEGIES,
        run_strategy_backtest,
    )
    from ml.fno_vol_gate import BUY_PREMIUM, SELL_PREMIUM, STAND_ASIDE

    _HAS_ORCH = True
except (ImportError, AttributeError) as _e:  # pragma: no cover
    _HAS_ORCH = False
    _import_error = str(_e)

needs_orch = pytest.mark.skipif(
    not _HAS_ORCH,
    reason=f"fno_orchestrator not importable: {'' if _HAS_ORCH else _import_error!r}",  # type: ignore[name-defined]
)


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------
def _cycle(
    rv: float,
    iv: float,
    *,
    spot: float = 24000.0,
    dte: int = 5,
    expiry_spot: float = 24050.0,
    entry: date = date(2025, 1, 2),
    expiry: date = date(2025, 1, 9),
) -> dict[str, Any]:
    return {
        "entry_date": entry,
        "expiry_date": expiry,
        "spot": spot,
        "straddle_iv": iv,
        "realized_vol_20d": rv,
        "dte": dte,
        "expiry_spot": expiry_spot,
    }


def _signals(
    *,
    gate: str = SELL_PREMIUM,
    dte: int = 5,
    vrp: float = 0.05,
    iv: float = 0.15,
    trend: float | None = None,
    iv_rank: float | None = None,
) -> RegimeSignals:
    rv = max(0.0, iv - vrp)
    return RegimeSignals(
        entry_date=date(2025, 1, 2),
        expiry_date=date(2025, 1, 9),
        dte=dte,
        spot=24000.0,
        realized_vol=rv,
        implied_vol=iv,
        vrp=vrp,
        iv_ratio=(rv / iv if iv > 0 else 0.0),
        gate_label=gate,
        implied_move=200.0,
        iv_rank=iv_rank,
        trend=trend,
    )


NIFTY = None
if _HAS_ORCH:
    NIFTY = INDEX_REGISTRY["NIFTY"]


# ===========================================================================
# 1. Routing policy correctness — each regime → expected strategy / stand-aside
# ===========================================================================
@needs_orch
class TestRoutingPolicy:
    def setup_method(self) -> None:
        self.policy = RegimeRoutingPolicy(RoutingParams())

    def _select(self, sig: RegimeSignals) -> str | None:
        name, _params, _reason = self.policy.select(sig, DEFAULT_ALLOWED)
        return name

    def test_r0_buy_premium_stands_aside(self):
        assert self._select(_signals(gate=BUY_PREMIUM)) is None

    def test_r0_stand_aside_gate_stands_aside(self):
        assert self._select(_signals(gate=STAND_ASIDE)) is None

    def test_r1_dte_too_short_stands_aside(self):
        assert self._select(_signals(gate=SELL_PREMIUM, dte=0)) is None

    def test_r1_dte_too_long_stands_aside(self):
        assert self._select(_signals(gate=SELL_PREMIUM, dte=20)) is None

    def test_r1_dte_boundary_in_window_trades(self):
        # dte_max default = 7 → dte=7 is IN window → trades (the workhorse condor).
        assert self._select(_signals(gate=SELL_PREMIUM, dte=7)) == "iron_condor"

    def test_r1_dte_boundary_just_over_stands_aside(self):
        # dte=8 is one past dte_max=7 → stand aside.
        assert self._select(_signals(gate=SELL_PREMIUM, dte=8)) is None

    def test_r1_dte_boundary_min_in_window(self):
        # dte_min default = 1 → dte=1 trades, dte=0 stands aside.
        assert self._select(_signals(gate=SELL_PREMIUM, dte=1)) == "iron_condor"

    def test_r2_vrp_below_floor_stands_aside(self):
        pol = RegimeRoutingPolicy(RoutingParams(vrp_min=0.10))
        name, _, _ = pol.select(_signals(vrp=0.02), DEFAULT_ALLOWED)
        assert name is None

    def test_r2_negative_vrp_at_default_floor_stands_aside(self):
        # vrp<0 (realized > implied) at the default vrp_min=0.0 floor → stand aside.
        pol = RegimeRoutingPolicy(RoutingParams())  # vrp_min=0.0
        name, _, _ = pol.select(_signals(gate=SELL_PREMIUM, vrp=-0.01), DEFAULT_ALLOWED)
        assert name is None

    def test_r3_iv_below_floor_stands_aside(self):
        pol = RegimeRoutingPolicy(RoutingParams(iv_floor=0.20))
        name, _, _ = pol.select(_signals(iv=0.12), DEFAULT_ALLOWED)
        assert name is None

    def test_r4_strong_downtrend_routes_iron_condor(self):
        # strong DOWN → neutral condor, NEVER a bullish put spread.
        assert self._select(_signals(trend=-0.9)) == "iron_condor"

    def test_r5_strong_uptrend_routes_bull_put(self):
        assert self._select(_signals(trend=0.9)) == "bull_put_spread"

    def test_r5_uptrend_rich_iv_routes_credit_put(self):
        assert self._select(_signals(trend=0.9, iv_rank=0.85)) == "credit_put_spread"

    def test_r6_neutral_skew_positive_routes_broken_wing(self):
        # trend present but weak (between skew and strong) → broken_wing_condor.
        assert self._select(_signals(trend=0.4)) == "broken_wing_condor"

    def test_r6_neutral_skew_negative_routes_broken_wing(self):
        # NEGATIVE skew (downtrend, weak) is ALSO routed to broken_wing (H6 / spec 02 R6).
        assert self._select(_signals(trend=-0.4)) == "broken_wing_condor"

    def test_r6_skew_orientation_toward_uptrend_widens_call_wing(self):
        # Uptrend broken-wing inverts the default put-wide skew → widens the CALL wing.
        _name, params, _reason = self.policy.select(_signals(trend=0.4), DEFAULT_ALLOWED)
        assert params is not None
        # default skew 1.5 → uptrend orientation gives 1/1.5 (< 1 → wider call wing).
        assert params["skew"] == pytest.approx(1.0 / 1.5)

    def test_r6_skew_orientation_downtrend_keeps_put_wide(self):
        # Downtrend keeps the default put-wide skew (>1).
        _name, params, _reason = self.policy.select(_signals(trend=-0.4), DEFAULT_ALLOWED)
        assert params is not None
        assert params["skew"] == pytest.approx(1.5)

    def test_r7_neutral_default_routes_iron_condor(self):
        # SELL_PREMIUM, no trend signal → workhorse condor.
        assert self._select(_signals(trend=None)) == "iron_condor"
        # explicit flat trend below skew threshold → also condor.
        assert self._select(_signals(trend=0.05)) == "iron_condor"

    def test_r5_disabled_falls_through_to_r6(self):
        # bull_put + credit_put disabled, uptrend but only skew-strength → R6 broken_wing.
        pol = RegimeRoutingPolicy(
            RoutingParams(enabled=frozenset({"iron_condor", "broken_wing_condor"}))
        )
        # trend 0.4 is in [skew, strong) so R5 (strong) does not fire; R6 does.
        name, _, _ = pol.select(_signals(trend=0.4), DEFAULT_ALLOWED)
        assert name == "broken_wing_condor"

    def test_r5_strong_uptrend_disabled_falls_through_to_r7(self):
        # STRONG uptrend but bull_put/credit_put disabled AND not skew-band → R7 condor.
        pol = RegimeRoutingPolicy(
            RoutingParams(enabled=frozenset({"iron_condor"}))
        )
        name, _, _ = pol.select(_signals(trend=0.9), DEFAULT_ALLOWED)
        assert name == "iron_condor"

    def test_allowed_strategies_fall_through_via_allowed_set(self):
        # bull_put EXCLUDED from allowed + strong uptrend → falls through to iron_condor (R7).
        pol = RegimeRoutingPolicy(RoutingParams())
        allowed = frozenset({"iron_condor", "credit_put_spread", "broken_wing_condor"})
        # strong uptrend, low iv_rank → would be bull_put, but it's not allowed → R7 condor.
        name, _, _ = pol.select(_signals(trend=0.9, iv_rank=0.1), allowed)
        assert name == "iron_condor"

    def test_vrp_default_policy(self):
        pol = VrpDefaultPolicy()
        assert pol.select(_signals(gate=SELL_PREMIUM), DEFAULT_ALLOWED)[0] == "iron_condor"
        assert pol.select(_signals(gate=BUY_PREMIUM), DEFAULT_ALLOWED)[0] is None

    def test_closure_only_go_set_emitted(self):
        # Sweep a grid of regimes; the policy must NEVER emit a non-GO name.
        for gate in (SELL_PREMIUM, BUY_PREMIUM, STAND_ASIDE):
            for dte in (0, 3, 5, 8, 20):
                for trend in (None, -0.9, -0.4, 0.0, 0.4, 0.9):
                    for ivr in (None, 0.1, 0.85):
                        name = self._select(
                            _signals(gate=gate, dte=dte, trend=trend, iv_rank=ivr)
                        )
                        assert name is None or name in GO_SET

    def test_determinism(self):
        sig = _signals(trend=0.9)
        a = self.policy.select(sig, DEFAULT_ALLOWED)
        b = self.policy.select(sig, DEFAULT_ALLOWED)
        assert a == b


# ===========================================================================
# 2. Defined-risk whitelist
# ===========================================================================
@needs_orch
class TestWhitelist:
    def test_default_allowed_each_name_defined_risk_and_go(self):
        # Per-name assertion (NOT the tautological frozenset(GO_SET)==DEFAULT_ALLOWED).
        for name in GO_SET:
            assert name in DEFAULT_ALLOWED
            assert name in FNO_STRATEGIES
            assert FNO_STRATEGIES[name].defined_risk is True
            assert FNO_STRATEGIES[name].sell_premium is True

    def test_undefined_risk_in_allowed_raises(self):
        # short_straddle is undefined-risk (defined_risk=False) → must be refused.
        assert FNO_STRATEGIES["short_straddle"].defined_risk is False
        with pytest.raises(ValueError, match="undefined-risk"):
            FnoOrchestrator(
                RegimeRoutingPolicy(),
                index=NIFTY,
                allowed_strategies=frozenset({"iron_condor", "short_straddle"}),
            )

    def test_unknown_strategy_in_allowed_raises(self):
        with pytest.raises(ValueError, match="not in FNO_STRATEGIES"):
            FnoOrchestrator(
                RegimeRoutingPolicy(),
                index=NIFTY,
                allowed_strategies=frozenset({"does_not_exist"}),
            )


# ===========================================================================
# 3. gate="none" dispatch — no double-gating
# ===========================================================================
@needs_orch
class TestNoDoubleGating:
    def test_dispatch_passes_gate_none(self, monkeypatch):
        """The orchestrator must call run_strategy_backtest with gate='none'."""
        import research.backtest.fno_orchestrator as orch_mod

        captured: dict[str, Any] = {}

        def _fake_rsb(spec, cycles, params=None, **kw):
            captured["gate"] = kw.get("gate")
            captured["spec"] = spec.name
            captured["n_cycles"] = len(cycles)
            return {"trades": []}

        monkeypatch.setattr(orch_mod, "run_strategy_backtest", _fake_rsb)
        orch = FnoOrchestrator(RegimeRoutingPolicy(), index=NIFTY)
        sig = _signals(gate=SELL_PREMIUM, trend=None)  # → iron_condor
        decision = orch.decide(sig)
        assert decision.strategy == "iron_condor"
        orch.dispatch(decision, _cycle(rv=0.10, iv=0.15))
        assert captured["gate"] == "none"
        assert captured["spec"] == "iron_condor"
        assert captured["n_cycles"] == 1

    def test_dispatch_runs_even_when_engine_gate_would_block(self):
        """A cycle the engine's vol-gate would skip under gate='vol' still produces
        a trade through the orchestrator (gate='none')."""
        cyc = _cycle(rv=0.20, iv=0.15)  # rv > iv → BUY_PREMIUM
        orch = FnoOrchestrator(RegimeRoutingPolicy(), index=NIFTY)
        from research.backtest.fno_orchestrator import RoutingDecision

        sig = regime_from_cycle(cyc, k=0.9)
        assert sig.gate_label == BUY_PREMIUM
        decision = RoutingDecision(
            entry_date=sig.entry_date,
            stand_aside=False,
            strategy="iron_condor",
            params={"move_mult": 1.5, "wing_strikes": 2},
            reason="forced",
            signals=sig,
        )
        result = orch.dispatch(decision, cyc)
        assert result is not None
        assert result["n_trades"] == 1  # gate='none' did NOT skip it

    def test_stand_aside_dispatch_returns_none(self):
        orch = FnoOrchestrator(RegimeRoutingPolicy(), index=NIFTY)
        sig = _signals(gate=BUY_PREMIUM)
        decision = orch.decide(sig)
        assert decision.stand_aside is True
        assert orch.dispatch(decision, _cycle(rv=0.20, iv=0.15)) is None

    def test_gate_none_orchestrator_deploys_on_non_sell_cycle(self):
        """Under gate='none' the router deploys even on a non-SELL_PREMIUM cycle
        (ungated baseline, R0 bypassed) while the recorded gate_label stays real.
        Uses a STAND_ASIDE cycle (rv between iv and k*iv → positive vrp) so the
        R2/R3 edge floors — which still apply under gate='none' — pass."""
        orch = FnoOrchestrator(RegimeRoutingPolicy(), index=NIFTY, gate="none")
        sig = regime_from_cycle(_cycle(rv=0.14, iv=0.15), k=0.9)  # STAND_ASIDE
        assert sig.gate_label == STAND_ASIDE
        assert sig.vrp is not None and sig.vrp > 0  # positive edge → floors pass
        decision = orch.decide(sig)
        assert decision.stand_aside is False  # ungated → deploys (R0 bypassed)
        assert decision.strategy == "iron_condor"
        assert decision.signals.gate_label == STAND_ASIDE  # provenance preserved

        # Sanity: the SAME cycle under gate='vol' stands aside (R0 gate master switch).
        orch_vol = FnoOrchestrator(RegimeRoutingPolicy(), index=NIFTY, gate="vol")
        assert orch_vol.decide(sig).stand_aside is True


# ===========================================================================
# 4. regime_from_cycle correctness + None handling (H3 / M3)
# ===========================================================================
@needs_orch
class TestRegimeFromCycle:
    def test_gate_label_uses_real_gate(self):
        assert regime_from_cycle(_cycle(rv=0.10, iv=0.15)).gate_label == SELL_PREMIUM
        assert regime_from_cycle(_cycle(rv=0.20, iv=0.15)).gate_label == BUY_PREMIUM
        assert regime_from_cycle(_cycle(rv=0.14, iv=0.15)).gate_label == STAND_ASIDE

    def test_vrp_and_iv_ratio(self):
        sig = regime_from_cycle(_cycle(rv=0.10, iv=0.15))
        assert sig.vrp == pytest.approx(0.05)
        assert sig.iv_ratio == pytest.approx(0.10 / 0.15)

    def test_missing_iv_keeps_none_not_fabricated_zero(self):
        # H3: a missing/zero straddle_iv must NOT be coerced to a measured 0.0.
        cyc = _cycle(rv=0.10, iv=0.15)
        cyc["straddle_iv"] = None
        sig = regime_from_cycle(cyc)
        assert sig.implied_vol is None
        assert sig.vrp is None
        assert sig.iv_ratio is None
        assert sig.gate_label == STAND_ASIDE  # gate fails open
        # the router must stand aside on this (R3 None-iv veto).
        name, _, _ = RegimeRoutingPolicy().select(sig, DEFAULT_ALLOWED)
        assert name is None

    def test_missing_rv_keeps_none(self):
        cyc = _cycle(rv=0.10, iv=0.15)
        cyc["realized_vol_20d"] = None
        sig = regime_from_cycle(cyc)
        assert sig.realized_vol is None
        assert sig.vrp is None
        assert sig.gate_label == STAND_ASIDE

    def test_missing_dte_keeps_none(self):
        # M3: a missing dte must be None (not a sentinel 0 that masquerades as real).
        cyc = _cycle(rv=0.10, iv=0.15)
        cyc["dte"] = None
        sig = regime_from_cycle(cyc)
        assert sig.dte is None
        name, _, _ = RegimeRoutingPolicy().select(sig, DEFAULT_ALLOWED)
        assert name is None  # R1 None-dte veto

    def test_trend_strength_property(self):
        assert _signals(trend=-0.4).trend_strength == pytest.approx(0.4)
        assert _signals(trend=None).trend_strength == 0.0

    def test_does_not_read_expiry_spot(self):
        # No-look-ahead: expiry_spot must not flow into spot/iv extraction.
        a = regime_from_cycle(_cycle(rv=0.10, iv=0.15, expiry_spot=24050.0))
        b = regime_from_cycle(_cycle(rv=0.10, iv=0.15, expiry_spot=99999.0))
        assert a == b
        # explicit: expiry_spot never becomes the regime spot/iv.
        assert a.spot == 24000.0 and a.implied_vol == 0.15


# ===========================================================================
# 5. one-position discipline + run() aggregation
# ===========================================================================
@needs_orch
class TestRunDiscipline:
    def _cycles(self) -> list[dict[str, Any]]:
        return [
            _cycle(rv=0.10, iv=0.15, entry=date(2025, 1, 2), expiry=date(2025, 1, 9)),   # SELL → trade
            _cycle(rv=0.20, iv=0.15, entry=date(2025, 1, 9), expiry=date(2025, 1, 16)),  # BUY → aside
            _cycle(rv=0.14, iv=0.15, entry=date(2025, 1, 16), expiry=date(2025, 1, 23)),  # STAND → aside
            _cycle(rv=0.09, iv=0.16, entry=date(2025, 1, 23), expiry=date(2025, 1, 30)),  # SELL → trade
        ]

    def test_one_decision_per_cycle_trades_le_cycles(self):
        orch = FnoOrchestrator(RegimeRoutingPolicy(), index=NIFTY)
        res = orch.run(self._cycles())
        assert len(res.decisions) == 4
        assert res.n_traded + res.n_stand_aside == 4
        assert len(res.trades) <= 4
        assert res.n_traded == 2
        assert res.n_stand_aside == 2

    def test_metrics_rom_headline_present(self):
        orch = FnoOrchestrator(RegimeRoutingPolicy(), index=NIFTY)
        res = orch.run(self._cycles())
        m = res.metrics
        assert "return_on_margin" in m
        assert "rom_oos" in m
        assert "go_no_go" in m
        total_span = sum(t.span for t in res.trades)
        if total_span > 0:
            assert m["return_on_margin"] == pytest.approx(m["net_pnl"] / total_span)

    def test_participation_rate(self):
        # M4: participation_rate = n_traded / n_cycles, present + correct.
        orch = FnoOrchestrator(RegimeRoutingPolicy(), index=NIFTY)
        res = orch.run(self._cycles())
        assert res.metrics["participation_rate"] == pytest.approx(2 / 4)
        assert "rom_deployment_norm" in res.metrics

    def test_metrics_gate_label_reflects_mode(self):
        # L9: _aggregate must stamp the actual gate mode, not a hardcoded "vol".
        orch_none = FnoOrchestrator(RegimeRoutingPolicy(), index=NIFTY, gate="none")
        res = orch_none.run(self._cycles())
        assert res.metrics["gate"] == "none"

    def test_zero_trades_metrics_has_rom_oos(self):
        # L4: zero-trades metrics dict must carry rom_oos (0.0) for consistency.
        orch = FnoOrchestrator(RegimeRoutingPolicy(), index=NIFTY)
        # all BUY_PREMIUM → all stand aside → zero trades under gate="vol".
        cycles = [_cycle(rv=0.20, iv=0.15, entry=date(2025, 1, 2 + i)) for i in range(3)]
        res = orch.run(cycles)
        assert res.metrics["n_trades"] == 0
        assert res.metrics["rom_oos"] == 0.0
        assert res.metrics["participation_rate"] == 0.0


# ===========================================================================
# 6. A/B parity — pinned single strategy reproduces standalone trades / ROM
# ===========================================================================
@needs_orch
class TestABParity:
    def _sell_cycles(self) -> list[dict[str, Any]]:
        return [
            _cycle(rv=0.10, iv=0.16, spot=24000, expiry_spot=24010,
                   entry=date(2025, 1, 2), expiry=date(2025, 1, 9)),
            _cycle(rv=0.09, iv=0.15, spot=24100, expiry_spot=24300,
                   entry=date(2025, 1, 9), expiry=date(2025, 1, 16)),
            _cycle(rv=0.11, iv=0.17, spot=23900, expiry_spot=23700,
                   entry=date(2025, 1, 16), expiry=date(2025, 1, 23)),
        ]

    def test_pinned_iron_condor_matches_standalone(self):
        cycles = self._sell_cycles()
        ic_params = {"move_mult": 1.5, "wing_strikes": 2}

        class _PinIC:
            def select(self, signals, allowed):
                if signals.gate_label == SELL_PREMIUM:
                    return ("iron_condor", dict(ic_params), "pin")
                return (None, None, "aside")

        # H4: orchestrator runs gate="none" internally; mirror that on the standalone
        # side with gate="none" so parity holds by construction (not by coincidence).
        orch = FnoOrchestrator(_PinIC(), index=NIFTY, k=0.9, capital=200_000.0)
        res = orch.run(cycles)

        standalone = run_strategy_backtest(
            FNO_STRATEGIES["iron_condor"],
            cycles,
            ic_params,
            k=0.9,
            capital=200_000.0,
            lot=NIFTY.lot,
            step=NIFTY.step,
            gate="none",  # mirror the orchestrator's dispatch gate
        )

        assert res.metrics["n_trades"] == standalone["n_trades"]
        assert res.metrics["net_pnl"] == pytest.approx(standalone["net_pnl"])
        assert res.metrics["return_on_margin"] == pytest.approx(
            standalone["return_on_margin"]
        )
        assert res.metrics["sharpe"] == pytest.approx(standalone["sharpe"])
        # M7: sharpe_is / sharpe_oos parity too.
        assert res.metrics["sharpe_is"] == pytest.approx(standalone["sharpe_is"])
        assert res.metrics["sharpe_oos"] == pytest.approx(standalone["sharpe_oos"])
        # OOS ROM parity (orchestrated rom_oos == single's computed rom_oos).
        assert res.metrics["rom_oos"] == pytest.approx(
            _rom_oos_from_trades(standalone["trades"])
        )
        # per-trade parity
        assert [t.net_pnl for t in res.trades] == pytest.approx(
            [t.net_pnl for t in standalone["trades"]]
        )

    def test_mixed_cycle_pinned_parity_with_gate_none_standalone(self):
        """M8: a mixed-cycle fixture (SELL + BUY + STAND_ASIDE). The pinned
        orchestrator (which honours its policy's R0 stand-aside) trades only the
        SELL cycles, while standalone(gate='none') trades ALL — so the orchestrator
        is a STRICT subset, proving it skips cycles the ungated single would take."""
        cycles = [
            _cycle(rv=0.10, iv=0.16, spot=24000, expiry_spot=24010,
                   entry=date(2025, 1, 2), expiry=date(2025, 1, 9)),    # SELL
            _cycle(rv=0.20, iv=0.15, spot=24100, expiry_spot=24300,
                   entry=date(2025, 1, 9), expiry=date(2025, 1, 16)),   # BUY
            _cycle(rv=0.14, iv=0.15, spot=23900, expiry_spot=23700,
                   entry=date(2025, 1, 16), expiry=date(2025, 1, 23)),  # STAND_ASIDE
            _cycle(rv=0.09, iv=0.17, spot=24200, expiry_spot=24250,
                   entry=date(2025, 1, 23), expiry=date(2025, 1, 30)),  # SELL
        ]
        ic_params = {"move_mult": 1.5, "wing_strikes": 2}

        class _PinIC:
            def select(self, signals, allowed):
                if signals.gate_label == SELL_PREMIUM:
                    return ("iron_condor", dict(ic_params), "pin")
                return (None, None, "aside")

        orch = FnoOrchestrator(_PinIC(), index=NIFTY, k=0.9)
        res = orch.run(cycles)

        standalone_none = run_strategy_backtest(
            FNO_STRATEGIES["iron_condor"], cycles, ic_params,
            k=0.9, lot=NIFTY.lot, step=NIFTY.step, gate="none",
        )
        # orchestrator skips (only 2 SELL cycles), standalone(none) trades all valid.
        assert res.metrics["n_trades"] == 2
        assert standalone_none["n_trades"] == 4
        assert res.metrics["n_trades"] < standalone_none["n_trades"]
        # the orchestrator's traded set is the SELL subset.
        sell_pnls = {
            t.entry_date: t.net_pnl
            for t in standalone_none["trades"]
            if t.entry_date in (date(2025, 1, 2), date(2025, 1, 23))
        }
        for t in res.trades:
            assert t.net_pnl == pytest.approx(sell_pnls[t.entry_date])


# ===========================================================================
# 7. Index-agnostic — lot/step forwarded to the engine
# ===========================================================================
@needs_orch
class TestIndexAgnostic:
    def test_lot_step_forwarded(self, monkeypatch):
        import research.backtest.fno_orchestrator as orch_mod

        captured: dict[str, Any] = {}

        def _fake_rsb(spec, cycles, params=None, **kw):
            captured["lot"] = kw.get("lot")
            captured["step"] = kw.get("step")
            return {"trades": []}

        monkeypatch.setattr(orch_mod, "run_strategy_backtest", _fake_rsb)
        synthetic = IndexParams(
            symbol="SYN", index_security_id="999", vix_id="998", lot=40, step=100,
            iv_source="vix", expiry_mode="weekly", has_history=True,
        )
        orch = FnoOrchestrator(RegimeRoutingPolicy(), index=synthetic)
        sig = _signals(gate=SELL_PREMIUM, trend=None)
        orch.dispatch(orch.decide(sig), _cycle(rv=0.10, iv=0.15))
        assert captured["lot"] == 40
        assert captured["step"] == 100

    def test_registry_uses_index_security_id(self):
        # L3: the registry field is index_security_id (spec 04), not nifty_id.
        assert NIFTY.index_security_id == "13"
        assert NIFTY.vix_id == "21"


# ===========================================================================
# 8. run_comparison — OOS ROM headline + slip threading + gate modes
# ===========================================================================
@needs_orch
class TestRunComparison:
    def _sell_cycles(self) -> list[dict[str, Any]]:
        # Enough SELL cycles for a non-trivial 70/30 OOS slice.
        out = []
        d = date(2025, 1, 2)
        from datetime import timedelta
        for i in range(8):
            entry = d + timedelta(days=7 * i)
            expiry = entry + timedelta(days=5)
            out.append(_cycle(rv=0.09, iv=0.16, spot=24000 + 10 * i,
                              expiry_spot=24000 + 12 * i, entry=entry, expiry=expiry))
        return out

    def test_singles_carry_rom_oos(self):
        cmp = run_comparison(self._sell_cycles(), index=NIFTY, k=0.9)
        for name in GO_SET:
            assert "rom_oos" in cmp["singles"][name]

    def test_best_single_picked_by_oos_rom(self):
        # H1: best_single is the max-OOS-ROM single, not max full-sample ROM.
        cmp = run_comparison(self._sell_cycles(), index=NIFTY, k=0.9)
        best_name = cmp["best_single"]
        best_oos = cmp["singles"][best_name]["rom_oos"]
        for name in GO_SET:
            assert cmp["singles"][name]["rom_oos"] <= best_oos + 1e-12

    def test_slip_pct_threaded_to_singles_and_orchestrated(self, monkeypatch):
        # H5: slip_pct must reach BOTH the singles and the orchestrated dispatch.
        import research.backtest.fno_orchestrator as orch_mod

        seen_slip: list[float] = []
        real = orch_mod.run_strategy_backtest

        def _spy(spec, cycles, params=None, **kw):
            seen_slip.append(kw.get("slip_pct"))
            return real(spec, cycles, params, **kw)

        monkeypatch.setattr(orch_mod, "run_strategy_backtest", _spy)
        run_comparison(self._sell_cycles(), index=NIFTY, k=0.9, slip_pct=0.02)
        # every engine call (singles + per-cycle dispatch) saw slip_pct=0.02.
        assert seen_slip  # non-empty
        assert all(s == 0.02 for s in seen_slip)

    def test_gate_none_row_produced(self):
        # M5 / --gate none: a gate='none' comparison runs the singles + orch ungated.
        cmp = run_comparison(self._sell_cycles(), index=NIFTY, k=0.9, gate="none")
        assert cmp["gate"] == "none"
        assert cmp["orchestrated"]["gate"] == "none"
        # ungated → orchestrator deploys on every valid cycle.
        assert cmp["orchestrated"]["n_trades"] >= cmp["orchestrated"]["n_cycles"] - 1

    def test_allowed_subset_restricts_singles(self):
        # M5 --strategies subset: only the allowed names appear in singles.
        allowed = frozenset({"iron_condor", "bull_put_spread"})
        cmp = run_comparison(self._sell_cycles(), index=NIFTY, k=0.9,
                             allowed_strategies=allowed)
        assert set(cmp["singles"]) == allowed


# ===========================================================================
# 9. Pure sidecar regime helpers (DB-free) — iv_rank + trend_slope (W1)
# ===========================================================================
@needs_orch
class TestRegimeHelpers:
    def test_iv_rank_percentile(self):
        hist = [0.10 + 0.001 * i for i in range(100)]  # 100 ascending obs
        # today at the median → ~0.5 percentile.
        r = iv_rank(0.10 + 0.001 * 50, hist)
        assert r == pytest.approx(0.50, abs=0.02)

    def test_iv_rank_short_history_returns_none(self):
        assert iv_rank(0.15, [0.12, 0.13, 0.14]) is None  # < min_periods

    def test_iv_rank_high(self):
        hist = [0.10 + 0.001 * i for i in range(100)]
        assert iv_rank(0.50, hist) == pytest.approx(1.0)  # above everything

    def test_trend_slope_positive(self):
        # rising series → spot above its 20DMA → positive slope.
        closes = [100.0 + i for i in range(30)]
        s = trend_slope(closes)
        assert s is not None and s > 0

    def test_trend_slope_negative(self):
        closes = [200.0 - i for i in range(30)]
        s = trend_slope(closes)
        assert s is not None and s < 0

    def test_trend_slope_short_history_none(self):
        assert trend_slope([100.0, 101.0, 102.0]) is None  # < ma_window


# ===========================================================================
# 10. _rom_oos_from_trades — same 70/30 split as the engine
# ===========================================================================
@needs_orch
class TestRomOos:
    def test_empty_trades_zero(self):
        assert _rom_oos_from_trades([]) == 0.0

    def test_oos_split_matches_engine_for_single(self):
        # The engine's sharpe_oos uses the same split; rom_oos must agree on slice.
        from datetime import timedelta
        cycles = []
        d = date(2025, 1, 2)
        for i in range(10):
            entry = d + timedelta(days=7 * i)
            cycles.append(_cycle(rv=0.09, iv=0.16, spot=24000 + 10 * i,
                                expiry_spot=24000 + 11 * i, entry=entry,
                                expiry=entry + timedelta(days=5)))
        m = run_strategy_backtest(
            FNO_STRATEGIES["iron_condor"], cycles, {"move_mult": 1.5, "wing_strikes": 2},
            k=0.9, lot=NIFTY.lot, step=NIFTY.step, gate="none",
        )
        trades = m["trades"]
        n = len(trades)
        split_idx = max(1, int(0.7 * n))
        oos = sorted(trades, key=lambda t: t.entry_date)[split_idx:]
        expected = sum(t.net_pnl for t in oos) / sum(t.span for t in oos)
        assert _rom_oos_from_trades(trades) == pytest.approx(expected)
