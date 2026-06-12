"""5-min aggregation + scorer versioning — the paper-alignment change.

Kronos's NSE pre-training data is 5-min and coarser (no 1-min), so the
engine aggregates raw 1-min bars before scoring. Pure pandas — no model.
"""
import pandas as pd

from core.kronos_signal import _aggregate_bars, scorer_version


def _bars(start, minutes, base=100.0):
    idx = pd.date_range(start, periods=minutes, freq="1min", tz="UTC")
    return pd.DataFrame({
        "open":  [base + i for i in range(minutes)],
        "high":  [base + i + 0.5 for i in range(minutes)],
        "low":   [base + i - 0.5 for i in range(minutes)],
        "close": [base + i + 0.25 for i in range(minutes)],
        "volume": [10] * minutes,
    }, index=idx)


def test_aggregates_ohlcv_correctly():
    df = _bars("2026-06-12 04:00", 10)        # two complete 5-min buckets
    agg = _aggregate_bars(df, "5min")
    assert len(agg) == 2
    first = agg.iloc[0]
    assert first["open"] == 100.0             # first bar's open
    assert first["high"] == 104.5             # max of bars 0-4
    assert first["low"] == 99.5
    assert first["close"] == 104.25           # last bar's close
    assert first["volume"] == 50              # summed


def test_drops_incomplete_trailing_bucket():
    df = _bars("2026-06-12 04:00", 12)        # 2 complete + 2 min of a third
    agg = _aggregate_bars(df, "5min")
    assert len(agg) == 2                      # in-progress bucket dropped


def test_overnight_gap_produces_no_empty_buckets():
    d1 = _bars("2026-06-11 09:55", 5)
    d2 = _bars("2026-06-12 03:45", 5, base=200.0)
    agg = _aggregate_bars(pd.concat([d1, d2]), "5min")
    assert len(agg) == 2                      # resample would emit ~215 empties
    assert agg.iloc[1]["open"] == 200.0


def test_1min_passthrough():
    df = _bars("2026-06-12 04:00", 7)
    assert _aggregate_bars(df, "1min") is df


def test_scorer_version_encodes_config():
    v = scorer_version()
    assert v.startswith("v2-")
    assert "5min" in v and "T0.6" in v and "L480" in v and "P6" in v
