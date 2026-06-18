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
def test_realized_vol_window_too_small_raises():
    """window < 2 must raise ValueError (not return all-None)."""
    with pytest.raises(ValueError):
        d.realized_vol_series([1, 2, 3], window=1)


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
    # result[i] covers rets[i-window .. i-1]; at i=25, window=20 → rets[5:25]
    i = 25
    expected = statistics.stdev(rets[i - window : i]) * math.sqrt(252)
    assert vols[i] == pytest.approx(expected)


def test_realized_vol_constant_series_is_zero():
    closes = [500.0] * 30
    vols = d.realized_vol_series(closes, window=20)
    assert vols[25] == pytest.approx(0.0)


def test_realized_vol_too_few_closes():
    # n < window+1 → all None, but NOT a ValueError (window=20 >= 2)
    assert d.realized_vol_series([1, 2, 3], window=20) == [None, None, None]


def test_realized_vol_nan_window():
    """A zero close produces two non-finite log returns (rets[9] and rets[10]).
    Any window that overlaps those returns must yield None; once the window has
    fully passed bar 10, values must become non-None again.

    closes layout (36 elements, indices 0-35):
      [100]*10 + [0] + [100]*25
    rets layout (35 elements, indices 0-34):
      rets[9]  = log(0/100)   → -inf / nan (bad)
      rets[10] = log(100/0)   → +inf / nan (bad)
      all others are finite

    With window=20, result[i] covers rets[i-20 .. i-1].
    rets[10] last falls inside the window at i=30 (i-1=10 → covered).
    At i=31, the window covers rets[11..30] — all finite → non-None.
    """
    closes = [100.0] * 10 + [0.0] + [100.0] * 25  # 36 elements
    window = 20
    vols = d.realized_vol_series(closes, window=window)

    assert len(vols) == 36

    # Bars in [window, 30] whose trailing return-window touches rets[9] or rets[10]
    # must be None.
    for i in range(window, 31):
        assert vols[i] is None, f"expected None at index {i}, got {vols[i]}"

    # Once the bad returns have exited the window, values must be non-None.
    for i in range(31, 36):
        assert vols[i] is not None, f"expected non-None at index {i}, got {vols[i]}"


# ── implied move ───────────────────────────────────────────────────────────────────
def test_implied_move_formula():
    # spot 23400, IV 12% (0.12), 7 dte
    im = d.implied_move(23400, 0.12, 7)
    assert im == pytest.approx(23400 * 0.12 * math.sqrt(7 / 365))


def test_implied_move_pct_consistent_with_points():
    spot, iv, dte = 23400, 0.12, 7
    assert d.implied_move(spot, iv, dte) == pytest.approx(spot * d.implied_move_pct(iv, dte))


def test_implied_move_pct_zero_dte():
    """dte=0 → implied_move_pct must be exactly 0.0 (expiry day, no move)."""
    assert d.implied_move_pct(0.12, 0) == 0.0


def test_implied_move_edge_cases():
    assert d.implied_move(23400, 0.12, 0) == 0.0       # expiry day → no move
    assert d.implied_move(None, 0.12, 7) is None
    assert d.implied_move(23400, None, 7) is None
    assert d.implied_move(23400, 0.12, None) is None
    assert d.implied_move(-1, 0.12, 7) is None
    assert d.implied_move(23400, 0, 7) is None
    assert d.implied_move(23400, 0.12, -3) is None
    assert d.implied_move_pct(None, 7) is None
