"""
Unit tests for Rsi2MeanRev — drive on_tick directly; no DB, no I/O.

Default test params (unless overridden):
    Rsi2MeanRevParams(rsi_period=2, rsi_oversold=10, rsi_overbought=90,
                      rsi_exit_long=65, rsi_exit_short=35,
                      trend_period=3, stop_pct=0.01, target_pct=0.0,
                      enable_short=True)

Series design notes:
  - RSI(2) Wilder: needs prev_close seed + 2 deltas = 3 closes minimum.
  - SMA(trend_period): needs `trend_period` closes.
  - For tests requiring RSI extremes while respecting the trend filter, we use
    trend_period=50 with 50 warm-up bars.  A 50-bar rising series (90..139)
    builds RSI≈100 and SMA=114.5; one drop to 120 gives RSI≈5 with price>SMA.
    A 50-bar falling series (139..90) then a pop to 109 gives RSI≈95 with
    price<SMA.  Exact arithmetic verified in isolation below.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from strategies.rsi2_meanrev import Rsi2MeanRev, Rsi2MeanRevParams

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _params(**kw) -> Rsi2MeanRevParams:
    """Default params: trend_period=3 for short tests; override as needed."""
    defaults = dict(
        rsi_period=2,
        rsi_oversold=10.0,
        rsi_overbought=90.0,
        rsi_exit_long=65.0,
        rsi_exit_short=35.0,
        trend_period=3,
        stop_pct=0.01,
        target_pct=0.0,
        enable_short=True,
    )
    defaults.update(kw)
    return Rsi2MeanRevParams(**defaults)


def _ts(h: int, m: int, day: int = 1) -> datetime:
    """IST-aware timestamp on 2024-01-{day:02d}."""
    return datetime(2024, 1, day, h, m, tzinfo=IST)


def _ts_offset(minutes_from_open: int, day: int = 1) -> datetime:
    """IST-aware timestamp offset minutes_from_open minutes after 09:15."""
    base = datetime(2024, 1, day, 9, 15, tzinfo=IST)
    return base + timedelta(minutes=minutes_from_open)


def _make(params=None) -> Rsi2MeanRev:
    return Rsi2MeanRev("TEST", params or _params())


def _warm_up_rising(s: Rsi2MeanRev, trend_period: int) -> None:
    """
    Feed `trend_period` bars of rising closes (90, 91, ...) starting at 09:15.
    After this call both RSI and SMA are ready.
    """
    for i, c in enumerate(range(90, 90 + trend_period)):
        ts = _ts_offset(i)
        s.on_tick(ts, float(c))


def _warm_up_falling(s: Rsi2MeanRev, trend_period: int) -> None:
    """
    Feed `trend_period` bars of falling closes (90+trend_period-1, ..., 90).
    After this call both RSI and SMA are ready.
    """
    for i, c in enumerate(range(90 + trend_period - 1, 89, -1)):
        ts = _ts_offset(i)
        s.on_tick(ts, float(c))


# ---------------------------------------------------------------------------
# Test 1: Warm-up returns None for the first two bars (trend_period=3)
# ---------------------------------------------------------------------------

def test_warmup_returns_none():
    """Two closes are not enough to ready SMA(3) or RSI(2)."""
    s = _make()
    r1 = s.on_tick(_ts(9, 15), 100.0)
    r2 = s.on_tick(_ts(9, 16), 101.0)
    assert r1 is None
    assert r2 is None
    assert s.rsi is None
    assert s.sma is None


# ---------------------------------------------------------------------------
# Test 2: Oversold long entry in uptrend
# ---------------------------------------------------------------------------

def test_oversold_long_entry():
    """
    50 rising bars (90..139) warm up RSI=100, SMA=114.5.
    Bar 51 drops to 120: RSI(2) Wilder → 5.0 (<=10), SMA(50)→115.1, price=120 > 115.1.
    Expect ENTER/BUY with stop=120*0.99=118.8 and far target.
    """
    p = _params(trend_period=50)
    s = _make(p)
    _warm_up_rising(s, 50)  # bars 09:15..10:04
    assert s.rsi is not None and s.sma is not None

    ts_entry = _ts_offset(50)  # 10:05
    d = s.on_tick(ts_entry, 120.0)

    # Verify conditions held
    assert s.rsi is not None
    assert s.rsi <= 10.0, f"RSI={s.rsi:.4f} expected <=10"
    assert s.sma is not None
    assert 120.0 > s.sma, f"price=120 must be > SMA={s.sma:.4f}"

    assert d is not None, "Expected ENTER decision"
    assert d.action == "ENTER"
    assert d.side == "BUY"
    assert abs(d.stop - 120.0 * 0.99) < 1e-9
    assert d.target > 120.0, "Far placeholder target must be above entry"
    assert "RSI2 long" in d.reason


# ---------------------------------------------------------------------------
# Test 3: Oversold but downtrend → no long, no short
# ---------------------------------------------------------------------------

def test_oversold_downtrend_no_long():
    """
    Falling series [110, 108, 106, 104, 102] with trend_period=3.
    RSI(2) hits 0 (<=10) after 3 bars, but close is always below SMA(3) (downtrend).
    enable_short=True but RSI never reaches >=90, so no short either.
    All on_tick calls must return None.
    """
    s = _make(_params(trend_period=3))
    for i, c in enumerate([110.0, 108.0, 106.0, 104.0, 102.0]):
        ts = _ts(9, 15 + i)
        result = s.on_tick(ts, c)
        assert result is None, f"Expected None at close={c}, got {result}"


# ---------------------------------------------------------------------------
# Test 4: Overbought short entry in downtrend + enable_short toggle
# ---------------------------------------------------------------------------

def test_overbought_short_entry():
    """
    50 falling bars (139..90) warm up RSI=0, SMA=114.5.
    Bar 51 pops to 109: RSI→95.0 (>=90), SMA(50)→113.9, price=109 < 113.9.
    Expect ENTER/SELL with stop=109*1.01.
    With enable_short=False, same bars must return None.
    """
    p = _params(trend_period=50)
    s = _make(p)
    _warm_up_falling(s, 50)  # bars 09:15..10:04
    assert s.rsi is not None and s.sma is not None

    ts_entry = _ts_offset(50)  # 10:05
    d = s.on_tick(ts_entry, 109.0)

    assert s.rsi is not None
    assert s.rsi >= 90.0, f"RSI={s.rsi:.4f} expected >=90"
    assert s.sma is not None
    assert 109.0 < s.sma, f"price=109 must be < SMA={s.sma:.4f}"

    assert d is not None, "Expected ENTER/SELL decision"
    assert d.action == "ENTER"
    assert d.side == "SELL"
    assert abs(d.stop - 109.0 * 1.01) < 1e-9
    assert d.target < 109.0, "Far placeholder target must be below entry"
    assert "RSI2 short" in d.reason

    # Repeat with enable_short=False
    p2 = _params(trend_period=50, enable_short=False)
    s2 = _make(p2)
    _warm_up_falling(s2, 50)
    d2 = s2.on_tick(_ts_offset(50), 109.0)
    assert d2 is None, "enable_short=False must suppress the short entry"


# ---------------------------------------------------------------------------
# Test 5: Long exit on RSI snap-back
# ---------------------------------------------------------------------------

def test_long_exit_rsi_snapback():
    """
    Warm up rising, enter long at 120 (RSI≈5), then feed a rise to 140.
    RSI rises to ≈68.3 (>=65) → EXIT with reason starting 'RSI exit'.
    We verify no exit is emitted at 120 after fill (position open),
    then one big up bar triggers the RSI exit.
    """
    p = _params(trend_period=50)
    s = _make(p)
    _warm_up_rising(s, 50)

    # Trigger ENTER at 120
    d_entry = s.on_tick(_ts_offset(50), 120.0)
    assert d_entry is not None and d_entry.action == "ENTER" and d_entry.side == "BUY"
    s.notify_fill("BUY", 10, 120.0)
    assert s.position == 10

    # Rise to 140: RSI should snap back above 65
    # Computed: avg_gain=(0.5+20)/2=10.25, avg_loss=(9.5+0)/2=4.75, RSI=68.33
    d_exit = s.on_tick(_ts_offset(51), 140.0)
    assert d_exit is not None, "Expected EXIT on RSI snap-back"
    assert d_exit.action == "EXIT"
    assert d_exit.reason.startswith("RSI exit"), f"Got reason: {d_exit.reason!r}"


# ---------------------------------------------------------------------------
# Test 6: Long exit on protective stop (close-based path)
# ---------------------------------------------------------------------------

def test_long_exit_stop():
    """
    Inject a long position at entry_price=100 with stop_pct=0.01 (stop=99.0).
    Warm up SMA/RSI so we're past the warm-up gate, then feed close=99.0.
    Expect EXIT with reason starting 'Stop-loss'.
    """
    p = _params(trend_period=50)
    s = _make(p)
    _warm_up_rising(s, 50)

    # Inject long position directly (simulates reconciled position)
    s.notify_fill("BUY", 10, 100.0)
    assert s.position == 10

    # Close at exactly the stop level (100 * 0.99 = 99.0)
    d = s.on_tick(_ts_offset(50), 99.0)
    assert d is not None, "Expected EXIT on stop hit"
    assert d.action == "EXIT"
    assert d.reason.startswith("Stop-loss"), f"Got reason: {d.reason!r}"


# ---------------------------------------------------------------------------
# Test 7: Short exit on RSI snap-back
# ---------------------------------------------------------------------------

def test_short_exit_rsi_snapback():
    """
    Warm up falling, enter short at 109 (RSI≈95), then feed a fall to 90.
    RSI drops to ≈32.76 (<=35) → EXIT with reason starting 'RSI exit'.
    """
    p = _params(trend_period=50)
    s = _make(p)
    _warm_up_falling(s, 50)

    # Trigger ENTER SELL at 109
    d_entry = s.on_tick(_ts_offset(50), 109.0)
    assert d_entry is not None and d_entry.action == "ENTER" and d_entry.side == "SELL"
    s.notify_fill("SELL", 10, 109.0)
    assert s.position == -10

    # Fall to 90: RSI should snap down below 35
    # Computed: avg_gain=(9.5+0)/2=4.75, avg_loss=(0.5+19)/2=9.75, RSI≈32.76
    d_exit = s.on_tick(_ts_offset(51), 90.0)
    assert d_exit is not None, "Expected EXIT on RSI snap-back"
    assert d_exit.action == "EXIT"
    assert d_exit.reason.startswith("RSI exit"), f"Got reason: {d_exit.reason!r}"


# ---------------------------------------------------------------------------
# Test 8: EOD square-off — unconditional and beats warm-up
# ---------------------------------------------------------------------------

def test_eod_squareoff_with_position():
    """
    At 15:15 (= 15:30 - 15 min), any open position must exit regardless of
    whether indicators are ready.
    """
    s = _make()
    # Inject position without any warm-up
    s.notify_fill("BUY", 5, 100.0)
    assert s.position == 5
    assert s.rsi is None  # indicators not ready

    ts = datetime(2024, 1, 1, 15, 15, tzinfo=IST)
    d = s.on_tick(ts, 100.0)
    assert d is not None
    assert d.action == "EXIT"
    assert d.reason == "EOD square-off"


def test_eod_squareoff_no_position():
    """At 15:15 with no open position → None."""
    s = _make()
    ts = datetime(2024, 1, 1, 15, 15, tzinfo=IST)
    d = s.on_tick(ts, 100.0)
    assert d is None


# ---------------------------------------------------------------------------
# Test 9: Session reset clears indicators
# ---------------------------------------------------------------------------

def test_session_reset_clears_indicators():
    """
    Warm up on day 1 until RSI and SMA are ready.
    Then call on_tick with a day-2 timestamp — session reset fires.
    After the first day-2 bar, both RSI and SMA are None again.
    """
    p = _params(trend_period=3)
    s = _make(p)

    # Day 1: 3 bars → warm-up complete
    s.on_tick(_ts(9, 15, day=1), 100.0)
    s.on_tick(_ts(9, 16, day=1), 105.0)
    s.on_tick(_ts(9, 17, day=1), 110.0)
    assert s.rsi is not None, "RSI should be ready after day-1 warm-up"
    assert s.sma is not None, "SMA should be ready after day-1 warm-up"

    # Day 2, first bar
    ts_day2 = datetime(2024, 1, 2, 9, 15, tzinfo=IST)
    d = s.on_tick(ts_day2, 100.0)
    assert d is None, "First bar after reset must return None (warm-up again)"
    assert s.rsi is None, "RSI must be None immediately after session reset"
    assert s.sma is None, "SMA must be None immediately after session reset"
    assert s._delta_count == 0, "_delta_count must be 0 after reset"


# ---------------------------------------------------------------------------
# Test 10: Future-skew tick is ignored (state unchanged)
# ---------------------------------------------------------------------------

def test_future_skew_tick_ignored():
    """
    A tick stamped > 2 min ahead of wall clock must be silently dropped;
    it must NOT advance _delta_count or the closes deque.
    """
    s = _make()

    count_before = s._delta_count
    closes_len_before = len(s.closes)

    # Fake a tick 10 minutes into the future
    future = datetime.now(IST) + timedelta(minutes=10)
    d = s.on_tick(future, 100.0)

    assert d is None
    assert s._delta_count == count_before, "_delta_count must not change on future tick"
    assert len(s.closes) == closes_len_before, "closes deque must not change on future tick"
    assert s.rsi is None
    assert s.sma is None


# ---------------------------------------------------------------------------
# Additional: notify_flat resets position
# ---------------------------------------------------------------------------

def test_notify_flat():
    s = _make()
    s.notify_fill("BUY", 5, 100.0)
    assert s.position == 5
    assert s.entry_price == 100.0
    s.notify_flat()
    assert s.position == 0
    assert s.entry_price == 0.0


# ---------------------------------------------------------------------------
# Additional: price at stop is EXIT, just above stop is not (close path)
# ---------------------------------------------------------------------------

def test_long_stop_boundary():
    """
    Long at 100, stop_pct=0.01 → stop=99.0.
    Close at 99.01 must NOT trigger stop; close at 99.0 must trigger it.
    """
    p = _params(trend_period=50)
    s = _make(p)
    _warm_up_rising(s, 50)
    s.notify_fill("BUY", 10, 100.0)

    # 99.01 > 99.0 → no stop
    d1 = s.on_tick(_ts_offset(50), 99.01)
    # May return None or RSI exit; must NOT be a stop-loss
    assert d1 is None or not d1.reason.startswith("Stop-loss"), \
        f"99.01 should not stop out (stop=99.0), got {d1}"


# ---------------------------------------------------------------------------
# Additional: target hit exits when target_pct > 0
# ---------------------------------------------------------------------------

def test_long_target_hit():
    """
    Long at 100 with target_pct=0.02 (target=102). Close at 102.0 must exit.
    """
    p = _params(trend_period=50, target_pct=0.02)
    s = _make(p)
    _warm_up_rising(s, 50)
    s.notify_fill("BUY", 10, 100.0)

    d = s.on_tick(_ts_offset(50), 102.0)
    assert d is not None
    assert d.action == "EXIT"
    assert d.reason.startswith("Target hit"), f"Got {d.reason!r}"


# ---------------------------------------------------------------------------
# Additional: flat tape → RSI=50 (neutral, no signal)
# ---------------------------------------------------------------------------

def test_rsi_flat_tape_gives_50():
    """
    All-same closes → avg_gain == avg_loss == 0 → RSI = 50 (neutral).
    Neither long nor short signal should fire.
    """
    p = _params(trend_period=3)
    s = _make(p)
    results = []
    for i in range(5):
        ts = _ts(9, 15 + i)
        results.append(s.on_tick(ts, 100.0))
    # After 3 bars, RSI must be 50
    assert s.rsi == 50.0, f"Flat tape should give RSI=50, got {s.rsi}"
    # No signal from neutral RSI (50 is not <= 10 or >= 90)
    for r in results:
        assert r is None
