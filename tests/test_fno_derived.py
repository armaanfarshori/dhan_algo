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


# ── compute_index_realized_vol (DB wrapper, monkeypatched) ────────────────────────
import datetime
from unittest.mock import MagicMock, patch


def _make_fake_session(rows):
    """Return a context-manager mock whose .execute().all() returns ``rows``."""
    result_mock = MagicMock()
    result_mock.all.return_value = rows
    session_mock = MagicMock()
    session_mock.execute.return_value = result_mock
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session_mock)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_compute_index_realized_vol_payload():
    """Verify that compute_index_realized_vol assembles the correct bulk-update
    payload: (security_id, timeframe, time, vol) tuples with None vols dropped,
    and the returned count equals the number of non-None vols."""
    security_id = "13"
    timeframe = "1d"
    window = 20

    # Build 30 (time, close) rows — enough for 10 non-None vol values.
    base = datetime.datetime(2024, 1, 2, tzinfo=datetime.timezone.utc)
    closes = [100.0 * (1 + 0.005 * math.sin(i)) for i in range(30)]
    fake_rows = [
        (base + datetime.timedelta(days=i), closes[i]) for i in range(30)
    ]

    captured: list = []

    def fake_bulk_update(sql, rows):
        captured.extend(rows)
        return len(rows)

    fake_cm = _make_fake_session(fake_rows)

    with (
        patch("db.get_session", return_value=fake_cm),
        patch("core.fno_derived._bulk_update", side_effect=fake_bulk_update),
    ):
        count = d.compute_index_realized_vol(security_id, timeframe, window)

    # Compute expected non-None vols independently.
    expected_vols = d.realized_vol_series(closes, window=window)
    expected_payload = [
        (security_id, timeframe, fake_rows[i][0], v)
        for i, v in enumerate(expected_vols)
        if v is not None
    ]

    assert count == len(expected_payload), (
        f"count mismatch: got {count}, expected {len(expected_payload)}"
    )
    assert len(captured) == len(expected_payload)
    for got, exp in zip(captured, expected_payload):
        assert got[0] == exp[0]   # security_id
        assert got[1] == exp[1]   # timeframe
        assert got[2] == exp[2]   # time
        assert got[3] == pytest.approx(exp[3])  # vol (float)


def test_compute_index_realized_vol_empty_returns_zero():
    """Empty DB result → return 0 without calling _bulk_update."""
    fake_cm = _make_fake_session([])
    with (
        patch("db.get_session", return_value=fake_cm),
        patch("core.fno_derived._bulk_update") as mock_bulk,
    ):
        result = d.compute_index_realized_vol("13")
    assert result == 0
    mock_bulk.assert_not_called()


def test_compute_index_realized_vol_sql_targets_index_bars():
    """The bulk-update SQL passed by compute_index_realized_vol must reference
    index_bars — not futures_bars."""
    security_id = "13"
    timeframe = "1d"
    window = 20

    base = datetime.datetime(2024, 1, 2, tzinfo=datetime.timezone.utc)
    closes = [100.0 * (1 + 0.005 * math.sin(i)) for i in range(30)]
    fake_rows = [
        (base + datetime.timedelta(days=i), closes[i]) for i in range(30)
    ]

    captured_sql: list[str] = []

    def fake_bulk_update(sql, rows):
        captured_sql.append(sql)
        return len(rows)

    fake_cm = _make_fake_session(fake_rows)

    with (
        patch("db.get_session", return_value=fake_cm),
        patch("core.fno_derived._bulk_update", side_effect=fake_bulk_update),
    ):
        d.compute_index_realized_vol(security_id, timeframe, window)

    assert len(captured_sql) == 1, "expected exactly one _bulk_update call"
    sql = captured_sql[0]
    assert "index_bars" in sql, f"SQL must reference index_bars; got: {sql!r}"
    assert "futures_bars" not in sql, f"SQL must NOT reference futures_bars; got: {sql!r}"
    # Regression guard (live run, 2026-06-19): a VALUES alias may carry only
    # column NAMES, not types — `AS d(s text, ...)` is a Postgres syntax error.
    # Type safety comes from ::casts on the projected columns, not the alias.
    alias = sql.split("AS d(")[1].split(")")[0]
    for kw in ("text", "timestamptz", "double precision", "date"):
        assert kw not in alias, f"VALUES alias must be untyped (no '{kw}'); got: {alias!r}"
    assert "::double precision" in sql and "::timestamptz" in sql, (
        f"projected columns must be explicitly cast; got: {sql!r}"
    )
