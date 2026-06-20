"""Unit tests for research/backtest/fno_risk_sweep.py — the portfolio risk-governor.

All tests are PURE: synthetic ``(net_pnl, span)`` sequences, no DB, no network. The
module's only DB touch (``cycles_from_db`` in the CLI) is never exercised here — the
governor + metric layer take in-memory sequences directly.

Test map:
  - Governor correctness: max_trades caps; dd_limit halts at the right trade; the
    breaching trade is kept; both knobs together; unlimited == full sequence.
  - Metric math: net_pnl / ROM / ROC scale with capital; OOS split matches the engine;
    win_rate / participation; CAGR-ish; max_drawdown sign + %; go_no_go wiring.
  - Edge cases: empty sequence, all-halt (dd=0), single trade, mismatched lengths.
  - Sweep: baseline is the no-governor row; rows ranked by OOS ROM; grid size sane.
"""

from __future__ import annotations

import math

import pytest

try:
    from research.backtest.fno_risk_sweep import (
        DEFAULT_CAPITAL,
        GovernorParams,
        apply_governor,
        base_sequence,
        default_grid,
        evaluate_combo,
        metrics_for_combo,
        render_sweep,
        run_sweep,
    )

    _HAS_MOD = True
except (ImportError, AttributeError) as _e:  # pragma: no cover
    _HAS_MOD = False
    _import_error = str(_e)

needs_mod = pytest.mark.skipif(
    not _HAS_MOD,
    reason=f"fno_risk_sweep not importable: {'' if _HAS_MOD else _import_error!r}",  # type: ignore[name-defined]
)

CAP = 500_000.0


# ---------------------------------------------------------------------------
# Governor correctness
# ---------------------------------------------------------------------------
@needs_mod
def test_no_governor_keeps_full_sequence():
    pnls = [100.0, -50.0, 200.0, -30.0]
    spans = [1000.0, 1000.0, 1000.0, 1000.0]
    run = apply_governor(pnls, spans, GovernorParams(capital=CAP))
    assert run.n_available == 4
    assert run.n_deployed == 4
    assert run.kept_pnls == pnls
    assert run.kept_spans == spans
    assert run.halt_reason == ""


@needs_mod
def test_max_trades_caps_count():
    pnls = [10.0] * 10
    spans = [100.0] * 10
    run = apply_governor(pnls, spans, GovernorParams(capital=CAP, max_trades=3))
    assert run.n_deployed == 3
    assert run.kept_pnls == [10.0, 10.0, 10.0]
    assert "max_trades=3" in run.halt_reason


@needs_mod
def test_max_trades_none_is_unlimited():
    pnls = [1.0] * 5
    spans = [1.0] * 5
    run = apply_governor(pnls, spans, GovernorParams(capital=CAP, max_trades=None))
    assert run.n_deployed == 5
    assert run.halt_reason == ""


@needs_mod
def test_max_trades_larger_than_sequence_is_noop():
    pnls = [1.0, 2.0]
    spans = [1.0, 1.0]
    run = apply_governor(pnls, spans, GovernorParams(capital=CAP, max_trades=100))
    assert run.n_deployed == 2
    assert run.halt_reason == ""


@needs_mod
def test_dd_limit_halts_at_right_trade_and_keeps_breaching_trade():
    # capital=100k, dd_limit=10% → threshold ₹10,000.
    # Equity: +5000 (peak 5000), then -16000 → cum=-11000, dd=16000 > 10000 → HALT.
    # The losing trade is KEPT (already open); trade #3 never opens.
    pnls = [5_000.0, -16_000.0, 9_999.0]
    spans = [1.0, 1.0, 1.0]
    run = apply_governor(
        pnls, spans, GovernorParams(capital=100_000.0, dd_limit_pct=10.0)
    )
    assert run.n_deployed == 2  # breaching trade kept, third never opened
    assert run.kept_pnls == [5_000.0, -16_000.0]
    assert "dd_limit=10%" in run.halt_reason
    assert "trade 2" in run.halt_reason


@needs_mod
def test_dd_limit_not_breached_runs_full():
    # Drawdown never exceeds 10% of 100k (₹10k).
    pnls = [1_000.0, -2_000.0, 5_000.0, -3_000.0]
    spans = [1.0, 1.0, 1.0, 1.0]
    run = apply_governor(
        pnls, spans, GovernorParams(capital=100_000.0, dd_limit_pct=10.0)
    )
    assert run.n_deployed == 4
    assert run.halt_reason == ""


@needs_mod
def test_dd_limit_zero_halts_on_first_loss():
    # dd_limit=0% → ANY drawdown > 0 halts. First trade is a loss → dd>0 immediately.
    pnls = [-1.0, 100.0, 100.0]
    spans = [1.0, 1.0, 1.0]
    run = apply_governor(
        pnls, spans, GovernorParams(capital=100_000.0, dd_limit_pct=0.0)
    )
    assert run.n_deployed == 1
    assert run.kept_pnls == [-1.0]


@needs_mod
def test_both_knobs_whichever_bites_first():
    pnls = [10.0] * 10
    spans = [1.0] * 10
    # max_trades=2 bites before any dd (all gains, dd never breaches).
    run = apply_governor(
        pnls, spans, GovernorParams(capital=CAP, max_trades=2, dd_limit_pct=10.0)
    )
    assert run.n_deployed == 2
    assert "max_trades=2" in run.halt_reason


@needs_mod
def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        apply_governor([1.0, 2.0], [1.0], GovernorParams(capital=CAP))


@needs_mod
def test_empty_sequence():
    run = apply_governor([], [], GovernorParams(capital=CAP))
    assert run.n_available == 0
    assert run.n_deployed == 0
    assert run.kept_pnls == []


# ---------------------------------------------------------------------------
# Metric math
# ---------------------------------------------------------------------------
@needs_mod
def test_metrics_basic_net_rom_roc():
    pnls = [100.0, 100.0, -50.0, 100.0]   # net = 250
    spans = [1_000.0, 1_000.0, 1_000.0, 1_000.0]  # Σspan = 4000
    run = apply_governor(pnls, spans, GovernorParams(capital=CAP))
    m = metrics_for_combo(run, GovernorParams(capital=CAP), strategy="iron_condor", gate="vol")
    assert m["net_pnl"] == pytest.approx(250.0)
    assert m["return_on_margin"] == pytest.approx(250.0 / 4000.0)
    assert m["return_on_capital"] == pytest.approx(250.0 / CAP)
    assert m["mean_span"] == pytest.approx(1000.0)
    assert m["n_trades"] == 4
    assert m["win_rate"] == pytest.approx(0.75)
    assert m["participation"] == pytest.approx(1.0)


@needs_mod
def test_roc_scales_with_capital():
    pnls = [1_000.0, 2_000.0]
    spans = [1.0, 1.0]
    run = apply_governor(pnls, spans, GovernorParams(capital=CAP))
    m_500k = metrics_for_combo(run, GovernorParams(capital=500_000.0), strategy="x", gate="vol")
    m_1m = metrics_for_combo(run, GovernorParams(capital=1_000_000.0), strategy="x", gate="vol")
    # Same net P&L; ROC halves when capital doubles.
    assert m_500k["net_pnl"] == m_1m["net_pnl"]
    assert m_1m["return_on_capital"] == pytest.approx(m_500k["return_on_capital"] / 2)


@needs_mod
def test_max_drawdown_sign_and_pct():
    # +1000 (peak), -4000 → cum=-3000, dd from peak = 4000. Worst dd = -4000.
    pnls = [1_000.0, -4_000.0, 500.0]
    spans = [1.0, 1.0, 1.0]
    run = apply_governor(pnls, spans, GovernorParams(capital=100_000.0))
    m = metrics_for_combo(run, GovernorParams(capital=100_000.0), strategy="x", gate="vol")
    assert m["max_drawdown"] == pytest.approx(-4_000.0)
    assert m["max_drawdown_pct"] == pytest.approx(4_000.0 / 100_000.0)


@needs_mod
def test_oos_split_matches_engine_70_30():
    # 10 trades → split_idx = int(0.7*10) = 7 → OOS = last 3.
    pnls = [float(i) for i in range(1, 11)]   # 1..10
    spans = [10.0] * 10
    run = apply_governor(pnls, spans, GovernorParams(capital=CAP))
    m = metrics_for_combo(run, GovernorParams(capital=CAP), strategy="x", gate="vol")
    # OOS trades = [8,9,10], spans 10 each → ROM_oos = 27 / 30.
    assert m["rom_oos"] == pytest.approx((8 + 9 + 10) / 30.0)


@needs_mod
def test_cagr_ish_annualises():
    # 52 weekly trades, ROC=10% → CAGR ≈ ROC (one year). (1.1)^(52/52)-1 = 0.10.
    pnls = [CAP * 0.10 / 52] * 52
    spans = [1.0] * 52
    run = apply_governor(pnls, spans, GovernorParams(capital=CAP))
    m = metrics_for_combo(run, GovernorParams(capital=CAP), strategy="x", gate="vol")
    assert m["cagr"] == pytest.approx(0.10, abs=1e-6)


@needs_mod
def test_cagr_half_year_annualises_up():
    # 26 trades, ROC=10% over half a year → annualised ≈ (1.1)^2 - 1 = 21%.
    pnls = [CAP * 0.10 / 26] * 26
    spans = [1.0] * 26
    run = apply_governor(pnls, spans, GovernorParams(capital=CAP))
    m = metrics_for_combo(run, GovernorParams(capital=CAP), strategy="x", gate="vol")
    assert m["cagr"] == pytest.approx(1.1 ** 2 - 1, abs=1e-6)
    assert m["cagr"] > m["return_on_capital"]


@needs_mod
def test_zero_trades_metrics():
    run = apply_governor([], [], GovernorParams(capital=CAP))
    m = metrics_for_combo(run, GovernorParams(capital=CAP), strategy="x", gate="vol")
    assert m["n_trades"] == 0
    assert m["net_pnl"] == 0.0
    assert m["return_on_margin"] == 0.0
    assert m["participation"] == 0.0
    assert m["go_no_go"][0] is False  # zero trades fails go_no_go


@needs_mod
def test_single_trade():
    run = apply_governor([1_000.0], [500.0], GovernorParams(capital=CAP))
    m = metrics_for_combo(run, GovernorParams(capital=CAP), strategy="x", gate="vol")
    assert m["n_trades"] == 1
    assert m["net_pnl"] == pytest.approx(1_000.0)
    assert m["sharpe"] == 0.0       # <2 trades → engine returns 0.0
    assert m["sharpe_oos"] == 0.0


@needs_mod
def test_go_no_go_wired_and_passes_on_strong_sequence():
    # 40 winning trades, modest drawdown → should be a GO on ₹500k.
    pnls = [2_000.0, -500.0] * 20  # 40 trades, net positive, small dd
    spans = [10_000.0] * 40
    run = apply_governor(pnls, spans, GovernorParams(capital=CAP))
    m = metrics_for_combo(run, GovernorParams(capital=CAP), strategy="iron_condor", gate="vol")
    assert m["n_trades"] == 40
    assert isinstance(m["go_no_go"], tuple)
    assert m["go_no_go"][0] is True


# ---------------------------------------------------------------------------
# evaluate_combo + sweep
# ---------------------------------------------------------------------------
@needs_mod
def test_evaluate_combo_equals_manual():
    pnls = [100.0, -200.0, 300.0]
    spans = [1.0, 1.0, 1.0]
    gov = GovernorParams(capital=CAP, max_trades=2)
    m = evaluate_combo(pnls, spans, gov, strategy="x", gate="vol")
    assert m["n_trades"] == 2
    assert m["net_pnl"] == pytest.approx(-100.0)  # first two only
    assert m["max_trades"] == 2


@needs_mod
def test_default_grid_has_baseline_first_and_sane_size():
    grid = default_grid(CAP)
    assert grid[0].max_trades is None
    assert grid[0].dd_limit_pct is None
    # 1 baseline + (6*4 - 1 dup) = 24 combos. Sensible 15-25 range upper bound.
    assert len(grid) == 24
    assert all(g.capital == CAP for g in grid)


@needs_mod
def test_run_sweep_baseline_and_ranking(monkeypatch):
    # Stub the engine so run_sweep needs no DB: return fixed trades.
    class _T:
        def __init__(self, net, span):
            self.net_pnl = net
            self.span = span
            self.entry_date = None

    import research.backtest.fno_risk_sweep as mod

    trades = [_T(100.0, 1_000.0), _T(-50.0, 1_000.0), _T(200.0, 1_000.0), _T(-300.0, 1_000.0)]

    def _fake_backtest(spec, cycles, params=None, **kw):
        return {"trades": trades}

    monkeypatch.setattr(mod, "run_strategy_backtest", _fake_backtest)

    sweep = run_sweep("iron_condor", cycles=[], gate="vol", capital=CAP)
    assert sweep["strategy"] == "iron_condor"
    assert sweep["n_available"] == 4
    # Baseline is the no-governor row.
    assert sweep["baseline"]["max_trades"] is None
    assert sweep["baseline"]["dd_limit_pct"] is None
    assert sweep["baseline"]["n_trades"] == 4
    # rows ranked by rom_oos desc.
    roms = [r["rom_oos"] for r in sweep["rows"]]
    assert roms == sorted(roms, reverse=True)
    # render does not crash and includes caveats.
    txt = render_sweep(sweep)
    assert "PAPER only" in txt
    assert "iron_condor" in txt


@needs_mod
def test_base_sequence_uses_engine(monkeypatch):
    class _T:
        def __init__(self, net, span):
            self.net_pnl = net
            self.span = span
            self.entry_date = None

    import research.backtest.fno_risk_sweep as mod

    def _fake_backtest(spec, cycles, params=None, **kw):
        return {"trades": [_T(1.0, 2.0), _T(3.0, 4.0)]}

    monkeypatch.setattr(mod, "run_strategy_backtest", _fake_backtest)
    pnls, spans = base_sequence("iron_condor", cycles=[], gate="none", capital=CAP)
    assert pnls == [1.0, 3.0]
    assert spans == [2.0, 4.0]


@needs_mod
def test_base_sequence_unknown_strategy_raises():
    with pytest.raises(ValueError):
        base_sequence("not_a_strategy", cycles=[], capital=CAP)


@needs_mod
def test_default_capital_constant():
    assert DEFAULT_CAPITAL == 500_000.0
    assert not math.isnan(DEFAULT_CAPITAL)
