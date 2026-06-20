"""Tests for research/backtest/fno_edge_restate.py.

Pure-function tests only — the module has NO DB / network / model touch; every
test drives the analysis with synthetic in-memory series with KNOWN answers.

TZ-safe: every date is an explicit literal (no date.today()/now() for injected
state), so the suite is deterministic on a UTC CI runner as well as the IST dev
Mac (see memory `ci-ist-date-trap`). The bootstrap / random benchmarks are all
seeded, so the suite is deterministic.
"""

from __future__ import annotations

from datetime import date

import pytest

try:
    from research.backtest.fno_condor import IV_SOURCE_REAL, IV_SOURCE_VIX_PROXY
    from research.backtest.fno_edge_restate import (
        GO,
        NO_GO,
        CycleSeries,
        _margin_weighted_rom,
        _moments,
        _norm_cdf,
        _norm_ppf,
        _percentile,
        alpha_analysis,
        beta_analysis,
        block_bootstrap_rom_ci,
        cvar,
        deflated_sharpe,
        falsification_gates,
        ols_regression,
        random_entry_benchmark,
        regime_breakdown,
        restate_edge,
        series_from_trades,
        tail_analysis,
        walk_forward_oos,
    )

    _HAS_MOD = True
    _import_error = ""
except Exception as _e:  # noqa: BLE001
    _HAS_MOD = False
    _import_error = str(_e)

pytestmark = pytest.mark.skipif(
    not _HAS_MOD, reason=f"fno_edge_restate not importable: {_import_error!r}"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _series(pnl, margin=None, **kw):
    if margin is None:
        margin = [10_000.0] * len(pnl)
    return CycleSeries(pnl=list(pnl), margin=list(margin), **kw)


class _FakeTrade:
    def __init__(self, entry_date, net_pnl, max_loss, iv_source=""):
        self.entry_date = entry_date
        self.net_pnl = net_pnl
        self.max_loss = max_loss
        self.iv_source = iv_source


# ===========================================================================
# CycleSeries validation
# ===========================================================================
def test_cycleseries_length_mismatch_raises():
    with pytest.raises(ValueError, match="margin length"):
        CycleSeries(pnl=[1.0, 2.0], margin=[1.0])
    with pytest.raises(ValueError, match="short_vol_factor length"):
        CycleSeries(pnl=[1.0, 2.0], margin=[1.0, 1.0], short_vol_factor=[1.0])
    with pytest.raises(ValueError, match="iv_source length"):
        CycleSeries(pnl=[1.0], margin=[1.0], iv_source=["a", "b"])


def test_cycleseries_n_configs_must_be_positive():
    with pytest.raises(ValueError, match="n_configs_tried"):
        CycleSeries(pnl=[1.0], margin=[1.0], n_configs_tried=0)


def test_cycleseries_n_property():
    assert _series([1.0, 2.0, 3.0]).n == 3


# ===========================================================================
# β — OLS regression on a synthetic short-vol series
# ===========================================================================
def test_ols_recovers_known_line():
    # y = 3 + 2x exactly → beta=2, alpha=3, R²=1.
    x = [0.0, 1.0, 2.0, 3.0, 4.0]
    y = [3.0 + 2.0 * xi for xi in x]
    reg = ols_regression(y, x)
    assert reg["beta"] == pytest.approx(2.0, abs=1e-9)
    assert reg["alpha"] == pytest.approx(3.0, abs=1e-9)
    assert reg["r_squared"] == pytest.approx(1.0, abs=1e-9)


def test_ols_zero_variance_factor_is_degenerate():
    reg = ols_regression([1.0, 5.0, 9.0], [2.0, 2.0, 2.0])
    assert reg["beta"] == 0.0
    assert reg["alpha"] == pytest.approx(5.0)  # mean(y)
    assert reg["r_squared"] == 0.0


def test_ols_length_mismatch_raises():
    with pytest.raises(ValueError):
        ols_regression([1.0, 2.0], [1.0])


def test_beta_analysis_detects_pure_short_vol_beta():
    # Construct P&L that is exactly a linear function of the short-vol factor →
    # the "edge" is entirely beta: R²=1, alpha-intercept small vs mean.
    factor = [-2.0, -1.0, 0.0, 1.0, 2.0, 3.0]
    pnl = [100.0 + 50.0 * f for f in factor]  # mean=250, alpha-intercept=100
    s = _series(pnl, short_vol_factor=factor)
    b = beta_analysis(s)
    assert b["available"] is True
    assert b["beta"] == pytest.approx(50.0, abs=1e-6)
    assert b["r_squared"] == pytest.approx(1.0, abs=1e-9)
    # alpha-intercept 100 vs mean 250 → 100 < 0.25*250=62.5 is FALSE here, so
    # edge_is_mostly_beta is False even though R²=1 (intercept is large).
    assert b["edge_is_mostly_beta"] is False


def test_beta_analysis_mostly_beta_when_small_intercept():
    factor = [-2.0, -1.0, 0.0, 1.0, 2.0, 3.0]
    pnl = [5.0 + 50.0 * f for f in factor]  # small intercept relative to mean
    s = _series(pnl, short_vol_factor=factor)
    b = beta_analysis(s)
    assert b["r_squared"] == pytest.approx(1.0, abs=1e-9)
    assert b["edge_is_mostly_beta"] is True


def test_beta_analysis_unavailable_without_factor():
    s = _series([1.0, 2.0, 3.0])
    assert beta_analysis(s)["available"] is False


# ===========================================================================
# α — vs passive on a known case + random benchmark
# ===========================================================================
def test_margin_weighted_rom_known():
    assert _margin_weighted_rom([100.0, 200.0], [1000.0, 1000.0]) == pytest.approx(0.15)
    assert _margin_weighted_rom([1.0], [0.0]) == 0.0  # no margin guard


def test_alpha_vs_passive_known_case():
    # Gated keeps the winners; passive (same cycles, no gate) earns less.
    pnl = [300.0, 300.0, 300.0, 300.0]
    passive = [100.0, 100.0, 100.0, 100.0]
    s = _series(pnl, passive_pnl=passive)
    a = alpha_analysis(s)
    # ROM gated = 1200/40000 = 0.03; passive = 400/40000 = 0.01; α = 0.02.
    assert a["gated_rom"] == pytest.approx(0.03)
    assert a["passive_rom"] == pytest.approx(0.01)
    assert a["alpha_vs_passive"] == pytest.approx(0.02)
    assert a["beats_passive"] is True


def test_alpha_vs_passive_when_passive_wins():
    s = _series([50.0] * 4, passive_pnl=[200.0] * 4)
    a = alpha_analysis(s)
    assert a["alpha_vs_passive"] < 0
    assert a["beats_passive"] is False


def test_alpha_no_passive_returns_none():
    a = alpha_analysis(_series([100.0] * 5))
    assert a["passive_rom"] is None
    assert a["alpha_vs_passive"] is None
    assert a["beats_passive"] is None


def test_random_entry_benchmark_is_seeded_deterministic():
    s = _series([float(i) for i in range(20)])
    r1 = random_entry_benchmark(s, seeds=50, rng_seed=7)
    r2 = random_entry_benchmark(s, seeds=50, rng_seed=7)
    assert r1 == r2
    assert r1["p05"] <= r1["mean"] <= r1["p95"]


def test_random_entry_benchmark_empty():
    r = random_entry_benchmark(_series([]))
    assert r == {"mean": 0.0, "p95": 0.0, "p05": 0.0}


# ===========================================================================
# γ / tail — CVaR, worst-cycle, drawdown, max-loss %, moments
# ===========================================================================
def test_cvar_worst_fraction_mean():
    # 10 values; worst 5% = ceil(0.05*10)=1 → the single worst value.
    pnls = [10.0, 20.0, -100.0, 5.0, 8.0, 9.0, 11.0, 12.0, 13.0, 14.0]
    assert cvar(pnls, alpha=0.05) == pytest.approx(-100.0)
    # worst 30% = ceil(0.30*10)=3 → mean of the 3 lowest: -100, 5, 8 → -29.0.
    assert cvar(pnls, alpha=0.30) == pytest.approx((-100.0 + 5.0 + 8.0) / 3.0)


def test_cvar_empty_zero():
    assert cvar([]) == 0.0


def test_tail_analysis_worst_and_maxloss_pct():
    pnl = [500.0, 500.0, -9800.0, 500.0]  # one cycle at ~full max-loss
    margin = [10_000.0] * 4
    s = _series(pnl, margin=margin)
    t = tail_analysis(s)
    assert t["worst_cycle"] == -9800.0
    # -9800 <= -0.98*10000 = -9800 → counts as at-max-loss. 1 of 4 → 0.25.
    assert t["pct_at_max_loss"] == pytest.approx(0.25)
    assert t["n"] == 4
    assert t["max_drawdown"] <= 0.0


def test_moments_normal_like_zero_skew_kurt():
    # Symmetric series → skew ~ 0.
    pnls = [-2.0, -1.0, 0.0, 1.0, 2.0]
    mean, std, skew, kurt = _moments(pnls)
    assert mean == pytest.approx(0.0)
    assert skew == pytest.approx(0.0, abs=1e-9)


def test_moments_left_skew_negative():
    # Big negative tail → negative skew.
    pnls = [1.0, 1.0, 1.0, 1.0, -10.0]
    _m, _s, skew, _k = _moments(pnls)
    assert skew < 0


def test_moments_flat_series():
    mean, std, skew, kurt = _moments([5.0, 5.0, 5.0])
    assert mean == 5.0
    assert (std, skew, kurt) == (0.0, 0.0, 0.0)


# ===========================================================================
# Percentile / norm helpers
# ===========================================================================
def test_percentile_interpolation():
    vals = [0.0, 10.0, 20.0, 30.0, 40.0]
    assert _percentile(vals, 0.0) == 0.0
    assert _percentile(vals, 1.0) == 40.0
    assert _percentile(vals, 0.5) == 20.0
    assert _percentile([], 0.5) == 0.0
    assert _percentile([7.0], 0.9) == 7.0


def test_norm_cdf_known():
    assert _norm_cdf(0.0) == pytest.approx(0.5)
    assert _norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)


def test_norm_ppf_inverts_cdf():
    for p in (0.1, 0.25, 0.5, 0.75, 0.9, 0.975):
        assert _norm_cdf(_norm_ppf(p)) == pytest.approx(p, abs=1e-6)


# ===========================================================================
# Deflated Sharpe
# ===========================================================================
def test_deflated_sharpe_more_trials_lowers_dsr():
    # Same observed Sharpe + sample, more trials → harder benchmark → lower DSR.
    d1 = deflated_sharpe(1.0, n_trials=1, n_obs=100)
    d50 = deflated_sharpe(1.0, n_trials=50, n_obs=100)
    assert d1["dsr"] > d50["dsr"]
    assert d1["sr0_annual"] == 0.0  # single trial → null benchmark is 0
    assert d50["sr0_annual"] > 0.0


def test_deflated_sharpe_high_sharpe_high_dsr():
    d = deflated_sharpe(3.0, n_trials=1, n_obs=200)
    assert d["dsr"] > 0.9


def test_deflated_sharpe_small_sample_guard():
    d = deflated_sharpe(2.0, n_trials=5, n_obs=1)
    assert d["dsr"] == 0.0


def test_deflated_sharpe_zero_sharpe_is_half():
    # Observed SR exactly at the single-trial null → DSR ~ 0.5.
    d = deflated_sharpe(0.0, n_trials=1, n_obs=100)
    assert d["dsr"] == pytest.approx(0.5, abs=1e-6)


# ===========================================================================
# Walk-forward OOS split
# ===========================================================================
def test_walk_forward_split_70_30():
    s = _series([float(i) for i in range(10)])
    wf = walk_forward_oos(s)
    # split_idx = int(0.7*10)=7 → OOS = indices 7,8,9.
    assert wf["oos_start"] == 7
    assert wf["n_oos"] == 3
    assert wf["net_oos"] == pytest.approx(7.0 + 8.0 + 9.0)


def test_walk_forward_purge_shrinks_oos():
    s = _series([float(i) for i in range(10)])
    wf = walk_forward_oos(s, purge=2)
    # oos_start = 7+2 = 9 → only index 9.
    assert wf["oos_start"] == 9
    assert wf["n_oos"] == 1


# ===========================================================================
# Block bootstrap CI
# ===========================================================================
def test_bootstrap_ci_brackets_point_and_is_seeded():
    pnl = [100.0, -50.0, 120.0, -30.0, 90.0, 110.0, -20.0, 80.0]
    s = _series(pnl)
    c1 = block_bootstrap_rom_ci(s, seeds=200, rng_seed=3)
    c2 = block_bootstrap_rom_ci(s, seeds=200, rng_seed=3)
    assert c1 == c2  # seeded → deterministic
    assert c1["lo"] <= c1["point"] <= c1["hi"]
    assert c1["half_width"] >= 0.0


def test_bootstrap_ci_single_cycle_collapses():
    c = block_bootstrap_rom_ci(_series([100.0]))
    assert c["lo"] == c["hi"] == c["point"]
    assert c["half_width"] == 0.0


# ===========================================================================
# Regime breakdown
# ===========================================================================
def test_regime_breakdown_groups_and_roms():
    pnl = [100.0, 100.0, -500.0, -500.0]
    regime = ["calm", "calm", "stress", "stress"]
    s = _series(pnl, regime=regime)
    rb = regime_breakdown(s)
    assert rb["available"] is True
    assert rb["regimes"]["calm"]["n"] == 2
    assert rb["regimes"]["stress"]["rom"] < 0
    assert rb["regimes"]["stress"]["worst_cycle"] == -500.0


def test_regime_breakdown_unavailable():
    assert regime_breakdown(_series([1.0, 2.0]))["available"] is False


# ===========================================================================
# Falsification gates
# ===========================================================================
def _winning_series(n=120, **kw):
    # Mostly-winning condor: small steady wins, occasional bounded loss.
    # n=120 keeps the OOS fold (30% → 36 cycles) above the n>=30 gate.
    pnl = []
    for i in range(n):
        pnl.append(-3000.0 if i % 10 == 0 else 600.0)
    margin = [10_000.0] * n
    return _series(pnl, margin=margin, **kw)


def _all_pass_series(n=120, **kw):
    # Steady, low-variance wins so random subsetting can't beat the full set: the
    # gate's selection (vs a much-worse passive) is the only source of return →
    # BEAT_PASSIVE_AND_RANDOM clears the bootstrap band. Tiny deterministic ripple
    # keeps Sharpe finite (non-zero std) without creating an exploitable tail.
    pnl = [500.0 + (10.0 if i % 2 == 0 else -10.0) for i in range(n)]
    margin = [10_000.0] * n
    return _series(pnl, margin=margin, **kw)


def test_gates_all_pass_on_strong_series():
    s = _all_pass_series(
        120,
        passive_pnl=[-300.0] * 120,   # passive much worse → gate adds α
        regime=(["calm"] * 80 + ["stress"] * 40),
        short_vol_factor=[0.1] * 120,
        iv_source=[IV_SOURCE_REAL] * 120,
    )
    hc = _all_pass_series(120)  # 0.80×IV haircut series still positive
    gates = falsification_gates(s, haircut_series=hc, bootstrap_seeds=200)
    assert gates["WF_OOS"]["pass"] is True
    assert gates["IV_HAIRCUT"]["pass"] is True
    assert gates["REGIME"]["pass"] is True
    assert gates["BEAT_PASSIVE_AND_RANDOM"]["pass"] is True
    assert gates["DEFLATED_SHARPE"]["pass"] is True
    # Each gate carries a GO/NO-GO verdict token.
    for g in gates.values():
        assert g["verdict"] in (GO, NO_GO)


def test_iv_haircut_gate_fails_without_series():
    s = _winning_series(60)
    gates = falsification_gates(s, haircut_series=None, bootstrap_seeds=100)
    assert gates["IV_HAIRCUT"]["pass"] is False
    assert "UNPROVEN" in gates["IV_HAIRCUT"]["detail"]


def test_iv_haircut_gate_fails_when_negative_oos():
    s = _winning_series(60)
    losing_hc = _series([-500.0] * 60)  # haircut wipes the OOS edge
    gates = falsification_gates(s, haircut_series=losing_hc, bootstrap_seeds=100)
    assert gates["IV_HAIRCUT"]["pass"] is False


def test_wf_oos_fails_small_sample():
    s = _winning_series(20)  # n_oos = 20-14 = 6 < 30
    gates = falsification_gates(s, bootstrap_seeds=100)
    assert gates["WF_OOS"]["pass"] is False


def test_regime_gate_fails_without_labels():
    gates = falsification_gates(_winning_series(60), bootstrap_seeds=100)
    assert gates["REGIME"]["pass"] is False
    assert "UNPROVEN" in gates["REGIME"]["detail"]


def test_regime_gate_catastrophic_stress_fails():
    # Stress tercile loses MORE than total deployed margin → catastrophic.
    pnl = [600.0] * 40 + [-30_000.0] * 20
    margin = [10_000.0] * 60
    s = _series(pnl, margin=margin, regime=(["calm"] * 40 + ["stress"] * 20))
    gates = falsification_gates(s, bootstrap_seeds=100)
    # stress ROM = -600000 / 200000 = -3.0 < -1.0 → catastrophic.
    assert gates["REGIME"]["pass"] is False


# ===========================================================================
# Top-level restate_edge
# ===========================================================================
def test_restate_edge_empty():
    out = restate_edge(_series([]))
    assert out["overall_pass"] is False
    assert out["n"] == 0
    assert NO_GO in out["verdict"]


def test_restate_edge_go_when_all_gates_pass():
    s = _all_pass_series(
        120,
        passive_pnl=[-300.0] * 120,
        regime=(["calm"] * 80 + ["stress"] * 40),
        iv_source=[IV_SOURCE_REAL] * 120,
    )
    hc = _all_pass_series(120)
    out = restate_edge(s, haircut_series=hc, bootstrap_seeds=200)
    assert out["overall_pass"] is True
    assert out["verdict"].startswith(GO)
    assert out["preliminary"] is False  # all real IV
    assert out["failed_gates"] == []


def test_restate_edge_preliminary_flag_on_vix_proxy():
    s = _winning_series(60, iv_source=[IV_SOURCE_VIX_PROXY] * 60)
    out = restate_edge(s, bootstrap_seeds=100)
    assert out["preliminary"] is True
    assert "PRELIMINARY" in out["verdict"]


def test_restate_edge_no_go_names_failed_gates():
    # No haircut series + no regime labels → at least those gates fail.
    s = _winning_series(60)
    out = restate_edge(s, bootstrap_seeds=100)
    assert out["overall_pass"] is False
    assert out["verdict"].startswith(NO_GO)
    assert "IV_HAIRCUT" in out["failed_gates"]
    assert "REGIME" in out["failed_gates"]


# ===========================================================================
# series_from_trades
# ===========================================================================
def test_series_from_trades_orders_and_extracts():
    trades = [
        _FakeTrade(date(2026, 1, 8), 500.0, 9_500.0, IV_SOURCE_VIX_PROXY),
        _FakeTrade(date(2026, 1, 1), -200.0, 9_800.0, IV_SOURCE_REAL),
        _FakeTrade(date(2026, 1, 15), 300.0, 9_700.0, IV_SOURCE_REAL),
    ]
    s = series_from_trades(trades)
    # Chronological order by entry_date.
    assert s.pnl == [-200.0, 500.0, 300.0]
    assert s.margin == [9_800.0, 9_500.0, 9_700.0]
    assert s.iv_source == [IV_SOURCE_REAL, IV_SOURCE_VIX_PROXY, IV_SOURCE_REAL]


def test_series_from_trades_passive_aligned_by_date():
    trades = [
        _FakeTrade(date(2026, 1, 1), 100.0, 1000.0),
        _FakeTrade(date(2026, 1, 8), 200.0, 1000.0),
    ]
    passive = [
        _FakeTrade(date(2026, 1, 1), 50.0, 1000.0),
        # 2026-01-08 missing in passive → that cycle's passive pnl = 0.0
    ]
    s = series_from_trades(trades, passive_trades=passive)
    assert s.passive_pnl == [50.0, 0.0]


def test_series_from_trades_default_iv_source_when_missing_attr():
    class _Bare:
        def __init__(self, d, p, m):
            self.entry_date, self.net_pnl, self.max_loss = d, p, m

    s = series_from_trades([_Bare(date(2026, 1, 1), 10.0, 100.0)])
    assert s.iv_source == [""]


# ===========================================================================
# Integration with the real condor engine (light smoke test)
# ===========================================================================
def test_integration_with_condor_run_backtest():
    from research.backtest.fno_condor import run_backtest

    # Build enough cycles that the gate trades and a series is produced.
    cycles = []
    base = date(2026, 1, 6)
    for i in range(40):
        d = date(base.year, base.month, min(28, 1 + (i % 27)))
        cycles.append(
            {
                "entry_date": d,
                "expiry_date": date(2026, 2, 1),
                "spot": 22_000.0,
                "straddle_iv": 0.12,
                "realized_vol_20d": 0.08,  # low realized → gate sells premium
                "dte": 7,
                "expiry_spot": 22_010.0,
            }
        )
    m = run_backtest(cycles)
    s = series_from_trades(m["trades"])
    # The restatement runs end-to-end on real engine trades without error.
    out = restate_edge(s, bootstrap_seeds=50)
    assert "verdict" in out
    assert out["n"] == len(m["trades"])
    assert out["preliminary"] is True  # VIX-proxy IV cycles


def test_tz_safe_deterministic_no_wall_clock():
    # The module must not depend on the wall clock for its analysis: a full
    # restatement built from literal-date trades is byte-identical across runs
    # (seeded bootstrap + no now()/today()). This is the CI IST-vs-UTC guard.
    trades = [
        _FakeTrade(date(2026, 1, 1 + (i % 27)), 600.0 if i % 10 else -3000.0, 10_000.0)
        for i in range(60)
    ]
    s = series_from_trades(trades)
    out1 = restate_edge(s, bootstrap_seeds=100)
    out2 = restate_edge(s, bootstrap_seeds=100)
    assert out1["verdict"] == out2["verdict"]
    assert out1["bootstrap"] == out2["bootstrap"]


# ===========================================================================
# QA-loop additions: previously-uncovered branches + post-fix behaviours
# ===========================================================================
def test_validation_rejects_non_finite_and_negative_margin():
    with pytest.raises(ValueError, match="non-finite"):
        CycleSeries(pnl=[float("nan"), 1.0], margin=[1.0, 1.0])
    with pytest.raises(ValueError, match="non-finite"):
        CycleSeries(pnl=[1.0, 2.0], margin=[float("inf"), 1.0])
    with pytest.raises(ValueError, match="negative"):
        CycleSeries(pnl=[1.0, 2.0], margin=[1.0, -5.0])


def test_norm_ppf_tail_branches():
    # Exercise the lower (p<0.02425) and upper (p>0.97575) Acklam branches.
    assert _norm_ppf(0.99) == pytest.approx(2.326, abs=1e-3)
    assert _norm_ppf(0.01) == pytest.approx(-2.326, abs=1e-3)
    assert _norm_ppf(0.001) == pytest.approx(-3.0902, abs=1e-3)


def test_deflated_sharpe_negative_radicand_clamp():
    # Extreme positive skew × high Sharpe drives the denominator radicand below 0;
    # the clamp must keep it finite (no math-domain error) and return a valid prob.
    d = deflated_sharpe(40.0, n_trials=1, n_obs=50, skew=50.0, kurtosis=0.0)
    assert 0.0 <= d["dsr"] <= 1.0


def test_deflated_sharpe_uses_excess_kurtosis_convention():
    # Gaussian returns have excess kurtosis 0; the denominator term must be
    # (0+2)/4 = +0.5 (raw-kurt 3), NOT (0-1)/4. Verify the radicand sign is right
    # by comparing fat-tailed (excess>0) vs gaussian: more excess kurt → larger
    # denominator → smaller z → lower DSR (for a positive observed Sharpe).
    d_gauss = deflated_sharpe(2.0, n_trials=1, n_obs=100, skew=0.0, kurtosis=0.0)
    d_fat = deflated_sharpe(2.0, n_trials=1, n_obs=100, skew=0.0, kurtosis=10.0)
    assert d_fat["dsr"] < d_gauss["dsr"]


def test_beta_analysis_zero_mean_pnl_branch():
    # mean_pnl == 0 with a high-R² factor → edge_is_mostly_beta uses the R²-only test.
    factor = [-2.0, -1.0, 1.0, 2.0]
    pnl = [50.0 * f for f in factor]  # mean exactly 0, R²=1
    b = beta_analysis(_series(pnl, short_vol_factor=factor))
    assert b["mean_pnl"] == pytest.approx(0.0)
    assert b["edge_is_mostly_beta"] is True


def test_regime_gate_fails_when_stress_label_absent():
    # Labels present but none is "stress" → NO-GO (UNPROVEN), not a silent pass.
    pnl = [600.0] * 40 + [600.0] * 20
    s = _series(pnl, regime=(["calm"] * 40 + ["mid"] * 20))
    gates = falsification_gates(s, bootstrap_seeds=100)
    assert gates["REGIME"]["pass"] is False
    assert "no 'stress'" in gates["REGIME"]["detail"]


def test_wf_oos_gate_fails_when_ci_lower_bound_not_positive():
    # A noisy series with positive point ROM but a CI that straddles 0 must FAIL
    # WF_OOS on the bootstrap-lower-bound requirement (spec G2), even with n_oos>=30.
    pnl = []
    for i in range(120):
        pnl.append(9000.0 if i % 2 == 0 else -8900.0)  # high variance, slim +mean
    s = _series(pnl)
    gates = falsification_gates(s, bootstrap_seeds=300)
    assert gates["WF_OOS"]["pass"] is False
    assert "CI=[" in gates["WF_OOS"]["detail"]


def test_beat_passive_gate_fails_closed_without_passive():
    s = _all_pass_series(120)  # strong but NO passive benchmark supplied
    gates = falsification_gates(s, bootstrap_seeds=200)
    assert gates["BEAT_PASSIVE_AND_RANDOM"]["pass"] is False
    assert "UNPROVEN" in gates["BEAT_PASSIVE_AND_RANDOM"]["detail"]


def test_stress_rom_floor_is_configurable():
    # stress ROM = -0.30; default floor -0.25 → FAIL; looser floor -0.5 → PASS.
    pnl = [600.0] * 80 + [-3000.0] * 40
    margin = [10_000.0] * 120
    s = _series(pnl, margin=margin, regime=(["calm"] * 80 + ["stress"] * 40))
    strict = falsification_gates(s, bootstrap_seeds=100)
    loose = falsification_gates(s, bootstrap_seeds=100, stress_rom_floor=-0.5)
    assert strict["REGIME"]["pass"] is False
    assert loose["REGIME"]["pass"] is True


def test_restate_edge_empty_has_full_key_set():
    out = restate_edge(_series([]))
    for key in (
        "verdict", "overall_pass", "failed_gates", "preliminary", "n",
        "beta", "alpha", "tail", "deflated_sharpe", "walk_forward",
        "bootstrap", "regime", "gates", "sharpe", "iv_source_summary",
    ):
        assert key in out, f"missing key {key!r} in empty-path dict"
    # Consumers can index the rich keys without KeyError.
    assert out["walk_forward"] == {}
    assert out["iv_source_summary"] == {"real": 0, "vix_proxy": 0, "unknown": 0}


def test_iv_source_summary_buckets_sum_to_n():
    # Mixed: real, proxy, and an unrecognised "" token — buckets must total n.
    s = _series(
        [1.0, 2.0, 3.0, 4.0],
        iv_source=[IV_SOURCE_REAL, IV_SOURCE_VIX_PROXY, "", "weird"],
    )
    out = restate_edge(s, bootstrap_seeds=50)
    summ = out["iv_source_summary"]
    assert summ["real"] == 1
    assert summ["vix_proxy"] == 1
    assert summ["unknown"] == 2  # "" and "weird"
    assert summ["real"] + summ["vix_proxy"] + summ["unknown"] == s.n


def test_series_from_trades_rejects_duplicate_entry_dates():
    dup = [
        _FakeTrade(date(2026, 1, 1), 100.0, 1000.0),
        _FakeTrade(date(2026, 1, 1), 200.0, 1000.0),  # same date
    ]
    passive = [_FakeTrade(date(2026, 1, 1), 50.0, 1000.0)]
    with pytest.raises(ValueError, match="duplicate entry_date"):
        series_from_trades(dup, passive_trades=passive)


def test_random_benchmark_population_differs_with_passive():
    # With a much-worse passive book, the random benchmark (which samples the
    # passive population) yields a LOWER mean ROM than self-sampling would.
    pnl = [500.0] * 20
    passive = [-500.0] * 20
    with_passive = _series(pnl, passive_pnl=passive)
    without = _series(pnl)
    r_pass = random_entry_benchmark(with_passive, seeds=100, rng_seed=5)
    r_self = random_entry_benchmark(without, seeds=100, rng_seed=5)
    assert r_pass["mean"] < r_self["mean"]


def test_random_benchmark_clamps_take_fraction_above_one():
    # take_fraction > 1.0 must not raise Sample-larger-than-population.
    r = random_entry_benchmark(_series([1.0, 2.0, 3.0]), take_fraction=1.5, seeds=10)
    assert "mean" in r
