"""Pure ORB strategy — OR build/lock, breakouts, exits, square-off.
The strategy is synchronous and IO-free, so tests construct timestamps directly."""
from datetime import datetime

from strategies.orb import ORB, ORBParams


def ist(hh, mm):
    return datetime(2026, 6, 11, hh, mm)


def test_or_builds_during_window():
    s = ORB("999")
    assert s.on_tick(ist(9, 20), 101, high=102, low=100) is None
    assert s.or_high == 102 and s.or_low == 100
    assert not s.or_locked


def test_long_breakout_after_lock():
    s = ORB("999")
    s.on_tick(ist(9, 20), 101, high=102, low=100)
    d = s.on_tick(ist(9, 31), 103)
    assert s.or_locked
    assert d is not None and d.action == "ENTER" and d.side == "BUY"
    assert d.stop < 100.0 <= 100.0          # stop padded below OR low
    assert d.target > 103


def test_short_breakdown():
    s = ORB("999")
    s.on_tick(ist(9, 20), 101, high=102, low=100)
    d = s.on_tick(ist(9, 31), 99)
    assert d is not None and d.action == "ENTER" and d.side == "SELL"


def test_no_reentry_same_direction():
    s = ORB("999")
    s.on_tick(ist(9, 20), 101, high=102, low=100)
    d1 = s.on_tick(ist(9, 31), 103)
    assert d1 and d1.action == "ENTER"
    # Entry was NOT filled (gate/risk rejected) — direction is consumed
    d2 = s.on_tick(ist(9, 32), 104)
    assert d2 is None


def test_target_exit_long():
    s = ORB("999")
    s.on_tick(ist(9, 20), 101, high=102, low=100)
    d = s.on_tick(ist(9, 31), 103)
    s.notify_fill("BUY", 10, 103)            # runner confirms the fill
    assert s.position == 10
    # target = 103 + 1.5 × 2 = 106
    d = s.on_tick(ist(10, 0), 106.5)
    assert d is not None and d.action == "EXIT" and "Target" in d.reason


def test_stop_exit_long():
    s = ORB("999")
    s.on_tick(ist(9, 20), 101, high=102, low=100)
    s.on_tick(ist(9, 31), 103)
    s.notify_fill("BUY", 10, 103)
    d = s.on_tick(ist(10, 0), 99.5)          # below OR low × (1 − buffer)
    assert d is not None and d.action == "EXIT" and "Stop" in d.reason


def test_eod_squareoff():
    s = ORB("999")
    s.on_tick(ist(9, 20), 101, high=102, low=100)
    s.on_tick(ist(9, 31), 103)
    s.notify_fill("BUY", 10, 103)
    d = s.on_tick(ist(15, 16), 104)
    assert d is not None and d.action == "EXIT" and "EOD" in d.reason


def test_narrow_range_skipped():
    s = ORB("999")
    s.on_tick(ist(9, 20), 1000.1, high=1000.2, low=1000.0)
    d = s.on_tick(ist(9, 31), 1001)
    assert d is None                          # range 0.2 < 0.3% of 1000


def test_session_reset():
    s = ORB("999")
    s.on_tick(ist(9, 20), 101, high=102, low=100)
    s.on_tick(ist(9, 31), 103)
    next_day = datetime(2026, 6, 12, 9, 16)
    s.on_tick(next_day, 200, high=201, low=199)
    assert not s.or_locked
    assert s.or_high == 201 and s.or_low == 199


def test_45min_window_no_crash():
    s = ORB("999", ORBParams(orb_minutes=45))
    assert s.on_tick(ist(9, 30), 100, high=101, low=99) is None
    assert not s.or_locked                    # still inside the window
    s.on_tick(ist(10, 1), 102)
    assert s.or_locked
