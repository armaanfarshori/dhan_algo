"""Tests for core/fno_derived.py — realized vol + implied move (pure metrics).

Numeric checks cross-validated against an independent statistics computation.
No DB: only the pure functions are exercised.
"""
import math
import statistics

import pytest

from core import fno_derived as d


# ── log returns ──────────────────────────────────────────────────────────────────
def test_daily_log_returns_basic():
    closes = [100, 110, 99]
    rets = d.daily_log_returns(closes)
    assert len(rets) == 2
    assert rets[0] == pytest.approx(math.log(110 / 100))
    assert rets[1] == pytest.approx(math.log(99 / 110))


def test_daily_log_returns_too_short():
    assert d.daily_log_returns([100]) == []
    assert d.daily_log_returns([]) == []


def test_daily_log_returns_bad_values_are_nan():
    rets = d.daily_log_returns([100, 0, 110])
    assert math.isnan(rets[0]) and math.isnan(rets[1])


# ── realized vol ──────────────────────────────────────────────────────────────────
def test_realized_vol_alignment_and_none_prefix():
    closes = list(range(1, 31))  # 30 closes
    vols = d.realized_vol_series(closes, window=20)
    assert len(vols) == len(closes)
    # first valid index == window (needs window+1 closes)
    assert all(v is None for v in vols[:20])
    assert vols[20] is not None


def test_realized_vol_matches_independent_calc():
    # Deterministic pseudo-walk
    closes = [100.0]
    for i in range(1, 40):
        closes.append(closes[-1] * (1 + 0.01 * math.sin(i)))
    window = 20
    vols = d.realized_vol_series(closes, window=window)
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    i = 25
    expected = statistics.stdev(rets[i - window:i]) * math.sqrt(252)
    assert vols[i] == pytest.approx(expected)


def test_realized_vol_constant_series_is_zero():
    closes = [500.0] * 30
    vols = d.realized_vol_series(closes, window=20)
    assert vols[25] == pytest.approx(0.0)


def test_realized_vol_too_few_closes():
    assert d.realized_vol_series([1, 2, 3], window=20) == [None, None, None]


# ── implied move ───────────────────────────────────────────────────────────────────
def test_implied_move_formula():
    # spot 23400, IV 12% (0.12), 7 dte
    im = d.implied_move(23400, 0.12, 7)
    assert im == pytest.approx(23400 * 0.12 * math.sqrt(7 / 365))


def test_implied_move_pct_consistent_with_points():
    spot, iv, dte = 23400, 0.12, 7
    assert d.implied_move(spot, iv, dte) == pytest.approx(spot * d.implied_move_pct(iv, dte))


def test_implied_move_edge_cases():
    assert d.implied_move(23400, 0.12, 0) == 0.0       # expiry day → no move
    assert d.implied_move(None, 0.12, 7) is None
    assert d.implied_move(23400, None, 7) is None
    assert d.implied_move(23400, 0.12, None) is None
    assert d.implied_move(-1, 0.12, 7) is None
    assert d.implied_move(23400, 0, 7) is None
    assert d.implied_move(23400, 0.12, -3) is None
    assert d.implied_move_pct(None, 7) is None
