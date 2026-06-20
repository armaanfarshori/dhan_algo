"""Unit tests for research/backtest/fno_orchestrator.py — the F&O selection layer.

All tests are PURE: synthetic regimes / hand-built cycle dicts, no DB, no network.
The module's only DB touch is ``cycles_for_index`` (not exercised here — ``run`` takes
cycles directly).

Test map (PR-1 / specs 02, 03, 06):
  - Routing policy correctness: each regime → expected strategy + stand-aside (R0–R7).
  - Defined-risk whitelist: undefined-risk allowed-set raises at construction.
  - gate="none" dispatch: the chosen strategy is not double-gated by the engine.
  - A/B parity: orchestrator pinned to one strategy reproduces that strategy's standalone
    trades / ROM (the glue adds no edge).
  - decide() determinism, one-position discipline, closure (never a NO-GO family).
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
        regime_from_cycle,
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

    def test_r2_vrp_below_floor_stands_aside(self):
        pol = RegimeRoutingPolicy(RoutingParams(vrp_min=0.10))
        name, _, _ = pol.select(_signals(vrp=0.02), DEFAULT_ALLOWED)
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

    def test_r6_neutral_skew_routes_broken_wing(self):
        # trend present but weak (between skew and strong) → broken_wing_condor.
        assert self._select(_signals(trend=0.4)) == "broken_wing_condor"

    def test_r7_neutral_default_routes_iron_condor(self):
        # SELL_PREMIUM, no trend signal (MVP regime) → workhorse condor.
        assert self._select(_signals(trend=None)) == "iron_condor"
        # explicit flat trend below skew threshold → also condor.
        assert self._select(_signals(trend=0.05)) == "iron_condor"

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
    def test_default_allowed_all_defined_risk_and_go(self):
        for name in DEFAULT_ALLOWED:
            assert name in FNO_STRATEGIES
            assert FNO_STRATEGIES[name].defined_risk is True
        assert frozenset(GO_SET) == DEFAULT_ALLOWED

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
        """The orchestrator must call run_strategy_backtest with gate='none'.

        Patch the engine call in the orchestrator namespace and capture the kwarg.
        """
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
        """Real engine path: a cycle the engine's vol-gate would skip under
        gate='vol' still produces a trade through the orchestrator (gate='none').
        """
        # BUY_PREMIUM cycle (rv > iv): engine gate='vol' would skip a sell-premium
        # spec. But if we (artificially) decide to deploy, gate='none' must trade it.
        cyc = _cycle(rv=0.20, iv=0.15)  # rv > iv → BUY_PREMIUM
        orch = FnoOrchestrator(RegimeRoutingPolicy(), index=NIFTY)
        # Hand-build a non-stand-aside decision (bypassing the policy's R0 gate).
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


# ===========================================================================
# 4. regime_from_cycle correctness
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

    def test_does_not_read_expiry_spot(self):
        # No-look-ahead: expiry_spot must not influence the regime read.
        a = regime_from_cycle(_cycle(rv=0.10, iv=0.15, expiry_spot=24050.0))
        b = regime_from_cycle(_cycle(rv=0.10, iv=0.15, expiry_spot=99999.0))
        assert a == b


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
        # 2 SELL_PREMIUM cycles → 2 deployed, 2 aside.
        assert res.n_traded == 2
        assert res.n_stand_aside == 2

    def test_metrics_rom_headline_present(self):
        orch = FnoOrchestrator(RegimeRoutingPolicy(), index=NIFTY)
        res = orch.run(self._cycles())
        m = res.metrics
        assert "return_on_margin" in m
        assert "rom_oos" in m
        assert "go_no_go" in m
        # ROM == net_pnl / Σ span (engine convention).
        total_span = sum(t.span for t in res.trades)
        if total_span > 0:
            assert m["return_on_margin"] == pytest.approx(m["net_pnl"] / total_span)


# ===========================================================================
# 6. A/B parity — pinned single strategy reproduces standalone trades / ROM
# ===========================================================================
@needs_orch
class TestABParity:
    def _sell_cycles(self) -> list[dict[str, Any]]:
        # All SELL_PREMIUM so the engine (gate='vol') and the pinned orchestrator
        # both trade every cycle — apples-to-apples.
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

        # Pin a policy to iron_condor for every SELL_PREMIUM cycle.
        class _PinIC:
            def select(self, signals, allowed):
                if signals.gate_label == SELL_PREMIUM:
                    return ("iron_condor", dict(ic_params), "pin")
                return (None, None, "aside")

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
            gate="vol",  # all cycles SELL → trades every cycle, same as the router
        )

        assert res.metrics["n_trades"] == standalone["n_trades"]
        assert res.metrics["net_pnl"] == pytest.approx(standalone["net_pnl"])
        assert res.metrics["return_on_margin"] == pytest.approx(
            standalone["return_on_margin"]
        )
        assert res.metrics["sharpe"] == pytest.approx(standalone["sharpe"])
        # per-trade parity
        assert [t.net_pnl for t in res.trades] == pytest.approx(
            [t.net_pnl for t in standalone["trades"]]
        )


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
            symbol="SYN", nifty_id="999", vix_id="998", lot=40, step=100,
            iv_source="vix", expiry_mode="weekly", has_history=True,
        )
        orch = FnoOrchestrator(RegimeRoutingPolicy(), index=synthetic)
        sig = _signals(gate=SELL_PREMIUM, trend=None)
        orch.dispatch(orch.decide(sig), _cycle(rv=0.10, iv=0.15))
        assert captured["lot"] == 40
        assert captured["step"] == 100
