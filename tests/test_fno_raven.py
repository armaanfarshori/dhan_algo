"""Tests for research/backtest/fno_raven.py — RAVEN regime-adaptive skew-aware condor.

Pure-function tests only. The module's single DB touch (``cycles_from_db`` in the
CLI) is never exercised here; everything is driven with synthetic in-memory cycles.

TZ-safe: every date is an explicit literal (no date.today()/now() for injected
state), so the suite is deterministic on a UTC CI runner as well as the IST dev
Mac (see memory `ci-ist-date-trap`).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

try:
    from ml.fno_vol_gate import SELL_PREMIUM, STAND_ASIDE, GateV2Config
    from research.backtest.fno_condor import (
        DELTA_IV_SOURCE_FLAT,
        DELTA_IV_SOURCE_REAL,
    )
    from research.backtest.fno_raven import (
        BASELINE_SPEC,
        DEFAULT_CALL_SHORT_DELTA,
        DEFAULT_PUT_SHORT_DELTA,
        RAVEN_SPEC,
        RavenParams,
        build_raven,
        build_raven_strikes,
        build_symmetric_baseline,
        gate_cycles_v2,
        render_report,
        run_raven_backtest,
    )

    _HAS_MOD = True
    _import_error = ""
except Exception as _e:  # noqa: BLE001
    _HAS_MOD = False
    _import_error = str(_e)

pytestmark = pytest.mark.skipif(
    not _HAS_MOD, reason=f"fno_raven not importable: {_import_error!r}"
)


# ---------------------------------------------------------------------------
# Synthetic cycle helpers
# ---------------------------------------------------------------------------
def _sell_cycles(n=60, spot=20000.0, iv=0.14, rvol=0.06, *, per_strike_iv=None):
    """n well-formed cycles where realized << implied → v1 SELL_PREMIUM.

    Chronological weekly cadence. ``atm_straddle_iv`` set so resolve_iv_source
    prefers a real ATM IV; ``per_strike_iv`` optionally attached to every cycle.
    """
    cs = []
    base = date(2026, 1, 6)
    for i in range(n):
        ed = base + timedelta(days=7 * i)
        c = {
            "entry_date": ed,
            "expiry_date": ed + timedelta(days=7),
            "spot": spot + (i % 5) * 50,
            "straddle_iv": iv,
            "atm_straddle_iv": iv,
            "realized_vol_20d": rvol,
            "dte": 7,
            "expiry_spot": spot + (i % 7) * 30,
        }
        if per_strike_iv is not None:
            c["per_strike_iv"] = dict(per_strike_iv)
        cs.append(c)
    return cs


def _skew_per_strike_iv(spot=20000.0, step=50, *, put_iv=0.22, call_iv=0.11):
    """A rich PUT-skew per-strike IV map: OTM puts >> OTM calls."""
    psi = {}
    k = int(round((spot - 4000) / step)) * step
    top = int(round((spot + 4000) / step)) * step
    while k <= top:
        psi[k] = put_iv if k < spot else call_iv
        k += step
    return psi


# Advisory percentile config: history accrues slowly in synthetic sets; turning
# the percentile sub-gate advisory lets the v1 VRP spine drive the SELL decision
# deterministically without look-ahead concerns.
_ADVISORY = GateV2Config(require_percentile=False)


# ===========================================================================
# RAVEN construction — asymmetric strikes, defined-risk bound
# ===========================================================================
def test_raven_put_short_deeper_otm_than_call():
    """Default RAVEN: the put short sits FURTHER OTM than the call short."""
    strikes, _src = build_raven_strikes(20000.0, 0.14, 7, params=RavenParams())
    put_otm = 20000.0 - strikes["short_put_k"]
    call_otm = strikes["short_call_k"] - 20000.0
    assert put_otm > 0 and call_otm > 0
    # put_short_delta (0.12) < call_short_delta (0.18) → put short is deeper OTM.
    assert put_otm > call_otm


def test_raven_asymmetric_wings():
    """Put wing is wider than the call wing by default (3 vs 2 grid steps)."""
    strikes, _src = build_raven_strikes(20000.0, 0.14, 7, params=RavenParams(), step=50)
    put_wing = strikes["short_put_k"] - strikes["long_put_k"]
    call_wing = strikes["long_call_k"] - strikes["short_call_k"]
    assert put_wing == 3 * 50
    assert call_wing == 2 * 50
    assert put_wing > call_wing


def test_raven_defined_risk_bound_holds():
    """RAVEN is always a bounded-loss condor: longs strictly OTM of their shorts."""
    for spot in (15000.0, 20000.0, 24000.0):
        for iv in (0.08, 0.14, 0.25):
            s, _ = build_raven_strikes(spot, iv, 7, params=RavenParams())
            # Long put BELOW short put BELOW short call BELOW long call.
            assert s["long_put_k"] < s["short_put_k"]
            assert s["short_put_k"] < s["short_call_k"]
            assert s["short_call_k"] < s["long_call_k"]
            # Both wings are positive width (defined risk on both sides).
            assert s["short_put_k"] - s["long_put_k"] > 0
            assert s["long_call_k"] - s["short_call_k"] > 0


def test_raven_legs_via_builder_signature():
    """build_raven conforms to the StrategyBuilder signature + canonical leg order."""
    params = dict(RAVEN_SPEC.default_params)
    legs = build_raven(20000.0, 20000, 50, 7, 0.14, params)
    assert len(legs) == 4
    assert [(l.option_type, l.side) for l in legs] == [
        ("PE", "SELL"), ("PE", "BUY"), ("CE", "SELL"), ("CE", "BUY"),
    ]
    # Defined-risk leg ordering.
    sp, lp, sc, lc = (l.strike for l in legs)
    assert lp < sp < sc < lc


def test_raven_params_validation():
    """RavenParams rejects out-of-range deltas and sub-1 wing widths."""
    with pytest.raises(ValueError):
        RavenParams(put_short_delta=0.0)
    with pytest.raises(ValueError):
        RavenParams(call_short_delta=1.0)
    with pytest.raises(ValueError):
        RavenParams(put_wing_strikes=0)
    with pytest.raises(ValueError):
        RavenParams(call_wing_strikes=0)


# ===========================================================================
# Real-IV-skew path vs flat-IV fallback
# ===========================================================================
def test_flat_iv_fallback_source():
    """No per-strike IV → the delta selection uses the flat-IV fallback source."""
    _strikes, src = build_raven_strikes(20000.0, 0.14, 7, params=RavenParams())
    assert src == DELTA_IV_SOURCE_FLAT


def test_real_per_strike_iv_source():
    """Per-strike IV supplied → the source is real_per_strike (skew SEEN)."""
    psi = _skew_per_strike_iv()
    _strikes, src = build_raven_strikes(
        20000.0, 0.14, 7, params=RavenParams(), per_strike_iv=psi,
    )
    assert src == DELTA_IV_SOURCE_REAL


def test_real_skew_pushes_put_short_relative_to_flat():
    """A rich put skew (higher put IV) moves the put short to a DIFFERENT strike
    than the flat-IV placement — i.e. the skew is actually harvested, not ignored."""
    flat, _ = build_raven_strikes(20000.0, 0.14, 7, params=RavenParams())
    psi = _skew_per_strike_iv(put_iv=0.30, call_iv=0.10)
    real, src = build_raven_strikes(
        20000.0, 0.14, 7, params=RavenParams(), per_strike_iv=psi,
    )
    assert src == DELTA_IV_SOURCE_REAL
    # Higher put IV → a given |delta| is reached FURTHER OTM → put short moves
    # away from / not equal to the flat placement on at least one short leg.
    assert (real["short_put_k"], real["short_call_k"]) != (
        flat["short_put_k"], flat["short_call_k"]
    )


# ===========================================================================
# Regime gating wired (GATE-V2)
# ===========================================================================
def test_gate_keeps_sell_premium_cycles():
    """realized << implied + advisory percentile → all cycles pass GATE-V2 SELL."""
    cs = _sell_cycles(40)
    kept, verdicts = gate_cycles_v2(cs, cfg=_ADVISORY)
    assert len(kept) == len(cs)
    assert all(v.decision == SELL_PREMIUM for v in verdicts)


def test_gate_vetoes_event_day():
    """An event-date veto drops that cycle from the gated subset."""
    cs = _sell_cycles(10)
    event = [cs[3]["entry_date"]]
    kept, _v = gate_cycles_v2(cs, cfg=_ADVISORY, event_dates=event)
    assert cs[3]["entry_date"] not in [c["entry_date"] for c in kept]
    assert len(kept) == len(cs) - 1


def test_gate_vetoes_backwardation():
    """Front IV >> next IV (backwardation) → GATE-V2 stands aside on that cycle."""
    cs = _sell_cycles(5)
    # Make every cycle's term structure backwardated: front (this IV ~0.14) > next.
    for c in cs:
        c["next_iv"] = 0.05
    kept, _v = gate_cycles_v2(cs, cfg=_ADVISORY)
    assert kept == []


def test_gate_drops_high_realized_vol():
    """realized > implied → v1 BUY_PREMIUM, not SELL → RAVEN does not deploy."""
    cs = _sell_cycles(10, iv=0.10, rvol=0.40)
    kept, _v = gate_cycles_v2(cs, cfg=_ADVISORY)
    assert kept == []


def test_gate_chronological_no_lookahead():
    """The first cycle's trailing-spread window is empty (no future leakage)."""
    cs = _sell_cycles(20)
    # require_percentile True but thin history → fail-open, still SELL; the point
    # is that gating runs without raising and is order-dependent (PIT).
    cfg = GateV2Config(vrp_min_obs=5)
    kept, verdicts = gate_cycles_v2(cs, cfg=cfg)
    assert len(kept) >= 1
    # Earliest kept verdict had no usable trailing percentile (None) — proving the
    # window excluded the future.
    assert verdicts[0].vrp_percentile is None


# ===========================================================================
# Full backtest + restate comparison
# ===========================================================================
def test_run_raven_backtest_shape():
    """run_raven_backtest returns both legs + the stable restate key set."""
    cs = _sell_cycles(60)
    res = run_raven_backtest(cs, gate_config=_ADVISORY)
    assert res.n_input_cycles == 60
    assert res.n_gated_cycles == 60
    assert res.raven_metrics["n_trades"] == 60
    assert res.baseline_metrics["n_trades"] == 60
    # Restate stable keys.
    for key in ("verdict", "alpha", "beta", "tail", "gates", "overall_pass"):
        assert key in res.restate


def test_restate_uses_symmetric_baseline_as_passive():
    """The α benchmark is the symmetric baseline (α-vs-passive is populated)."""
    cs = _sell_cycles(60)
    res = run_raven_backtest(cs, gate_config=_ADVISORY)
    alpha = res.restate["alpha"]
    assert alpha["passive_rom"] is not None
    assert alpha["alpha_vs_passive"] is not None


def test_raven_differs_from_baseline_structure():
    """RAVEN and the symmetric baseline build genuinely different structures."""
    rparams = dict(RAVEN_SPEC.default_params)
    bparams = dict(BASELINE_SPEC.default_params)
    rl = build_raven(20000.0, 20000, 50, 7, 0.14, rparams)
    bl = build_symmetric_baseline(20000.0, 20000, 50, 7, 0.14, bparams)
    r_strikes = [l.strike for l in rl]
    b_strikes = [l.strike for l in bl]
    assert r_strikes != b_strikes
    # Baseline is symmetric about ATM in distance; RAVEN is not.
    b_put_otm = 20000.0 - bl[0].strike
    b_call_otm = bl[2].strike - 20000.0
    assert b_put_otm == b_call_otm  # symmetric
    r_put_otm = 20000.0 - rl[0].strike
    r_call_otm = rl[2].strike - 20000.0
    assert r_put_otm != r_call_otm  # asymmetric


def test_backtest_real_iv_marks_skew_seen():
    """When per-strike IV is present on cycles, the delta-IV provenance is real."""
    psi = _skew_per_strike_iv()
    cs = _sell_cycles(40, per_strike_iv=psi)
    res = run_raven_backtest(cs, gate_config=_ADVISORY)
    assert res.delta_iv_sources.get(DELTA_IV_SOURCE_REAL, 0) > 0
    assert res.delta_iv_sources.get(DELTA_IV_SOURCE_FLAT, 0) == 0


def test_backtest_flat_iv_marks_skew_blind():
    """No per-strike IV → every priced cycle falls back to flat-IV (skew-blind)."""
    cs = _sell_cycles(40)
    res = run_raven_backtest(cs, gate_config=_ADVISORY)
    assert res.delta_iv_sources.get(DELTA_IV_SOURCE_FLAT, 0) == 40
    assert res.delta_iv_sources.get(DELTA_IV_SOURCE_REAL, 0) == 0


def test_empty_gated_set_yields_no_go():
    """When the gate drops every cycle, the restate is the empty NO-GO series."""
    cs = _sell_cycles(10, iv=0.10, rvol=0.40)  # BUY regime → none kept
    res = run_raven_backtest(cs, gate_config=_ADVISORY)
    assert res.n_gated_cycles == 0
    assert res.raven_metrics["n_trades"] == 0
    assert res.restate["overall_pass"] is False
    assert res.restate["n"] == 0


def test_preliminary_flag_set():
    """The restate carries the PRELIMINARY flag (synthetic = proxy IV provenance)."""
    cs = _sell_cycles(60)
    res = run_raven_backtest(cs, gate_config=_ADVISORY)
    assert res.restate["preliminary"] is True


def test_haircut_series_feeds_iv_gate():
    """A 0.80× IV haircut series is built and the IV_HAIRCUT gate is evaluated
    (not the 'no haircut supplied' fail-closed message)."""
    cs = _sell_cycles(60)
    res = run_raven_backtest(cs, gate_config=_ADVISORY, haircut_iv_mult=0.80)
    iv_gate = res.restate["gates"]["IV_HAIRCUT"]
    assert "no 0.80×IV haircut series supplied" not in iv_gate["detail"]


def test_no_haircut_fails_closed():
    """haircut_iv_mult=None → the IV_HAIRCUT gate is fail-closed NO-GO."""
    cs = _sell_cycles(60)
    res = run_raven_backtest(cs, gate_config=_ADVISORY, haircut_iv_mult=None)
    iv_gate = res.restate["gates"]["IV_HAIRCUT"]
    assert iv_gate["pass"] is False


def test_render_report_runs():
    """render_report produces a non-empty string mentioning RAVEN + the verdict."""
    cs = _sell_cycles(60)
    res = run_raven_backtest(cs, gate_config=_ADVISORY)
    txt = render_report(res)
    assert "RAVEN" in txt
    assert "HONEST RESTATE" in txt


def test_specs_are_defined_risk_sell_premium():
    """Both specs are defined-risk premium sellers on the 'defined' SPAN model."""
    for spec in (RAVEN_SPEC, BASELINE_SPEC):
        assert spec.defined_risk is True
        assert spec.sell_premium is True
        assert spec.needs_multi_expiry is False
        assert spec.span_model == "defined"


def test_default_deltas_are_asymmetric():
    """The default put short delta is deeper (smaller) than the call short delta."""
    assert DEFAULT_PUT_SHORT_DELTA < DEFAULT_CALL_SHORT_DELTA


def test_index_agnostic_step_and_lot():
    """A non-NIFTY-like step (100) builds a valid asymmetric defined-risk RAVEN."""
    strikes, _src = build_raven_strikes(45000.0, 0.16, 5, params=RavenParams(), step=100)
    # All strikes on the 100-grid.
    assert all(v % 100 == 0 for v in strikes.values())
    assert strikes["long_put_k"] < strikes["short_put_k"]
    assert strikes["short_call_k"] < strikes["long_call_k"]


def test_stand_aside_constant_imported():
    """Sanity: the gate module's STAND_ASIDE token is the expected value."""
    assert STAND_ASIDE == "STAND_ASIDE"


# ===========================================================================
# Defined-risk guards (QA: degenerate inputs must never emit a malformed condor)
# ===========================================================================
def test_zero_dte_raises():
    """dte <= 0 collapses both shorts to ATM → rejected (defined-risk guard)."""
    with pytest.raises(ValueError):
        build_raven_strikes(20000.0, 0.14, 0, params=RavenParams())
    with pytest.raises(ValueError):
        build_raven_strikes(20000.0, 0.14, -3, params=RavenParams())


def test_subgrid_spot_raises():
    """An absurd sub-grid spot would push long_put_k <= 0 → rejected."""
    with pytest.raises(ValueError):
        build_raven_strikes(0.01, 0.14, 7, params=RavenParams())


# ===========================================================================
# Real skew flows into the BACKTESTED legs (not just the diagnostics)
# ===========================================================================
def test_real_skew_changes_backtested_strikes():
    """A rich put skew on the cycles must change the ACTUAL traded RAVEN strikes
    vs the flat-IV run — proving the per-strike IV reaches the engine's legs."""
    flat_cs = _sell_cycles(20)
    skew_cs = _sell_cycles(20, per_strike_iv=_skew_per_strike_iv(put_iv=0.30, call_iv=0.10))

    flat_res = run_raven_backtest(flat_cs, gate_config=_ADVISORY)
    skew_res = run_raven_backtest(skew_cs, gate_config=_ADVISORY)

    # Compare the first traded RAVEN trade's short-put strike between the two runs.
    flat_sp = flat_res.raven_metrics["trades"][0].legs[0].strike
    skew_sp = skew_res.raven_metrics["trades"][0].legs[0].strike
    # A genuinely richer put IV reaches a target |delta| further OTM → different
    # short-put strike in the actual backtested legs (not just _delta_iv_source_counts).
    assert flat_sp != skew_sp
    assert skew_res.delta_iv_sources.get(DELTA_IV_SOURCE_REAL, 0) == 20


# ===========================================================================
# GATE-V2 percentile sub-gate veto (the V2-over-V1 upgrade)
# ===========================================================================
def test_percentile_subgate_can_veto_v1_sell():
    """With require_percentile True + enough history, a v1 SELL whose VRP spread
    is in a LOW percentile of its window is vetoed to STAND_ASIDE."""
    # Build cycles whose VRP spread DECLINES over time: early cycles rich, later
    # cycles thin → the latest cycle ranks LOW in its trailing window.
    cs = []
    base = date(2026, 1, 6)
    for i in range(40):
        ed = base + timedelta(days=7 * i)
        # IV shrinks toward RV over time → spread (iv-rv) declines → late cycles
        # sit at a low percentile of the (richer) earlier window.
        iv = 0.28 - i * 0.004
        cs.append({
            "entry_date": ed,
            "expiry_date": ed + timedelta(days=7),
            "spot": 20000.0,
            "straddle_iv": iv,
            "atm_straddle_iv": iv,
            "realized_vol_20d": 0.06,
            "dte": 7,
            "expiry_spot": 20000.0,
        })
    cfg = GateV2Config(vrp_min_obs=10, vrp_rich_percentile=0.50, require_percentile=True)
    kept, _v = gate_cycles_v2(cs, cfg=cfg)
    # Some early-rich cycles pass; some late-thin cycles are percentile-vetoed →
    # the kept set is a STRICT subset (the percentile gate actually bites).
    assert 0 < len(kept) < len(cs)


# ===========================================================================
# _scale_cycle_iv correctness (IV haircut stress integrity)
# ===========================================================================
def test_scale_cycle_iv_scales_all_iv_fields():
    """The haircut scaler scales every IV field + per-strike map, leaves the rest,
    and does not mutate the input cycle."""
    from research.backtest.fno_raven import _scale_cycle_iv

    c = {
        "entry_date": date(2026, 1, 6),
        "spot": 20000.0,
        "dte": 7,
        "straddle_iv": 0.20,
        "atm_straddle_iv": 0.20,
        "next_iv": 0.18,
        "per_strike_iv": {19000: 0.25, 21000: 0.15},
    }
    out = _scale_cycle_iv(c, 0.5)
    assert out["straddle_iv"] == 0.10
    assert out["atm_straddle_iv"] == 0.10
    assert out["next_iv"] == 0.09
    assert out["per_strike_iv"] == {19000: 0.125, 21000: 0.075}
    # Non-IV fields pass through untouched.
    assert out["spot"] == 20000.0
    assert out["dte"] == 7
    # Input not mutated.
    assert c["straddle_iv"] == 0.20
    assert c["per_strike_iv"] == {19000: 0.25, 21000: 0.15}


# ===========================================================================
# _strategy_series duplicate-date guard + restate empty stable keys
# ===========================================================================
def test_duplicate_entry_date_rejected():
    """Two RAVEN trades sharing an entry_date make by-date passive alignment
    ambiguous → _strategy_series raises (mirrors series_from_trades)."""
    from research.backtest.fno_raven import _strategy_series

    class _T:
        def __init__(self, d, pnl, span):
            self.entry_date = d
            self.net_pnl = pnl
            self.span = span
            self.straddle_iv = 0.14
            self.realized_vol_20d = 0.06

    d = date(2026, 1, 6)
    dup = [_T(d, 100.0, 5000.0), _T(d, -50.0, 5000.0)]
    with pytest.raises(ValueError):
        _strategy_series(dup, passive_trades=dup, capital=200_000.0, n_configs_tried=1)


def test_empty_restate_has_stable_keys():
    """An empty gated set yields the full restate key set (no KeyError downstream)."""
    cs = _sell_cycles(10, iv=0.10, rvol=0.40)  # BUY regime → none kept
    res = run_raven_backtest(cs, gate_config=_ADVISORY)
    for key in (
        "verdict", "overall_pass", "failed_gates", "preliminary", "n",
        "beta", "alpha", "tail", "gates", "sharpe",
    ):
        assert key in res.restate
    assert res.restate["n"] == 0


def test_symmetric_baseline_unaffected_by_per_strike_iv_bridge():
    """The engine's per-cycle skew bridge must not change the SYMMETRIC baseline
    structure (it ignores per_strike_iv except for which strike hits the delta)."""
    # The baseline must remain symmetric about ATM regardless of a skew map.
    psi = _skew_per_strike_iv()
    cs = _sell_cycles(10, per_strike_iv=psi)
    res = run_raven_backtest(cs, gate_config=_ADVISORY)
    bl = res.baseline_metrics["trades"][0].legs
    # baseline legs: SELL PE, BUY PE, SELL CE, BUY CE — symmetric wing widths.
    put_wing = bl[0].strike - bl[1].strike
    call_wing = bl[3].strike - bl[2].strike
    assert put_wing == call_wing
