"""
Unit tests for VwapTrend strategy (strategies/vwap_trend.py).

All tests drive on_tick directly with volume= keyword — no DB, no IO.
Parameters default to a small, hand-checkable set unless a case overrides:
  VwapTrendParams(slope_lag=2, slope_eps=0.0, entry_start_min=0,
                  touch_band_pct=0.002, reclaim_band_pct=0.0,
                  invalidate_band_pct=0.005, vwap_exit_band_pct=0.0,
                  sl_buffer_pct=0.01, far_target_mult=0.50)

With reclaim_band_pct=0 and vwap_exit_band_pct=0, triggers/exits fire on a
clean cross; slope_eps=0 means any positive/negative slope counts.
"""
from datetime import datetime

from strategies.vwap_trend import VwapTrend, VwapTrendParams

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DATE = "2024-03-01"


def ist(hh: int, mm: int) -> datetime:
    """Naive IST datetime on the test date (strategy treats naive as IST)."""
    return datetime(2024, 3, 1, hh, mm)


def _default_params(**overrides) -> VwapTrendParams:
    base = dict(
        slope_lag=2,
        slope_eps=0.0,
        entry_start_min=0,
        touch_band_pct=0.002,
        reclaim_band_pct=0.0,
        invalidate_band_pct=0.005,
        vwap_exit_band_pct=0.0,
        sl_buffer_pct=0.01,
        far_target_mult=0.50,
    )
    base.update(overrides)
    return VwapTrendParams(**base)


def _strat(**overrides) -> VwapTrend:
    return VwapTrend("TEST", _default_params(**overrides))


def tick(s: VwapTrend, hh: int, mm: int, close: float,
         high: float = None, low: float = None, vol: float = 1000.0):
    """Convenience wrapper: on_tick with volume= kwarg."""
    h = high if high is not None else close
    l = low if low is not None else close
    return s.on_tick(ist(hh, mm), close, high=h, low=l, volume=vol)


# ---------------------------------------------------------------------------
# T1 — warm-up: no volume → None
# ---------------------------------------------------------------------------

def test_t1_no_volume_returns_none():
    """Volume=0 / None keeps _cum_vol==0 so the strategy stays in warm-up."""
    s = _strat()
    # Pass 3 bars with vol=0 (or vol=None)
    assert tick(s, 9, 15, 100.0, vol=0.0) is None
    assert tick(s, 9, 16, 101.0, vol=0.0) is None
    assert tick(s, 9, 17, 102.0, vol=None) is None
    # Internal VWAP undefined
    assert s._cum_vol == 0.0


def test_t1_volume_none_kwarg():
    """Calling on_tick without volume= also stays in warm-up."""
    s = _strat()
    result = s.on_tick(ist(9, 15), 100.0, high=100.0, low=100.0)
    assert result is None
    assert s._cum_vol == 0.0


# ---------------------------------------------------------------------------
# T2 — VWAP accumulates correctly
# ---------------------------------------------------------------------------

def test_t2_vwap_accumulates():
    """When h=l=close, typical = close. Two equal-volume bars."""
    s = _strat()
    # bar1: typical=100, vol=1000 → cum_pv=100000, cum_vol=1000
    r1 = tick(s, 9, 15, 100.0, high=100.0, low=100.0, vol=1000.0)
    assert r1 is None  # slope_lag=2 needs ≥3 VWAP points before slope exists
    # bar2: typical=102, vol=1000 → cum_pv=202000, cum_vol=2000 → vwap=101.0
    r2 = tick(s, 9, 16, 102.0, high=102.0, low=102.0, vol=1000.0)
    assert r2 is None  # still no slope (need slope_lag+1 = 3 history points)
    vwap = s._cum_pv / s._cum_vol
    assert abs(vwap - 101.0) < 1e-9


# ---------------------------------------------------------------------------
# T3 — rising VWAP + pullback that holds → ENTER BUY
# ---------------------------------------------------------------------------

def test_t3_long_pullback_continuation():
    """Rising VWAP, price above; a bar dips its low to VWAP (arm);
    next bar's close reclaims VWAP → ENTER BUY."""
    s = _strat()

    # Feed 3 ascending bars to build rising VWAP and slope.
    # typical=close when h=l=close.
    # bar1: vwap=100
    tick(s, 9, 15, 100.0, high=100.0, low=100.0, vol=1000.0)
    # bar2: vwap = (100k+101k)/2k = 100.5
    tick(s, 9, 16, 101.0, high=101.0, low=101.0, vol=1000.0)
    # bar3: vwap = (100k+101k+105k)/3k = 102.0  slope = (102-100)/100 = +0.02 (rising)
    # price=105 >> vwap=102 → price above VWAP; prev_close=101, prev_vwap=100.5
    r3 = tick(s, 9, 17, 105.0, high=106.0, low=105.0, vol=1000.0)
    assert r3 is None  # no setup yet — prev close was above prev vwap already, but no wick touch

    # bar4: vwap rises further; price=106, high=106, low exactly at vwap level
    # First compute expected vwap after bar4: cum_pv += 106*1000=106000, cum_vol=4000
    # cum_pv = 100000+101000+105000+106000 = 412000; vwap = 412000/4000 = 103.0
    # low=103.0 <= vwap*(1+0.002) = 103.206 → arms PULLBACK_LONG
    # close=106.0 >= vwap*(1+0.0) = 103.0 → triggers reclaim
    # So in ONE bar: arm then trigger (spec allows this)
    # But we need prev_close (105) > prev_vwap (102) to arm
    d = tick(s, 9, 18, 106.0, high=106.0, low=103.0, vol=1000.0)
    assert d is not None
    assert d.action == "ENTER"
    assert d.side == "BUY"
    assert "long pullback" in d.reason

    # stop < close < target; stop is below VWAP (VWAP ≈ 103)
    assert d.stop < 106.0       # stop is below VWAP (VWAP around 103)
    assert d.target > 106.0     # far target = 106 * 1.5 = 159
    assert "slope" in d.reason


def test_t3_arm_then_trigger_two_bars():
    """Two-bar version: arm on one bar, trigger on the next."""
    s = _strat()

    # Build rising VWAP with slope
    tick(s, 9, 15, 100.0, high=100.0, low=100.0, vol=1000.0)
    tick(s, 9, 16, 101.0, high=101.0, low=101.0, vol=1000.0)
    # bar3: slope becomes available; vwap=102; price=105; prev_close=101>prev_vwap=100.5
    tick(s, 9, 17, 105.0, high=106.0, low=105.0, vol=1000.0)

    # ARM bar: price=104, low touches VWAP (≈103), close=104 but maybe not yet above vwap
    # cum_pv after bar4 = 100k+101k+105k+104k = 410k; cum_vol=4k; vwap=102.5
    # low=102.5 <= vwap*(1+0.002)=102.705 → arms; close=104.0>=vwap*(1+0)=102.5 → trigger in same bar
    # So let's make low far below vwap to arm, close just AT vwap (no reclaim, not above)
    # low=99 definitely touches; close=102.0 = exactly at vwap → reclaim_band=0 → triggers!
    # To get a two-bar scenario: make close on arm-bar below vwap.
    # Actually reclaim_band_pct=0 so close >= vwap*1.0 triggers immediately.
    # To separate arm and trigger: arm on bar with close < vwap (below reclaim threshold).
    # Use close=101.9 < vwap=102.5 → won't trigger; next bar close=103 > vwap → trigger.

    # ARM: cum_pv=100k+101k+105k+101.9k=407.9k; vwap=407.9/4=101.975
    # low=101.9 <= vwap*(1.002)=102.178 → arms
    # close=101.9 < vwap=101.975 → no trigger (below VWAP, not a reclaim)
    r_arm = tick(s, 9, 18, 101.9, high=102.0, low=101.9, vol=1000.0)
    assert r_arm is None  # armed but not triggered
    assert s._zone == "PULLBACK_LONG"

    # TRIGGER: bar5 — close above VWAP
    # cum_pv += 103*1000=103k; cum_vol=5000; cum_pv=510.9k; vwap=102.18
    # close=103 >= vwap=102.18 → triggers reclaim
    d = tick(s, 9, 19, 103.0, high=104.0, low=102.5, vol=1000.0)
    assert d is not None
    assert d.action == "ENTER" and d.side == "BUY"


# ---------------------------------------------------------------------------
# T4 — falling VWAP + rally into VWAP that rejects → ENTER SELL
# ---------------------------------------------------------------------------

def test_t4_short_rejection():
    """Descending VWAP; bar high reaches VWAP from below (arm);
    next close stays below VWAP → ENTER SELL."""
    s = _strat()

    # Descending bars
    tick(s, 9, 15, 105.0, high=105.0, low=105.0, vol=1000.0)
    tick(s, 9, 16, 103.0, high=103.0, low=103.0, vol=1000.0)
    # bar3: vwap = (105k+103k+100k)/3k = 102.67; price=100 below VWAP; slope<0
    tick(s, 9, 17, 100.0, high=100.0, low=100.0, vol=1000.0)

    # bar4: price=99 (still below VWAP); high reaches VWAP to arm
    # cum_pv=105k+103k+100k+99k=407k; vwap=407/4=101.75
    # high=101.75 >= vwap*(1-0.002)=101.547 → arms PULLBACK_SHORT
    # close=99 <= vwap*(1-0.0)=101.75 → triggers ENTER SELL in same bar
    # Need prev_close(100) < prev_vwap(102.67) ✓
    d = tick(s, 9, 18, 99.0, high=101.75, low=98.0, vol=1000.0)
    assert d is not None
    assert d.action == "ENTER"
    assert d.side == "SELL"
    assert "short rejection" in d.reason

    # target < close < vwap < stop
    assert d.target < 99.0
    assert d.stop > 99.0
    assert "slope" in d.reason


# ---------------------------------------------------------------------------
# T5 — flat VWAP blocks entry
# ---------------------------------------------------------------------------

def test_t5_flat_vwap_no_entry():
    """With slope_eps=0.001, a barely-moving VWAP blocks entries."""
    s = _strat(slope_eps=0.001)

    # Feed bars with nearly identical prices so VWAP barely moves
    tick(s, 9, 15, 100.0, high=100.0, low=100.0, vol=1000.0)
    tick(s, 9, 16, 100.001, high=100.001, low=100.001, vol=1000.0)
    # bar3: slope will be tiny (100.0007 - 100.0) / 100.0 ≈ 7e-6 << slope_eps=0.001
    # even if a "pullback" pattern appears, zone should be forced NONE
    d = tick(s, 9, 17, 100.001, high=100.1, low=100.0, vol=1000.0)
    assert d is None
    # Zone should be cleared because slope is flat
    assert s._zone == "NONE"


# ---------------------------------------------------------------------------
# T6 — pullback invalidation → no entry
# ---------------------------------------------------------------------------

def test_t6_pullback_invalidation():
    """PULLBACK_LONG is armed, then close breaks decisively below VWAP → zone reset, no ENTER."""
    s = _strat()

    # Build rising VWAP + slope
    tick(s, 9, 15, 100.0, high=100.0, low=100.0, vol=1000.0)
    tick(s, 9, 16, 101.0, high=101.0, low=101.0, vol=1000.0)
    tick(s, 9, 17, 105.0, high=106.0, low=105.0, vol=1000.0)  # slope +ve

    # ARM bar: low touches VWAP, close just barely below VWAP (no trigger)
    # vwap ≈ 101.975 after this bar
    r_arm = tick(s, 9, 18, 101.9, high=102.0, low=101.9, vol=1000.0)
    assert r_arm is None
    assert s._zone == "PULLBACK_LONG"

    # INVALIDATION: close breaks below vwap*(1 - invalidate_band_pct=0.005)
    # vwap after bar5: (100k+101k+105k+101.9k+100k)/5k = 407.9k/5k = ... let's compute
    # cum_pv=407.9k+100k*1 = actually need to use the actual bar close
    # close=101.5 < vwap*(1-0.005) = ~101.975*0.995 ≈ 101.465 ... that's not quite
    # Let's use a more decisively broken close: close=100, which is well below any VWAP
    # After bar5: cum_pv=407.9k+100k=507.9k; cum_vol=5k; vwap=101.58
    # invalidate threshold = 101.58 * (1-0.005) = 101.07
    # close=100.0 < 101.07 → invalidation fires
    d_invalid = tick(s, 9, 19, 100.0, high=101.0, low=99.0, vol=1000.0)
    assert d_invalid is None  # no ENTER on breakdown
    assert s._zone == "NONE"


# ---------------------------------------------------------------------------
# T7 — VWAP cross-back exits an open long
# ---------------------------------------------------------------------------

def test_t7_vwap_crossback_exits_long():
    """After ENTER BUY via T3 setup, a close below VWAP → EXIT 'VWAP cross-back (long)'."""
    s = _strat()

    # Set up a long entry (mirror T3)
    tick(s, 9, 15, 100.0, high=100.0, low=100.0, vol=1000.0)
    tick(s, 9, 16, 101.0, high=101.0, low=101.0, vol=1000.0)
    tick(s, 9, 17, 105.0, high=106.0, low=105.0, vol=1000.0)
    d_enter = tick(s, 9, 18, 106.0, high=106.0, low=103.0, vol=1000.0)
    assert d_enter is not None and d_enter.action == "ENTER" and d_enter.side == "BUY"

    s.notify_fill("BUY", 1, 106.0)
    assert s.position == 1

    # Now feed a bar whose close falls below VWAP
    # vwap continues to accumulate but must remain roughly 103-104 range
    # If we feed close=99 with h=l=99, vwap drops; close=99 < vwap*(1-0)=vwap → EXIT
    d_exit = tick(s, 9, 19, 99.0, high=99.5, low=98.0, vol=1000.0)
    assert d_exit is not None
    assert d_exit.action == "EXIT"
    assert "VWAP cross-back (long)" in d_exit.reason


def test_t7_no_new_short_on_crossback():
    """The VWAP cross-back exit does NOT simultaneously open a SELL entry."""
    s = _strat()

    tick(s, 9, 15, 100.0, high=100.0, low=100.0, vol=1000.0)
    tick(s, 9, 16, 101.0, high=101.0, low=101.0, vol=1000.0)
    tick(s, 9, 17, 105.0, high=106.0, low=105.0, vol=1000.0)
    d_enter = tick(s, 9, 18, 106.0, high=106.0, low=103.0, vol=1000.0)
    assert d_enter and d_enter.action == "ENTER"
    s.notify_fill("BUY", 1, 106.0)

    d_exit = tick(s, 9, 19, 99.0, high=99.5, low=98.0, vol=1000.0)
    assert d_exit is not None
    assert d_exit.action == "EXIT"  # only EXIT, not ENTER SELL


# ---------------------------------------------------------------------------
# T8 — protective stop on close exits a short
# ---------------------------------------------------------------------------

def test_t8_stop_on_close_exits_short():
    """After SELL ENTER, close >= stop_level fires the stop exit."""
    s = _strat()

    # Build descending VWAP for a short entry (mirror T4)
    tick(s, 9, 15, 105.0, high=105.0, low=105.0, vol=1000.0)
    tick(s, 9, 16, 103.0, high=103.0, low=103.0, vol=1000.0)
    tick(s, 9, 17, 100.0, high=100.0, low=100.0, vol=1000.0)
    d_enter = tick(s, 9, 18, 99.0, high=101.75, low=98.0, vol=1000.0)
    assert d_enter is not None and d_enter.action == "ENTER" and d_enter.side == "SELL"

    s.notify_fill("SELL", 1, 99.0)
    assert s.position == -1

    # Feed a bar that closes exactly at the stop level (stop fires)
    # but whose close is still below the VWAP cross-back threshold
    # stop is above vwap so close=stop crosses vwap too → vwap_exit fires first
    # To isolate the stop: make close just at stop, with exit_band=0 meaning
    # cross-back fires when close > vwap. stop > vwap so closing at stop means
    # both would fire; the code checks VWAP cross-back FIRST.
    # Test: with vwap_exit_band_pct=0, close at stop (above vwap) → cross-back fires.
    # The spec says: test stop-only path too (close at stop but on short side of vwap).
    # That requires vwap_exit_band_pct > 0 so there's a gap between vwap and exit trigger.
    # Use a separate strategy with nonzero exit band.
    s2 = VwapTrend("TEST2", VwapTrendParams(
        slope_lag=2, slope_eps=0.0, entry_start_min=0,
        touch_band_pct=0.002, reclaim_band_pct=0.0, invalidate_band_pct=0.005,
        vwap_exit_band_pct=0.02,  # 2% band — needs close > vwap*1.02 to cross-back exit
        sl_buffer_pct=0.01, far_target_mult=0.50,
    ))
    tick(s2, 9, 15, 105.0, high=105.0, low=105.0, vol=1000.0)
    tick(s2, 9, 16, 103.0, high=103.0, low=103.0, vol=1000.0)
    tick(s2, 9, 17, 100.0, high=100.0, low=100.0, vol=1000.0)
    d2 = tick(s2, 9, 18, 99.0, high=101.75, low=98.0, vol=1000.0)
    assert d2 is not None and d2.side == "SELL"
    stop2 = d2.stop  # vwap * 1.01
    s2.notify_fill("SELL", 1, 99.0)

    # Feed a bar whose close is at stop_level but below vwap*(1+0.02) cross-back trigger
    # vwap after next bar is around 101 range; stop2 ≈ vwap*1.01 ≈ 102
    # cross-back exit needs close > vwap*1.02 ≈ 103
    # close=stop2 ≈ 102 < 103 → only stop fires
    # For this bar: cum_pv adds close*vol; let's just ensure close == stop2
    close_at_stop = stop2
    d_stop = tick(s2, 9, 19, close_at_stop, high=close_at_stop, low=close_at_stop, vol=1000.0)
    assert d_stop is not None
    assert d_stop.action == "EXIT"
    assert "Stop-loss" in d_stop.reason


def test_t8_crossback_has_priority_over_stop_for_short():
    """When both VWAP cross-back and stop would fire, cross-back fires first (higher priority)."""
    s = _strat(vwap_exit_band_pct=0.0, sl_buffer_pct=0.0001)
    # Build a short position
    tick(s, 9, 15, 105.0, high=105.0, low=105.0, vol=1000.0)
    tick(s, 9, 16, 103.0, high=103.0, low=103.0, vol=1000.0)
    tick(s, 9, 17, 100.0, high=100.0, low=100.0, vol=1000.0)
    d_enter = tick(s, 9, 18, 99.0, high=101.75, low=98.0, vol=1000.0)
    assert d_enter and d_enter.side == "SELL"
    s.notify_fill("SELL", 1, 99.0)

    # Feed a bar that would trigger both exits: close > vwap and >= stop
    # With tiny sl_buffer, stop ≈ vwap. Close well above vwap fires cross-back first.
    d = tick(s, 9, 19, 120.0, high=120.0, low=119.0, vol=1000.0)
    assert d is not None and d.action == "EXIT"
    assert "VWAP cross-back (short)" in d.reason


# ---------------------------------------------------------------------------
# T9 — EOD square-off is unconditional and dominates
# ---------------------------------------------------------------------------

def test_t9_eod_squareoff_open_position():
    """EOD exits any open position regardless of VWAP state."""
    s = _strat(squareoff_before_close_min=15)
    # Force a position without going through the full entry flow
    s.notify_fill("BUY", 1, 100.0)
    assert s.position == 1
    # 15:30 - 15 = 15:15
    d = s.on_tick(ist(15, 15), 100.0, high=100.0, low=100.0, volume=0.0)
    assert d is not None
    assert d.action == "EXIT"
    assert "EOD square-off" in d.reason


def test_t9_eod_squareoff_with_zero_vwap():
    """EOD square-off fires even when _cum_vol == 0 (unconditional)."""
    s = _strat(squareoff_before_close_min=15)
    s.notify_fill("SELL", 1, 100.0)
    assert s._cum_vol == 0.0  # no volume fed
    d = s.on_tick(ist(15, 16), 99.0, high=100.0, low=98.0)  # no volume kwarg
    assert d is not None
    assert d.action == "EXIT"
    assert "EOD square-off" in d.reason


def test_t9_eod_flat_position_returns_none():
    """EOD with flat position returns None."""
    s = _strat(squareoff_before_close_min=15)
    d = s.on_tick(ist(15, 15), 100.0, high=100.0, low=100.0, volume=0.0)
    assert d is None


# ---------------------------------------------------------------------------
# T10 — session reset clears VWAP across a date change
# ---------------------------------------------------------------------------

def test_t10_session_reset():
    """Day 2 starts fresh: _cum_vol=0, slope history empty, _zone='NONE'."""
    # Build up state on day 1
    s = _strat()
    tick(s, 9, 15, 100.0, high=100.0, low=100.0, vol=1000.0)
    tick(s, 9, 16, 101.0, high=101.0, low=101.0, vol=1000.0)
    tick(s, 9, 17, 105.0, high=106.0, low=105.0, vol=1000.0)
    assert s._cum_vol > 0
    assert len(s._vwap_hist) > 0

    # Day 2 — first tick
    day2 = datetime(2024, 3, 4, 9, 16)  # next trading day
    d2 = s.on_tick(day2, 101.0, high=101.0, low=101.0, volume=1000.0)
    assert d2 is None  # warm-up: slope needs slope_lag+1 points
    # After reset + one bar: cum_vol=1000, vwap_hist has 1 point, slope=None
    assert s._session_date == day2.date()
    assert s._cum_vol == 1000.0
    assert len(s._vwap_hist) == 1  # only 1 VWAP value, not enough for slope
    assert s._zone == "NONE"
    assert s._prev_vwap is not None  # updated after the first valid bar


def test_t10_no_cross_session_leakage():
    """Day-2 replay of day-1 bars produces the same results as day 1."""
    def run_sequence(day_dt_fn):
        """Feed the same 4-bar sequence, return the list of decisions."""
        s = _strat()
        results = []
        results.append(tick(s, 9, 15, 100.0, high=100.0, low=100.0, vol=1000.0))
        results.append(tick(s, 9, 16, 101.0, high=101.0, low=101.0, vol=1000.0))
        results.append(tick(s, 9, 17, 105.0, high=106.0, low=105.0, vol=1000.0))
        results.append(tick(s, 9, 18, 106.0, high=106.0, low=103.0, vol=1000.0))
        return results, s

    # Day 1
    decisions_day1, s1 = run_sequence(lambda hh, mm: datetime(2024, 3, 1, hh, mm))
    # Record what day 1 produced
    last_day1 = decisions_day1[-1]

    # Day 2: on same strategy object, advance to new date and replay
    s = _strat()
    # day1
    tick(s, 9, 15, 100.0, high=100.0, low=100.0, vol=1000.0)
    tick(s, 9, 16, 101.0, high=101.0, low=101.0, vol=1000.0)
    tick(s, 9, 17, 105.0, high=106.0, low=105.0, vol=1000.0)
    tick(s, 9, 18, 106.0, high=106.0, low=103.0, vol=1000.0)  # potential entry

    # day2: cross-session with on_tick
    s.on_tick(datetime(2024, 3, 4, 9, 15), 100.0, high=100.0, low=100.0, volume=1000.0)
    s.on_tick(datetime(2024, 3, 4, 9, 16), 101.0, high=101.0, low=101.0, volume=1000.0)
    s.on_tick(datetime(2024, 3, 4, 9, 17), 105.0, high=106.0, low=105.0, volume=1000.0)
    d_day2_bar4 = s.on_tick(datetime(2024, 3, 4, 9, 18), 106.0, high=106.0, low=103.0, volume=1000.0)

    # Day-2 replay produces same pattern as day-1 replay
    assert type(d_day2_bar4) == type(last_day1)
    if last_day1 is not None:
        assert d_day2_bar4.action == last_day1.action
        assert d_day2_bar4.side == last_day1.side


# ---------------------------------------------------------------------------
# T11 — entry_start delay blocks early setups
# ---------------------------------------------------------------------------

def test_t11_entry_start_blocks_early():
    """With entry_start_min=15, a valid pullback at 09:20 → None."""
    s = _strat(entry_start_min=15, slope_eps=0.0)

    # Build rising VWAP with slope — using times in the 09:15-09:30 window
    s.on_tick(datetime(2024, 3, 1, 9, 15), 100.0, high=100.0, low=100.0, volume=1000.0)
    s.on_tick(datetime(2024, 3, 1, 9, 16), 101.0, high=101.0, low=101.0, volume=1000.0)
    s.on_tick(datetime(2024, 3, 1, 9, 17), 105.0, high=106.0, low=105.0, volume=1000.0)

    # Valid pullback-and-reclaim at 09:18 — within first 15 min from MARKET_OPEN(09:15)
    # entry_start = 09:15 + 15min = 09:30; 09:18 < 09:30 → blocked
    d_early = s.on_tick(datetime(2024, 3, 1, 9, 18), 106.0, high=106.0, low=103.0, volume=1000.0)
    assert d_early is None  # blocked by entry_start gate


def test_t11_entry_start_allows_after_window():
    """Same setup after 09:30 → produces ENTER BUY."""
    s = _strat(entry_start_min=15, slope_eps=0.0)

    # These bars are AFTER 09:30, so entry gate is open
    s.on_tick(datetime(2024, 3, 1, 9, 31), 100.0, high=100.0, low=100.0, volume=1000.0)
    s.on_tick(datetime(2024, 3, 1, 9, 32), 101.0, high=101.0, low=101.0, volume=1000.0)
    s.on_tick(datetime(2024, 3, 1, 9, 33), 105.0, high=106.0, low=105.0, volume=1000.0)
    d = s.on_tick(datetime(2024, 3, 1, 9, 34), 106.0, high=106.0, low=103.0, volume=1000.0)
    assert d is not None
    assert d.action == "ENTER"
    assert d.side == "BUY"


# ---------------------------------------------------------------------------
# T12 — orientation guard: trend, NOT mean-reversion
# ---------------------------------------------------------------------------

def test_t12_no_buy_on_deep_discount_below_vwap():
    """A rising VWAP with price far below it should NOT trigger a BUY.
    This confirms trend-following, not mean-reversion.
    """
    s = _strat()

    # Build rising VWAP: bars at 100, 102, 105 → vwap rising
    tick(s, 9, 15, 100.0, high=100.0, low=100.0, vol=1000.0)
    tick(s, 9, 16, 102.0, high=102.0, low=102.0, vol=1000.0)
    # bar3: slope available; vwap = (100k+102k+105k)/3k = 102.33, slope +ve
    tick(s, 9, 17, 105.0, high=106.0, low=105.0, vol=1000.0)

    # bar4: price far BELOW rising VWAP — no pullback arming occurred, prev_close was NOT above vwap
    # prev_close=105 > prev_vwap≈100.67... wait, let's check:
    # After bar1: vwap=100; after bar2: vwap=101; after bar3: vwap=(100+102+105)/3=102.33
    # Wait: equal volumes, so vwap = simple avg of closes when h=l=close.
    # prev_close for bar4 = 105 (bar3 close), prev_vwap = 102.33
    # prev_close(105) > prev_vwap(102.33) → arming condition holds if low touches vwap!
    # To test "deep discount, no pullback arm": price goes far below VWAP WITHOUT the low touching first.
    # Use a bar where close AND low are both far below VWAP (no touch of vwap from above).
    # Actually bar4 with low=80 would touch VWAP (80 < 102.33*(1+0.002)=102.53), arming it,
    # and if close also recovers above VWAP it would trigger.
    # For the MR test: price is far below without a touch pattern that leads to reclaim.
    # Make close=80 (way below vwap), which won't trigger reclaim (close < vwap).
    # Expected: arms PULLBACK_LONG (because prev_close>prev_vwap and low<vwap), but
    # close is way below → no reclaim trigger → None.
    d = tick(s, 9, 18, 80.0, high=85.0, low=79.0, vol=1000.0)
    # Should be None: low touched VWAP (arm) but close is way below VWAP (no reclaim)
    assert d is None
    # zone should be PULLBACK_LONG (armed but not triggered) unless invalidate fires
    # invalidate: close < vwap*(1-0.005). vwap after bar4 ≈ (100k+102k+105k+80k)/4k = 96.75
    # invalidate threshold = 96.75*(1-0.005)=96.27; close=80 < 96.27 → INVALIDATED
    assert s._zone == "NONE"  # invalidation fired, not a MR entry


def test_t12_buy_only_fires_on_pullback_reclaim_not_deep_dip():
    """A price far below rising VWAP that never arms-then-reclaims produces no ENTER BUY.
    This is the critical anti-MR assertion: we do not buy because price is 'cheap' vs VWAP."""
    s = _strat()

    # Rising VWAP
    tick(s, 9, 15, 100.0, high=100.0, low=100.0, vol=1000.0)
    tick(s, 9, 16, 102.0, high=102.0, low=102.0, vol=1000.0)
    tick(s, 9, 17, 104.0, high=104.0, low=104.0, vol=1000.0)
    # slope positive; vwap = 102

    # bar4: price plunges below VWAP with high still below VWAP (no touch from above)
    # prev_close=104 > prev_vwap=101 → arming condition on LOW would arm it
    # But let's confirm: even if it arms and then close is still below, no entry fires.
    d = tick(s, 9, 18, 90.0, high=95.0, low=89.0, vol=1000.0)
    assert d is None  # no ENTER BUY because close is NOT above/at VWAP

    # bar5: still below vwap, high not reaching vwap
    d2 = tick(s, 9, 19, 88.0, high=92.0, low=87.0, vol=1000.0)
    assert d2 is None
    # Total: no BUY triggered despite price being far below VWAP for 2 bars.
    # An MR strategy would have bought here — this proves trend-only behavior.
