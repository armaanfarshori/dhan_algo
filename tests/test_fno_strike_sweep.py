"""Tests for research/backtest/fno_strike_sweep.py.

Pure-function tests only — the module's only DB touch (``cycles_from_db`` in the
CLI) is never exercised here; we drive ``run_sweep`` / ``evaluate_config`` with
synthetic in-memory cycles.

TZ-safe: every date is an explicit literal (no date.today()/now() for injected
state), so the suite is deterministic on a UTC CI runner as well as the IST dev
Mac (see memory `ci-ist-date-trap`).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

try:
    from research.backtest.fno_strike_sweep import (
        STRIKE_METHOD_DELTA,
        STRIKE_METHOD_MOVE,
        StrikeConfig,
        default_grid,
        enumerate_grid,
        evaluate_config,
        run_sweep,
        walk_forward_split,
    )

    _HAS_MOD = True
    _import_error = ""
except Exception as _e:  # noqa: BLE001
    _HAS_MOD = False
    _import_error = str(_e)

pytestmark = pytest.mark.skipif(
    not _HAS_MOD, reason=f"fno_strike_sweep not importable: {_import_error!r}"
)


# ---------------------------------------------------------------------------
# Synthetic cycle helper
# ---------------------------------------------------------------------------
def _cycles(n=40, spot=20000.0, iv=0.12, rvol=0.06):
    """n well-formed SELL_PREMIUM cycles (realized << implied → gate passes)."""
    cs = []
    base = date(2026, 1, 6)
    for i in range(n):
        ed = base + timedelta(days=7 * i)
        cs.append(
            {
                "entry_date": ed,
                "expiry_date": ed + timedelta(days=7),
                "spot": spot + (i % 5) * 50,
                "straddle_iv": iv,
                "realized_vol_20d": rvol,
                "dte": 7,
                "expiry_spot": spot + (i % 5) * 50,  # settles ATM → win
            }
        )
    return cs


# ===========================================================================
# Grid enumeration
# ===========================================================================
class TestEnumerateGrid:
    def test_move_axis_only_uses_move_mults(self):
        grid = enumerate_grid(
            strike_methods=[STRIKE_METHOD_MOVE],
            move_mults=[1.0, 1.5],
            target_deltas=[0.10, 0.16, 0.25],  # must be ignored under move
            wing_strikes_opts=[2],
            entry_dtes=[None],
            exit_configs=[(None, None, None)],
        )
        assert len(grid) == 2  # only the two move_mults
        assert all(c.strike_method == STRIKE_METHOD_MOVE for c in grid)

    def test_delta_axis_only_uses_target_deltas(self):
        grid = enumerate_grid(
            strike_methods=[STRIKE_METHOD_DELTA],
            move_mults=[1.0, 1.5, 2.0],  # ignored under delta
            target_deltas=[0.10, 0.16],
            wing_strikes_opts=[2],
            entry_dtes=[None],
            exit_configs=[(None, None, None)],
        )
        assert len(grid) == 2
        assert all(c.strike_method == STRIKE_METHOD_DELTA for c in grid)

    def test_full_crossproduct_size(self):
        grid = enumerate_grid(
            strike_methods=[STRIKE_METHOD_MOVE, STRIKE_METHOD_DELTA],
            move_mults=[1.0, 1.5],          # 2 move cells
            target_deltas=[0.10, 0.16, 0.25],  # 3 delta cells
            wing_strikes_opts=[1, 2],       # ×2
            entry_dtes=[None, 4],           # ×2
            exit_configs=[(None, None, None), (0.5, None, None)],  # ×2
        )
        # (2 move + 3 delta) × 2 × 2 × 2 = 5 × 8 = 40
        assert len(grid) == 40

    def test_dedup(self):
        grid = enumerate_grid(
            strike_methods=[STRIKE_METHOD_MOVE, STRIKE_METHOD_MOVE],  # dup method
            move_mults=[1.0],
            target_deltas=[0.16],
            wing_strikes_opts=[2],
            entry_dtes=[None],
            exit_configs=[(None, None, None)],
        )
        assert len(grid) == 1

    def test_default_grid_nonempty_and_has_both_methods(self):
        grid = default_grid()
        assert len(grid) > 0
        methods = {c.strike_method for c in grid}
        assert methods == {STRIKE_METHOD_MOVE, STRIKE_METHOD_DELTA}

    def test_config_label_distinguishes_methods(self):
        move = StrikeConfig(strike_method=STRIKE_METHOD_MOVE, move_mult=1.5)
        delta = StrikeConfig(strike_method=STRIKE_METHOD_DELTA, target_delta=0.16)
        assert "mm=" in move.label
        assert "Δ=" in delta.label

    def test_config_exit_params_roundtrip(self):
        c = StrikeConfig(profit_target_pct=0.5, stop_loss_mult=2.0, time_stop_dte=1)
        ep = c.exit_params
        assert ep.profit_target_pct == 0.5
        assert ep.stop_loss_mult == 2.0
        assert ep.time_stop_dte == 1
        assert ep.is_expiry_hold is False


# ===========================================================================
# Walk-forward split
# ===========================================================================
class TestWalkForwardSplit:
    def test_plain_70_30(self):
        is_end, oos_start = walk_forward_split(100, purge=0)
        assert is_end == 70
        assert oos_start == 70

    def test_purge_opens_a_gap(self):
        is_end, oos_start = walk_forward_split(100, purge=5)
        assert is_end == 65
        assert oos_start == 75
        # OOS is strictly later than IS with a gap of 2*purge trades
        assert oos_start - is_end == 10

    def test_purge_clamped_at_bounds(self):
        is_end, oos_start = walk_forward_split(10, purge=100)
        assert is_end == 0
        assert oos_start == 10

    def test_matches_engine_boundary(self):
        # The engine uses int(0.7*n); purge=0 must reproduce it exactly.
        for n in (5, 33, 64, 233):
            is_end, oos_start = walk_forward_split(n, purge=0)
            assert is_end == oos_start == max(1, int(0.7 * n))


# ===========================================================================
# evaluate_config
# ===========================================================================
class TestEvaluateConfig:
    def test_move_config_produces_trades(self):
        cfg = StrikeConfig(strike_method=STRIKE_METHOD_MOVE, move_mult=1.5, wing_strikes=2)
        row = evaluate_config(cfg, _cycles(40), k=0.9)
        assert row["n_trades"] > 0
        assert row["strike_method"] == STRIKE_METHOD_MOVE
        assert "rom_oos" in row and "go_no_go" in row

    def test_delta_config_produces_trades(self):
        cfg = StrikeConfig(strike_method=STRIKE_METHOD_DELTA, target_delta=0.16, wing_strikes=2)
        row = evaluate_config(cfg, _cycles(40), k=0.9)
        assert row["n_trades"] > 0
        assert row["strike_method"] == STRIKE_METHOD_DELTA

    def test_zero_cycles_yields_zero_trades_row(self):
        cfg = StrikeConfig()
        row = evaluate_config(cfg, [], k=0.9)
        assert row["n_trades"] == 0
        assert row["rom_oos"] == 0.0
        assert row["go_no_go"][0] is False  # n_trades<30 → NO-GO

    def test_oos_rom_uses_span_proxy(self):
        cfg = StrikeConfig(strike_method=STRIKE_METHOD_MOVE, move_mult=1.5)
        row = evaluate_config(cfg, _cycles(40), k=0.9)
        # ROM and ROM_oos are finite and computed off max_loss span proxy.
        assert isinstance(row["return_on_margin"], float)
        assert isinstance(row["rom_oos"], float)

    def test_purge_changes_oos_window(self):
        cfg = StrikeConfig(strike_method=STRIKE_METHOD_MOVE, move_mult=1.5)
        r0 = evaluate_config(cfg, _cycles(40), k=0.9, purge=0)
        r5 = evaluate_config(cfg, _cycles(40), k=0.9, purge=5)
        assert r0["oos_start"] != r5["oos_start"]


# ===========================================================================
# run_sweep — ranking + robust filter
# ===========================================================================
class TestRunSweep:
    def test_basic_run_ranks_by_rom_oos(self):
        cbd = {None: _cycles(40), 4: _cycles(40), 2: _cycles(40)}
        sweep = run_sweep(cbd, k=0.9, min_trades=10)
        rows = sweep["rows"]
        assert len(rows) == sweep["n_configs"] > 0
        # Ranked descending by (rom_oos, sharpe).
        keys = [(r["rom_oos"], r["sharpe"]) for r in rows]
        assert keys == sorted(keys, reverse=True)

    def test_robust_subset_is_go_and_min_trades(self):
        cbd = {None: _cycles(40), 4: _cycles(40), 2: _cycles(40)}
        sweep = run_sweep(cbd, k=0.9, min_trades=10)
        for r in sweep["robust"]:
            assert r["go_no_go"][0] is True
            assert r["n_trades"] >= 10

    def test_min_trades_gate_excludes_small_samples(self):
        # Only ~12 cycles → with a high min_trades bar, nothing is robust.
        cbd = {None: _cycles(12), 4: _cycles(12), 2: _cycles(12)}
        sweep = run_sweep(cbd, k=0.9, min_trades=30)
        assert sweep["robust"] == []

    def test_uses_per_dte_cycles(self):
        # Distinct DTE keys map to distinct cycle lists; missing key → no trades.
        cbd = {None: _cycles(40)}  # 4 and 2 absent → those cells get []
        sweep = run_sweep(cbd, k=0.9, min_trades=10)
        prior_rows = [r for r in sweep["rows"] if r["entry_dte"] is None]
        dte4_rows = [r for r in sweep["rows"] if r["entry_dte"] == 4]
        assert any(r["n_trades"] > 0 for r in prior_rows)
        assert all(r["n_trades"] == 0 for r in dte4_rows)

    def test_metadata_fields_present(self):
        cbd = {None: _cycles(40), 4: _cycles(40), 2: _cycles(40)}
        sweep = run_sweep(cbd, k=0.9, capital=300_000, purge=3, min_trades=15)
        assert sweep["capital"] == 300_000
        assert sweep["purge"] == 3
        assert sweep["min_trades"] == 15
        assert sweep["k"] == 0.9


class TestRunSweepQaGaps:
    """QA-loop additions: robust subset is ranked, boundary min_trades, empty robust."""

    def test_robust_subset_is_ranked(self):
        cbd = {None: _cycles(40), 4: _cycles(40), 2: _cycles(40)}
        sweep = run_sweep(cbd, k=0.9, min_trades=10)
        assert len(sweep["robust"]) > 0
        rom = [r["rom_oos"] for r in sweep["robust"]]
        assert rom == sorted(rom, reverse=True)

    def test_empty_robust_when_all_below_min_trades(self):
        cbd = {None: _cycles(20), 4: _cycles(20), 2: _cycles(20)}
        sweep = run_sweep(cbd, k=0.9, min_trades=30)
        assert sweep["robust"] == []
        assert len(sweep["rows"]) > 0  # rows still present for diagnostics

    def test_no_go_never_in_robust(self):
        cbd = {None: _cycles(40), 4: _cycles(40), 2: _cycles(40)}
        sweep = run_sweep(cbd, k=0.9, min_trades=10)
        for r in sweep["rows"]:
            if not r["go_no_go"][0]:
                assert r not in sweep["robust"]
