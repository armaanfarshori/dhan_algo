"""Tests for the market-session abstraction (core/sessions.py).

Covers BOTH profiles: EQUITY (must reproduce the legacy 09:15–15:30 / poll
09:00–15:35 behaviour byte-identically) and MCX (09:00 open, evening close that
flips with US DST: 23:30 when New York is on DST, 23:55 on US standard time).

All datetimes are constructed tz-aware IST and the file must pass under TZ=UTC
(CI IST-date trap) — none of the assertions depend on the host wall clock.
"""
import dataclasses
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pytest

from core.sessions import (
    EQUITY,
    MCX,
    MarketSession,
    SessionClock,
    get_session,
)

IST = ZoneInfo("Asia/Kolkata")

# Reference dates (all verified):
#   2026-07-01 Wed → New York on DST   → MCX close 23:30
#   2026-01-14 Wed → New York standard → MCX close 23:55
#   2026-06-13 Sat → weekend
DST_ON = date(2026, 7, 1)
DST_OFF = date(2026, 1, 14)
SATURDAY = date(2026, 6, 13)


def _ist(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


# ── EQUITY profile (legacy parity) ──────────────────────────────────────────────

def test_equity_constants_match_legacy():
    assert EQUITY.open_time == dtime(9, 15)
    assert EQUITY.close_time == dtime(15, 30)
    assert EQUITY.poll_open == dtime(9, 0)
    assert EQUITY.poll_close == dtime(15, 35)
    assert EQUITY.squareoff_before_close_min == 15
    # Equity close never flips with DST.
    assert EQUITY.standard_close_time is None
    assert EQUITY.close_time_for(DST_ON) == dtime(15, 30)
    assert EQUITY.close_time_for(DST_OFF) == dtime(15, 30)


def test_equity_is_open():
    c = SessionClock(EQUITY)
    assert c.is_open(_ist(DST_ON, 9, 15)) is True       # open inclusive
    assert c.is_open(_ist(DST_ON, 10, 30)) is True
    assert c.is_open(_ist(DST_ON, 9, 14)) is False      # before open
    assert c.is_open(_ist(DST_ON, 15, 30)) is False     # close exclusive
    assert c.is_open(_ist(DST_ON, 15, 31)) is False


def test_equity_poll_window():
    c = SessionClock(EQUITY)
    assert c.in_poll_window(_ist(DST_ON, 10, 30)) is True
    assert c.in_poll_window(_ist(DST_ON, 9, 0)) is True    # both ends inclusive
    assert c.in_poll_window(_ist(DST_ON, 15, 35)) is True
    assert c.in_poll_window(_ist(DST_ON, 8, 59)) is False
    assert c.in_poll_window(_ist(DST_ON, 15, 36)) is False


def test_equity_squareoff():
    c = SessionClock(EQUITY)
    # 15:30 − 15 min = 15:15, regardless of DST (equity is fixed).
    assert c.squareoff_time(DST_ON) == dtime(15, 15)
    assert c.squareoff_time(DST_OFF) == dtime(15, 15)


def test_equity_weekend_closed():
    c = SessionClock(EQUITY)
    assert c.is_open(_ist(SATURDAY, 10, 30)) is False
    assert c.in_poll_window(_ist(SATURDAY, 10, 30)) is False
    assert c.is_trading_day(SATURDAY) is False
    assert c.is_trading_day(DST_ON) is True


# ── MCX profile (DST-sensitive evening close) ───────────────────────────────────

def test_mcx_open_time():
    assert MCX.open_time == dtime(9, 0)


def test_mcx_close_flips_dst_on():
    # New York on DST → earlier close 23:30.
    assert MCX.close_time_for(DST_ON) == dtime(23, 30)


def test_mcx_close_flips_dst_off():
    # New York on US standard time → later close 23:55.
    assert MCX.close_time_for(DST_OFF) == dtime(23, 55)


def test_mcx_is_open_dst_on():
    c = SessionClock(MCX)
    assert c.is_open(_ist(DST_ON, 9, 0)) is True       # open inclusive
    assert c.is_open(_ist(DST_ON, 16, 0)) is True       # mid-evening (would be shut for equity)
    assert c.is_open(_ist(DST_ON, 23, 29)) is True
    assert c.is_open(_ist(DST_ON, 23, 30)) is False     # close exclusive (DST close)
    assert c.is_open(_ist(DST_ON, 23, 45)) is False
    assert c.is_open(_ist(DST_ON, 8, 59)) is False


def test_mcx_is_open_dst_off():
    c = SessionClock(MCX)
    # On US-standard the session runs 25 min later.
    assert c.is_open(_ist(DST_OFF, 23, 40)) is True     # open under DST-off close
    assert c.is_open(_ist(DST_OFF, 23, 54)) is True
    assert c.is_open(_ist(DST_OFF, 23, 55)) is False    # close exclusive (DST-off close)


def test_mcx_is_open_dst_off_mid_evening():
    # Pin that the DST-off close branch leaves the session open mid-evening.
    # A regression that accidentally used the DST-ON close (23:30) on a DST-off day
    # would make this return False.
    assert SessionClock(MCX).is_open(_ist(DST_OFF, 23, 30)) is True


def test_mcx_squareoff_flips_with_dst():
    c = SessionClock(MCX)
    assert c.squareoff_time(DST_ON) == dtime(23, 15)    # 23:30 − 15
    assert c.squareoff_time(DST_OFF) == dtime(23, 40)   # 23:55 − 15


def test_mcx_poll_window_flips_with_dst():
    c = SessionClock(MCX)
    # Pin the DST-off poll-close constant so a regression widening the window is caught.
    assert MCX.poll_close_dst_off == dtime(23, 59)
    # Poll window extends past close by ~10 min and flips with DST.
    assert c.in_poll_window(_ist(DST_ON, 23, 40)) is True
    assert c.in_poll_window(_ist(DST_ON, 23, 41)) is False
    assert c.in_poll_window(_ist(DST_OFF, 23, 59)) is True
    # One-past-boundary on DST-off side must be False (mirrors DST-on 23:41 check).
    assert c.in_poll_window(_ist(DST_OFF, 0, 0)) is False
    # 8:45 pre-margin both ends inclusive
    assert c.in_poll_window(_ist(DST_ON, 8, 45)) is True
    assert c.in_poll_window(_ist(DST_ON, 8, 44)) is False


def test_mcx_weekend_closed():
    c = SessionClock(MCX)
    assert c.is_open(_ist(SATURDAY, 16, 0)) is False
    assert c.in_poll_window(_ist(SATURDAY, 16, 0)) is False


# ── Clock helpers / general ─────────────────────────────────────────────────────

def test_naive_datetime_treated_as_ist():
    c = SessionClock(EQUITY)
    naive = datetime(2026, 7, 1, 10, 30)  # no tzinfo
    assert c.is_open(naive) is True
    assert c.session_date(naive) == DST_ON


def test_session_date_is_ist():
    c = SessionClock(EQUITY)
    # A UTC instant at 20:00 on 2026-06-30 is 2026-07-01 01:30 IST.
    utc = datetime(2026, 6, 30, 20, 0, tzinfo=ZoneInfo("UTC"))
    assert c.session_date(utc) == date(2026, 7, 1)


def test_get_session_lookup():
    assert get_session("equity") is EQUITY
    assert get_session("MCX") is MCX
    assert get_session("Mcx") is MCX
    with pytest.raises(KeyError):
        get_session("forex")


def test_market_session_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        MCX.open_time = dtime(10, 0)  # type: ignore[misc]


def test_custom_fixed_close_session():
    s = MarketSession(
        name="TEST", open_time=dtime(8, 0), close_time=dtime(17, 0),
        poll_open=dtime(7, 0), poll_close=dtime(18, 0),
        squareoff_before_close_min=30,
    )
    c = SessionClock(s)
    assert c.squareoff_time(DST_ON) == dtime(16, 30)
    assert c.is_open(_ist(DST_ON, 8, 0)) is True
    assert c.is_open(_ist(DST_ON, 17, 0)) is False
