"""Tests for scripts/cas_surprise_capture.py — the N2 CAS-surprise capture.

Pure-function coverage of the reference-window computation (no network, no DB):
the 15:00–15:15 IST VWAP window bounds, auction-print detection at/after 15:30,
and cross-date exclusion. Timestamps are built as real IST epochs so the
epoch→IST conversion inside _ref_window is exercised, not bypassed.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from scripts.cas_surprise_capture import _ref_window

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 8, 14)


def _epoch(h: int, m: int, d: date = DAY) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=IST).timestamp())


def _bars(rows):
    """rows: [(epoch, close, volume)] → charts/intraday-shaped dict."""
    return {
        "data": {
            "timestamp": [r[0] for r in rows],
            "close": [r[1] for r in rows],
            "volume": [r[2] for r in rows],
        }
    }


def test_ref_window_vwap_is_volume_weighted():
    bars = _bars([
        (_epoch(15, 0), 100.0, 100),
        (_epoch(15, 14), 110.0, 300),
    ])
    vwap, vol, auction = _ref_window(bars, DAY)
    assert vwap == (100.0 * 100 + 110.0 * 300) / 400
    assert vol == 400
    assert auction is None


def test_ref_window_bounds_start_inclusive_end_exclusive():
    bars = _bars([
        (_epoch(14, 59), 999.0, 1000),   # before window — excluded
        (_epoch(15, 0), 100.0, 100),     # start inclusive
        (_epoch(15, 15), 999.0, 1000),   # end exclusive (auction order-entry)
    ])
    vwap, vol, _ = _ref_window(bars, DAY)
    assert vwap == 100.0
    assert vol == 100


def test_auction_prints_counted_separately():
    bars = _bars([
        (_epoch(15, 10), 100.0, 100),
        (_epoch(15, 30), 101.0, 5000),   # auction cross
        (_epoch(15, 33), 101.0, 2000),
    ])
    vwap, vol, auction = _ref_window(bars, DAY)
    assert vwap == 100.0                 # auction never contaminates the VWAP
    assert vol == 100
    assert auction == 7000


def test_other_days_bars_excluded():
    prev = date(2026, 8, 13)
    bars = _bars([
        (_epoch(15, 5, prev), 999.0, 9999),
        (_epoch(15, 5), 100.0, 100),
    ])
    vwap, vol, _ = _ref_window(bars, DAY)
    assert vwap == 100.0
    assert vol == 100


def test_empty_window_returns_none():
    vwap, vol, auction = _ref_window(_bars([(_epoch(10, 0), 100.0, 50)]), DAY)
    assert vwap is None and vol == 0 and auction is None
    # zero-volume window must not divide by zero
    vwap, _, _ = _ref_window(_bars([(_epoch(15, 5), 100.0, 0)]), DAY)
    assert vwap is None


def test_unnested_payload_accepted():
    """charts/intraday sometimes returns the arrays at top level (no "data")."""
    flat = {"timestamp": [_epoch(15, 5)], "close": [50.0], "volume": [10]}
    vwap, vol, _ = _ref_window(flat, DAY)
    assert vwap == 50.0 and vol == 10
