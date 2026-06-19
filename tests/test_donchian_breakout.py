"""
Donchian Channel Breakout — unit tests.

All tests drive on_tick() directly with IST timestamps; no DB, no I/O.
Tests 1–11 map to the numbered cases in the spec (§7).
"""
from datetime import date, datetime, timedelta

from strategies.donchian_breakout import DonchianBreakout, DonchianBreakoutParams

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SESSION_DATE = date(2024, 1, 1)
NEXT_SESSION_DATE = date(2024, 1, 2)


def tk(hh: int, mm: int, close: float, high: float, low: float,
       session: date = SESSION_DATE) -> tuple:
    """Build an (now, close, high, low) tuple for on_tick calls."""
    return datetime(session.year, session.month, session.day, hh, mm), close, high, low


def _base_params(**overrides) -> DonchianBreakoutParams:
    """Small-N params suitable for unit tests:
       N=3, no chop guard, no warmup delay, large ATR mult (effectively no cap).
    """
    defaults = dict(
        channel_period=3,
        exclude_current_bar=True,
        warmup_skip_open_min=0,
        min_channel_pct=0.0,
        sl_atr_mult=100.0,    # huge → ATR cap never tighter than channel edge
        sl_buffer_pct=0.001,
        target_r_multiple=2.0,
        squareoff_before_close_min=15,
    )
    defaults.update(overrides)
    return DonchianBreakoutParams(**defaults)


def _feed(strategy: DonchianBreakout, ticks):
    """Feed a list of (now, close, high, low) tuples; return list of decisions."""
    results = []
    for now, close, high, low in ticks:
        results.append(strategy.on_tick(now, close, high=high, low=low))
    return results


def _warm_up(strategy: DonchianBreakout, n: int = None,
             session: date = SESSION_DATE):
    """Feed N bars starting at 09:15, one minute each, all with high=103/low=97/close=100.
       Returns the number of bars fed.
    """
    if n is None:
        n = strategy.p.channel_period
    for i in range(n):
        t = datetime(session.year, session.month, session.day, 9, 15 + i)
        strategy.on_tick(t, 100, high=103, low=97)
    return n


# ---------------------------------------------------------------------------
# Test 1 — Warm-up returns None (channel not ready)
# ---------------------------------------------------------------------------

def test_warmup_returns_none():
    """Feed N-1 bars → both return None (channel needs ≥ N prior bars)."""
    s = DonchianBreakout("TEST", _base_params(channel_period=3))
    d1 = s.on_tick(*tk(9, 15, 100, 101, 99)[:4])
    d2 = s.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    assert d1 is None
    assert d2 is None
    assert not s._channel_ready()


# ---------------------------------------------------------------------------
# Test 2 — Long breakout emits ENTER BUY with correct levels
# ---------------------------------------------------------------------------

def test_long_breakout_enter_buy():
    """
    N=3, ATR not ready (atr_period=14 > 4 bars fed, so falls back to channel-edge stop).
    Bars 09:15–09:17 build channel: high=101/102/103, low=99/98/97.
    Bar 09:18: close=104 > upper=103 → ENTER BUY.
    stop  = lower * (1 - sl_buffer_pct) = 97 * 0.999 ≈ 96.903
    risk  = 104 - 96.903 ≈ 7.097
    target = 104 + 2 * 7.097 ≈ 118.194
    """
    s = DonchianBreakout("TEST", _base_params())
    s.on_tick(*tk(9, 15, 100, 101, 99)[:4])
    s.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    s.on_tick(*tk(9, 17, 100, 103, 97)[:4])
    d = s.on_tick(*tk(9, 18, 104, 104, 100)[:4])

    assert d is not None
    assert d.action == "ENTER"
    assert d.side == "BUY"
    assert d.stop < 104                        # stop below entry
    assert d.target > 104                      # target above entry
    expected_stop = 97 * (1 - 0.001)
    assert abs(d.stop - expected_stop) < 0.01
    expected_risk = 104 - expected_stop
    expected_target = 104 + 2.0 * expected_risk
    assert abs(d.target - expected_target) < 0.01
    assert s._long_tried is True


# ---------------------------------------------------------------------------
# Test 3 — Short breakdown emits ENTER SELL
# ---------------------------------------------------------------------------

def test_short_breakdown_enter_sell():
    """
    Symmetric to test 2.
    Channel: highs 103/102/101 (descending), lows 99/98/97.
    upper=103, lower=97. Feed close=96 < 97 → ENTER SELL.
    stop_raw = upper * (1 + sl_buffer_pct) = 103 * 1.001 ≈ 103.103
    risk = stop - price = 103.103 - 96 ≈ 7.103
    target = 96 - 2 * 7.103 ≈ 81.794
    """
    s = DonchianBreakout("TEST", _base_params())
    s.on_tick(*tk(9, 15, 100, 103, 99)[:4])
    s.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    s.on_tick(*tk(9, 17, 100, 101, 97)[:4])
    d = s.on_tick(*tk(9, 18, 96, 97, 95)[:4])

    assert d is not None
    assert d.action == "ENTER"
    assert d.side == "SELL"
    assert d.stop > 96                         # stop above entry (SHORT)
    assert d.target < 96                       # target below entry
    expected_stop = 103 * (1 + 0.001)
    assert abs(d.stop - expected_stop) < 0.01
    assert s._short_tried is True


# ---------------------------------------------------------------------------
# Test 4 — One-attempt-per-side: second long breakout suppressed
# ---------------------------------------------------------------------------

def test_one_attempt_per_side_long_suppressed():
    """After a long is emitted, notify_fill + notify_flat, then another breakout
    above the channel → None (long_tried blocks it).  SHORT is still allowed.
    """
    s = DonchianBreakout("TEST", _base_params())
    s.on_tick(*tk(9, 15, 100, 101, 99)[:4])
    s.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    s.on_tick(*tk(9, 17, 100, 103, 97)[:4])

    d = s.on_tick(*tk(9, 18, 104, 104, 100)[:4])
    assert d is not None and d.action == "ENTER" and d.side == "BUY"

    s.notify_fill("BUY", 10, 104)
    s.notify_flat()

    # Another bar above the channel — long side consumed
    d2 = s.on_tick(*tk(9, 19, 106, 106, 103)[:4])
    assert d2 is None, "Second long breakout must be suppressed"
    assert s._long_tried is True

    # SHORT side is still open — push a bar below lower channel
    # Update channel with new bar data first then check
    d3 = s.on_tick(*tk(9, 20, 90, 91, 89)[:4])
    assert d3 is not None and d3.action == "ENTER" and d3.side == "SELL"


# ---------------------------------------------------------------------------
# Test 5 — No breakout inside the channel → None
# ---------------------------------------------------------------------------

def test_no_breakout_inside_channel():
    """Price in 97–103 band → no entry signal."""
    s = DonchianBreakout("TEST", _base_params())
    s.on_tick(*tk(9, 15, 100, 101, 99)[:4])
    s.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    s.on_tick(*tk(9, 17, 100, 103, 97)[:4])
    d = s.on_tick(*tk(9, 18, 100, 102, 98)[:4])
    assert d is None


# ---------------------------------------------------------------------------
# Test 6 — exclude_current_bar flag changes the channel window
# ---------------------------------------------------------------------------

def test_exclude_current_bar_flag():
    """
    With exclude_current_bar=True: the current bar's high is excluded from the channel
    used for entry comparison. With False: the current bar is included.

    Setup: prior-3 highs = 101, 102, 103 → upper = 103.
    New bar: high=110, close=102.

    exclude_current_bar=True:
        channel read BEFORE push → upper=103; close=102 < 103 → None.
        After the tick: upper_channel() reflects the pushed bar = 110.

    exclude_current_bar=False:
        push first → upper includes 110; close=102 < 110 → None.
        upper_channel() = 110 (same, because bar is included both ways).

    The key assertion is that the *decision-time* channel differs between the two modes.
    We verify via upper_channel() post-tick for the exclude=True case (channel updated
    to 110 after decision) vs exclude=False (channel was 110 at decision time).
    """
    # exclude_current_bar=True (default)
    s_excl = DonchianBreakout("TEST_E", _base_params(exclude_current_bar=True))
    s_excl.on_tick(*tk(9, 15, 100, 101, 99)[:4])
    s_excl.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    s_excl.on_tick(*tk(9, 17, 100, 103, 97)[:4])
    # At this point, 3 bars pushed; channel ready (i=3)
    # Feed bar with high=110, close=102: channel read first → upper=103; 102 < 103 → None
    d_excl = s_excl.on_tick(*tk(9, 18, 102, 110, 101)[:4])
    assert d_excl is None, "close=102 < excluded upper=103 → no entry"
    # Post-tick: upper_channel now includes the bar high=110
    assert s_excl.upper_channel() == 110

    # exclude_current_bar=False
    s_incl = DonchianBreakout("TEST_I", _base_params(exclude_current_bar=False))
    s_incl.on_tick(*tk(9, 15, 100, 101, 99)[:4])
    s_incl.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    s_incl.on_tick(*tk(9, 17, 100, 103, 97)[:4])
    # Feed same bar: push first → upper=110; close=102 < 110 → None
    d_incl = s_incl.on_tick(*tk(9, 18, 102, 110, 101)[:4])
    assert d_incl is None, "close=102 < included upper=110 → no entry"
    assert s_incl.upper_channel() == 110

    # Confirm the decision-time channels differed: for exclude=True the entry
    # bar is NOT in the channel before the decision; we can verify by checking
    # that upper_channel() AFTER the tick is 110 for both but that at decision
    # time exclude=True saw 103 (prior-3 only). We test this by feeding a close
    # that would exceed exclude=True's channel (103) but not include=False's (110).
    s_excl2 = DonchianBreakout("TEST_E2", _base_params(exclude_current_bar=True))
    s_excl2.on_tick(*tk(9, 15, 100, 101, 99)[:4])
    s_excl2.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    s_excl2.on_tick(*tk(9, 17, 100, 103, 97)[:4])
    # Feed bar with high=110, close=104 (breaks prior upper=103, but not 110)
    d_excl2 = s_excl2.on_tick(*tk(9, 18, 104, 110, 100)[:4])
    assert d_excl2 is not None and d_excl2.action == "ENTER" and d_excl2.side == "BUY", \
        "exclude=True: close=104 > prior upper=103 → ENTER BUY"

    s_incl2 = DonchianBreakout("TEST_I2", _base_params(exclude_current_bar=False))
    s_incl2.on_tick(*tk(9, 15, 100, 101, 99)[:4])
    s_incl2.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    s_incl2.on_tick(*tk(9, 17, 100, 103, 97)[:4])
    # Same bar: push first → upper=110; close=104 < 110 → None
    d_incl2 = s_incl2.on_tick(*tk(9, 18, 104, 110, 100)[:4])
    assert d_incl2 is None, \
        "exclude=False: close=104 < included upper=110 → None"


# ---------------------------------------------------------------------------
# Test 7 — Opposite-channel signal exit on open LONG
# ---------------------------------------------------------------------------

def test_opposite_channel_exit_long():
    """Open a LONG; feed close below lower channel → EXIT (opposite channel break)."""
    s = DonchianBreakout("TEST", _base_params())
    s.on_tick(*tk(9, 15, 100, 101, 99)[:4])
    s.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    s.on_tick(*tk(9, 17, 100, 103, 97)[:4])
    d = s.on_tick(*tk(9, 18, 104, 104, 100)[:4])
    assert d is not None and d.side == "BUY"
    s.notify_fill("BUY", 10, 104)

    # Push a bar where close drops below lower channel
    # After the breakout bar, lower_channel reflects the last N bars.
    # Feed a tick whose close is clearly below 97 (the channel floor).
    # First push one more bar to keep channel warm (not strictly needed but safe).
    d_exit = s.on_tick(*tk(9, 19, 95, 96, 94)[:4])
    assert d_exit is not None
    assert d_exit.action == "EXIT"


# ---------------------------------------------------------------------------
# Test 8 — Stop-loss EXIT on close for open LONG
# ---------------------------------------------------------------------------

def test_stop_loss_exit_long():
    """Open LONG at 104 with stop ≈ 96.9. Feed close=96.5 → Stop-loss EXIT."""
    s = DonchianBreakout("TEST", _base_params())
    s.on_tick(*tk(9, 15, 100, 101, 99)[:4])
    s.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    s.on_tick(*tk(9, 17, 100, 103, 97)[:4])
    d = s.on_tick(*tk(9, 18, 104, 104, 100)[:4])
    assert d is not None and d.side == "BUY"
    entry_stop = d.stop
    s.notify_fill("BUY", 10, 104)

    # Feed a close below the stored stop
    close_below_stop = entry_stop - 0.5
    d_exit = s.on_tick(*tk(9, 19, close_below_stop, close_below_stop + 1, close_below_stop - 1)[:4])
    assert d_exit is not None
    assert d_exit.action == "EXIT"
    assert d_exit.reason.startswith("Stop-loss")


# ---------------------------------------------------------------------------
# Test 9 — Target EXIT on close for open LONG
# ---------------------------------------------------------------------------

def test_target_exit_long():
    """Open LONG at 104, target ≈ 118.19. Feed close=119 → Target hit EXIT."""
    s = DonchianBreakout("TEST", _base_params())
    s.on_tick(*tk(9, 15, 100, 101, 99)[:4])
    s.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    s.on_tick(*tk(9, 17, 100, 103, 97)[:4])
    d = s.on_tick(*tk(9, 18, 104, 104, 100)[:4])
    assert d is not None and d.side == "BUY"
    entry_target = d.target
    s.notify_fill("BUY", 10, 104)

    close_above_target = entry_target + 1.0
    d_exit = s.on_tick(*tk(9, 19, close_above_target, close_above_target + 1, close_above_target - 1)[:4])
    assert d_exit is not None
    assert d_exit.action == "EXIT"
    assert d_exit.reason.startswith("Target hit")


# ---------------------------------------------------------------------------
# Test 10 — EOD square-off is unconditional
# ---------------------------------------------------------------------------

def test_eod_squareoff_unconditional():
    """
    Open position set via notify_fill on a fresh strategy (no channel data).
    A tick at 15:16 (past the 15:15 squareoff cutoff) must return EXIT even
    without a ready channel.
    With position == 0, the same tick returns None.
    """
    # With position open — even on a fresh session (no bars pushed)
    s = DonchianBreakout("TEST", _base_params())
    s.notify_fill("BUY", 10, 100)    # simulate a DB-reconciled position
    assert s.position == 10

    now = datetime(SESSION_DATE.year, SESSION_DATE.month, SESSION_DATE.day, 15, 16)
    d = s.on_tick(now, 101, high=102, low=100)
    assert d is not None
    assert d.action == "EXIT"
    assert d.reason == "EOD square-off"

    # With no position — same tick returns None
    s2 = DonchianBreakout("TEST2", _base_params())
    d2 = s2.on_tick(now, 101, high=102, low=100)
    assert d2 is None


# ---------------------------------------------------------------------------
# Test 11 — Session reset clears tried flags + channel
# ---------------------------------------------------------------------------

def test_session_reset_clears_state():
    """
    Run test-2 scenario on 2024-01-01 → _long_tried = True, channel has data.
    Feed a tick dated 2024-01-02 → session resets; new-day warm-up returns None;
    after re-warming, a long breakout is allowed (proves no cross-session leakage).
    """
    s = DonchianBreakout("TEST", _base_params())

    # Day 1: build channel and fire long breakout
    s.on_tick(*tk(9, 15, 100, 101, 99)[:4])
    s.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    s.on_tick(*tk(9, 17, 100, 103, 97)[:4])
    d = s.on_tick(*tk(9, 18, 104, 104, 100)[:4])
    assert d is not None and d.action == "ENTER"
    assert s._long_tried is True
    assert s._session_date == SESSION_DATE

    # Day 2: first tick resets the session
    d_next = s.on_tick(*tk(9, 15, 100, 101, 99, session=NEXT_SESSION_DATE)[:4])
    assert d_next is None                          # warm-up on new day
    assert s._session_date == NEXT_SESSION_DATE
    assert s._long_tried is False                  # cleared by reset
    assert s._i == 1                               # only the one bar pushed today
    assert not s._channel_ready()                  # need N=3 bars

    # Re-warm day 2 and verify a fresh long breakout is permitted
    s.on_tick(*tk(9, 16, 100, 102, 98, session=NEXT_SESSION_DATE)[:4])
    s.on_tick(*tk(9, 17, 100, 103, 97, session=NEXT_SESSION_DATE)[:4])
    d_fresh = s.on_tick(*tk(9, 18, 104, 104, 100, session=NEXT_SESSION_DATE)[:4])
    assert d_fresh is not None and d_fresh.action == "ENTER" and d_fresh.side == "BUY"


# ---------------------------------------------------------------------------
# Test 12 — Future-skew guard
# ---------------------------------------------------------------------------

def test_future_skew_guard():
    """A tick stamped > 2 min ahead of wall clock must return None
    and must NOT advance the session date or update channel state."""
    s = DonchianBreakout("TEST", _base_params())

    # First, give the strategy a valid tick close to the real wall clock
    from zoneinfo import ZoneInfo
    IST_zone = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(IST_zone).replace(tzinfo=None)
    s.on_tick(now_ist, 100, high=101, low=99)
    saved_date = s._session_date
    saved_i = s._i

    # Now send a tick stamped 1 hour in the future
    future_tick = now_ist + timedelta(hours=1)
    result = s.on_tick(future_tick, 500, high=600, low=400)
    assert result is None
    # Session date must NOT have changed to a future date
    assert s._session_date == saved_date
    # Bar counter must not have advanced
    assert s._i == saved_i


# ---------------------------------------------------------------------------
# Additional: price <= 0 guard
# ---------------------------------------------------------------------------

def test_zero_price_returns_none():
    s = DonchianBreakout("TEST", _base_params())
    assert s.on_tick(datetime(2024, 1, 1, 9, 30), 0.0) is None
    assert s.on_tick(datetime(2024, 1, 1, 9, 30), -5.0) is None


# ---------------------------------------------------------------------------
# Additional: chop guard
# ---------------------------------------------------------------------------

def test_chop_guard_suppresses_entry():
    """Channel width < min_channel_pct * price → no entry even on breakout."""
    params = _base_params(min_channel_pct=0.05)   # 5% — very wide guard
    s = DonchianBreakout("TEST", params)
    # Build channel with very tight range (width ≈ 0.2 on price 1000)
    s.on_tick(datetime(2024, 1, 1, 9, 15), 1000, high=1000.1, low=999.9)
    s.on_tick(datetime(2024, 1, 1, 9, 16), 1000, high=1000.1, low=999.9)
    s.on_tick(datetime(2024, 1, 1, 9, 17), 1000, high=1000.1, low=999.9)
    d = s.on_tick(datetime(2024, 1, 1, 9, 18), 1001, high=1001, low=1000)
    assert d is None, "Chop guard must block entry on narrow channel"


# ---------------------------------------------------------------------------
# Additional: notify_flat clears _stop/_target
# ---------------------------------------------------------------------------

def test_notify_flat_clears_stop_target():
    s = DonchianBreakout("TEST", _base_params())
    s.on_tick(*tk(9, 15, 100, 101, 99)[:4])
    s.on_tick(*tk(9, 16, 100, 102, 98)[:4])
    s.on_tick(*tk(9, 17, 100, 103, 97)[:4])
    d = s.on_tick(*tk(9, 18, 104, 104, 100)[:4])
    assert d is not None
    s.notify_fill("BUY", 10, 104)
    assert s._stop != 0.0
    assert s._target != 0.0
    s.notify_flat()
    assert s._stop == 0.0
    assert s._target == 0.0
    assert s.position == 0
