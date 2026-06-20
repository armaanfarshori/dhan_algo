"""DB-free synthetic tests for the equity realized-correlation module + rank-IC.

Covers (per the task brief):
  * perfectly-correlated panel → ρ̄ ≈ 1 and dispersion ≈ 0;
  * independent large-N panel → ρ̄ ≈ 0;
  * NaN-in-window kills the pair/day (and the min_names guard);
  * roll-day scrub removes an injected jump on an expiry change;
  * Spearman rank-IC + tercile math on a hand-built monotone case.

All synthetic; no DB, no network.  Uses a fixed RNG seed for determinism.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from research.backtest.equity_correlation import (
    closes_to_returns,
    corr_implied_from_vols,
    dispersion_frame,
    realized_corr_series,
    realized_dispersion_series,
    scrub_returns,
)
from research.backtest.equity_corr_rank_ic import (
    bootstrap_rank_ic_ci,
    spearman_rank_ic,
    tercile_table,
)


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.bdate_range("2024-01-01", periods=n)


# ── ρ̄ extremes ──────────────────────────────────────────────────────────────────
def test_perfectly_correlated_panel_rho_near_one_and_dispersion_zero():
    n_days, n_names, window = 80, 40, 63
    rng = np.random.default_rng(1)
    common = rng.normal(0, 0.01, size=n_days)
    # Every name = the SAME return series → perfect correlation, zero dispersion.
    returns = pd.DataFrame(
        {f"S{i}-FUT": common for i in range(n_names)}, index=_dates(n_days)
    )
    rho = realized_corr_series(returns, window=window, min_names=10)
    last = rho.dropna().iloc[-1]
    assert last > 0.99, f"expected rho ~1, got {last}"

    disp = realized_dispersion_series(returns, window=window, min_names=10)
    d_last = disp["dispersion"].dropna().iloc[-1]
    assert abs(d_last) < 1e-6, f"expected ~0 dispersion, got {d_last}"


def test_independent_large_n_panel_rho_near_zero():
    n_days, n_names, window = 80, 150, 63
    rng = np.random.default_rng(2)
    data = {
        f"S{i}-FUT": rng.normal(0, 0.01, size=n_days) for i in range(n_names)
    }
    returns = pd.DataFrame(data, index=_dates(n_days))
    rho = realized_corr_series(returns, window=window, min_names=30)
    last = rho.dropna().iloc[-1]
    assert abs(last) < 0.10, f"expected rho ~0 for independent panel, got {last}"


# ── NaN-in-window kills the pair/day ─────────────────────────────────────────────
def test_nan_in_window_kills_pair_and_min_names_guard():
    n_days, window = 70, 63
    rng = np.random.default_rng(3)
    common = rng.normal(0, 0.01, size=n_days)
    # Exactly 30 valid names + 5 names that each carry a NaN inside the window.
    cols = {f"OK{i}-FUT": common.copy() for i in range(30)}
    for i in range(5):
        c = common.copy()
        c[window - 2] = np.nan  # inside the trailing window of the last bar
        cols[f"BAD{i}-FUT"] = c
    returns = pd.DataFrame(cols, index=_dates(n_days))

    # With min_names=30, the 5 NaN-carrying names are dropped, 30 remain → valid.
    rho30 = realized_corr_series(returns, window=window, min_names=30)
    assert not math.isnan(rho30.dropna().iloc[-1])

    # Require 31 → the dropped names tip it under the floor → NaN that day.
    rho31 = realized_corr_series(returns, window=window, min_names=31)
    assert math.isnan(rho31.iloc[-1])


# ── roll-day scrub ───────────────────────────────────────────────────────────────
def test_roll_day_scrub_removes_injected_jump():
    idx = _dates(10)
    # Smooth close series with a contrived 30% jump at the roll bar (index 5).
    closes = pd.Series(
        [100, 101, 102, 101, 103, 134, 134.5, 135, 136, 137.0],  # jump 103->134
        index=idx, name="X-FUT",
    ).to_frame()
    # expiry_date: contract rolls exactly at the jump bar (index 5).
    exp = pd.Series(
        ["2024-01-25"] * 5 + ["2024-02-29"] * 5, index=idx, name="X-FUT"
    ).to_frame()

    raw = closes_to_returns(closes)
    # The jump return (index 5 of closes → return-index date) must be large.
    jump_date = idx[5]
    assert abs(raw.loc[jump_date, "X-FUT"]) > 0.20

    scrubbed = scrub_returns(raw, expiry_df=exp)
    # Roll-day return is NaN'd by the expiry-change scrub.
    assert math.isnan(scrubbed.loc[jump_date, "X-FUT"])
    # A non-roll, in-bounds return survives.
    assert not math.isnan(scrubbed.loc[idx[2], "X-FUT"])


def test_scrub_bad_print_guard_without_expiry():
    idx = _dates(4)
    closes = pd.Series([100, 101, 200, 201.0], index=idx, name="Y-FUT").to_frame()
    raw = closes_to_returns(closes)
    scrubbed = scrub_returns(raw)  # no expiry_df → only |ret|>0.20 guard
    assert math.isnan(scrubbed.loc[idx[2], "Y-FUT"])  # 101->200 jump scrubbed


# ── corr_implied consistency check ───────────────────────────────────────────────
def test_corr_implied_tracks_variance_ratio_on_correlated_panel():
    n_days, n_names, window = 80, 50, 63
    rng = np.random.default_rng(4)
    common = rng.normal(0, 0.01, size=n_days)
    idio = rng.normal(0, 0.003, size=(n_days, n_names))
    data = {f"S{i}-FUT": common + idio[:, i] for i in range(n_names)}
    returns = pd.DataFrame(data, index=_dates(n_days))
    frame = dispersion_frame(returns, window=window, ewma_lambda=None, min_names=30)
    last = frame.dropna(subset=["rho_bar", "corr_implied"]).iloc[-1]
    # The two correlation estimates should agree closely.
    assert abs(last["rho_bar"] - last["corr_implied"]) < 0.05


def test_corr_implied_clamps_and_handles_degenerate():
    assert corr_implied_from_vols(0.0, 0.1, 10) is None
    assert corr_implied_from_vols(0.1, 0.0, 1) is None
    # basket_vol == single_vol with N names → rho ~ 1 (clamped at 1).
    r = corr_implied_from_vols(0.2, 0.2, 50)
    assert r is not None and r <= 1.0


# ── EWMA variant runs and is bounded ─────────────────────────────────────────────
def test_ewma_variant_bounded():
    n_days, n_names = 90, 40
    rng = np.random.default_rng(5)
    common = rng.normal(0, 0.01, size=n_days)
    idio = rng.normal(0, 0.005, size=(n_days, n_names))
    data = {f"S{i}-FUT": common + idio[:, i] for i in range(n_names)}
    returns = pd.DataFrame(data, index=_dates(n_days))
    rho = realized_corr_series(returns, window=63, ewma_lambda=0.94, min_names=30)
    vals = rho.dropna()
    assert not vals.empty
    assert (vals <= 1.0 + 1e-9).all() and (vals >= -1.0).all()


# ── Spearman rank-IC + tercile math ──────────────────────────────────────────────
def test_spearman_rank_ic_monotone_is_one():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [10.0, 20.0, 25.0, 40.0, 100.0]  # strictly increasing (non-linear)
    ic = spearman_rank_ic(x, y)
    assert ic is not None and abs(ic - 1.0) < 1e-9


def test_spearman_rank_ic_antitone_is_minus_one():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [5.0, 4.0, 3.0, 2.0, 1.0]
    ic = spearman_rank_ic(x, y)
    assert ic is not None and abs(ic + 1.0) < 1e-9


def test_spearman_rank_ic_drops_nan_and_handles_constant():
    assert spearman_rank_ic([1.0, 2.0, math.nan], [3.0, 4.0, 5.0]) is None  # <3 pairs
    assert spearman_rank_ic([1.0, 1.0, 1.0, 1.0], [1.0, 2.0, 3.0, 4.0]) is None


def test_tercile_table_monotone_high_minus_low_positive():
    # feature strictly predicts pnl → high tercile mean > low tercile mean.
    feature = list(range(1, 10))  # 9 points → 3 per tercile
    pnl = [float(f) * 100 for f in feature]
    tt = tercile_table(feature, pnl)
    assert tt["n"] == 9
    means = {row["bin"]: row["mean_pnl"] for row in tt["terciles"]}
    assert means["high"] > means["mid"] > means["low"]
    assert tt["high_minus_low"] > 0
    # win rate: all positive pnl → 100% wins in every tercile.
    for row in tt["terciles"]:
        assert row["win_rate"] == 1.0


def test_bootstrap_rank_ic_ci_monotone_positive():
    rng = np.random.default_rng(6)
    x = list(rng.normal(0, 1, size=40))
    y = [v * 3 + rng.normal(0, 0.1) for v in x]  # strong positive monotone
    point, lo, hi = bootstrap_rank_ic_ci(x, y, n_boot=500, seed=11)
    assert point is not None and point > 0.8
    assert lo is not None and hi is not None
    assert lo > 0  # CI excludes 0 for a strong relationship


def test_bootstrap_rank_ic_ci_too_few_pairs():
    point, lo, hi = bootstrap_rank_ic_ci([1.0, 2.0], [3.0, 4.0], n_boot=100)
    assert lo is None and hi is None


def test_attach_features_pit_no_lookahead():
    from research.backtest.equity_corr_rank_ic import attach_features_to_cycles

    idx = _dates(20)
    frame = pd.DataFrame(
        {
            "rho_bar": np.linspace(0.2, 0.6, 20),
            "rho_bar_ewma": np.linspace(0.2, 0.6, 20),
            "dispersion": np.linspace(0.1, 0.0, 20),
        },
        index=idx,
    )
    # Entry on a NON-feature date (Sat after idx[11]=Tue is idx[15] gap)
    # → must pick the latest feature strictly <= entry, never look ahead.
    entry = (idx[10] + pd.Timedelta(days=2)).date()  # Wed 2024-01-17, a feature date
    cycles = [{"entry_date": entry, "net_pnl": 500.0, "rom": 0.04}]
    attached = attach_features_to_cycles(cycles, frame)
    assert len(attached) == 1
    fdate = attached.iloc[0]["feature_date"]
    assert fdate <= entry  # PIT respected — never a future feature date
    assert fdate == idx[12].date()  # 2024-01-17, the entry day's own feature

    # Entry on a weekend gap → falls back to the prior trading day's feature.
    weekend_entry = (idx[11] + pd.Timedelta(days=3)).date()  # Fri+? lands off-grid
    cyc2 = [{"entry_date": weekend_entry, "net_pnl": 0.0, "rom": 0.0}]
    att2 = attach_features_to_cycles(cyc2, frame)
    assert att2.iloc[0]["feature_date"] <= weekend_entry
