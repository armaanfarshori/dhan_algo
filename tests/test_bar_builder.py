"""BarBuilder — minute roll, OHLC aggregation, cumulative-volume deltas."""
from datetime import datetime
from zoneinfo import ZoneInfo

from engine.bar_builder import BarBuilder

IST = ZoneInfo("Asia/Kolkata")


def ts(hh, mm, ss):
    return datetime(2026, 6, 11, hh, mm, ss, tzinfo=IST)


def test_aggregates_within_minute():
    bb = BarBuilder()
    bb.on_tick("999", 100.0, cum_volume=1000, ts=ts(10, 0, 1))
    bb.on_tick("999", 102.0, cum_volume=1500, ts=ts(10, 0, 30))
    bb.on_tick("999", 99.0,  cum_volume=1800, ts=ts(10, 0, 59))
    bar = bb._current["999"]
    assert bar.open == 100 and bar.high == 102 and bar.low == 99 and bar.close == 99
    assert bar.volume == 800            # deltas: 0 (first) + 500 + 300
    assert bb._pending == []            # minute not closed yet


def test_minute_roll_closes_bar():
    bb = BarBuilder()
    bb.on_tick("999", 100.0, cum_volume=1000, ts=ts(10, 0, 50))
    bb.on_tick("999", 101.0, cum_volume=1200, ts=ts(10, 1, 5))
    assert len(bb._pending) == 1
    closed = bb._pending[0]
    assert closed["close"] == 100 and closed["time"].minute == 0
    assert bb._current["999"].open == 101


def test_volume_reset_clamped():
    bb = BarBuilder()
    bb.on_tick("999", 100.0, cum_volume=5000, ts=ts(10, 0, 1))
    bb.on_tick("999", 100.5, cum_volume=100, ts=ts(10, 0, 30))   # feed reset
    assert bb._current["999"].volume == 0   # negative delta clamped


def test_ignores_zero_price():
    bb = BarBuilder()
    bb.on_tick("999", 0.0, ts=ts(10, 0, 1))
    assert "999" not in bb._current


# ── DATA-04: get_current() for single-source-of-truth intrabar OHLC ──────────

def test_get_current_returns_intrabar_ohlc():
    """get_current() must expose the in-progress bar to LiveFeed.get_ohlc_tick()."""
    bb = BarBuilder()
    bb.on_tick("999", 100.0, cum_volume=1000, ts=ts(10, 0, 1))
    bb.on_tick("999", 103.0, cum_volume=1500, ts=ts(10, 0, 30))
    bb.on_tick("999",  97.0, cum_volume=1800, ts=ts(10, 0, 59))
    cur = bb.get_current("999")
    assert cur is not None
    assert cur["open"]  == 100.0
    assert cur["high"]  == 103.0
    assert cur["low"]   == 97.0
    assert cur["close"] == 97.0
    assert cur["volume"] == 800   # deltas: 0 + 500 + 300


def test_get_current_unknown_sid_returns_none():
    """get_current() on an unseen SID must return None, not raise."""
    bb = BarBuilder()
    assert bb.get_current("does_not_exist") is None


def test_get_current_updates_after_minute_roll():
    """After a minute boundary, get_current() reflects only the new bar."""
    bb = BarBuilder()
    bb.on_tick("999", 100.0, cum_volume=1000, ts=ts(10, 0, 50))
    bb.on_tick("999", 101.0, cum_volume=1200, ts=ts(10, 1,  5))
    cur = bb.get_current("999")
    # New minute bar opened at 101; old bar was closed into _pending
    assert cur["open"] == 101.0
    assert cur["close"] == 101.0
