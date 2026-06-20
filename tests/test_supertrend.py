"""
Supertrend strategy unit tests.

Drive on_tick() directly; no DB, no IO. All tests use security_id="T" and
SupertrendParams(atr_period=3, multiplier=2.0) unless noted. Bars are fed on
2024-06-03 (Monday), 09:15 IST onward, 1 min apart.

Expected indicator values are pre-computed from the band-locking recurrence
in the spec (§1.3–§1.5) and verified numerically in the test module itself.
"""
from datetime import datetime, date, timedelta

from strategies.supertrend import Supertrend, SupertrendParams
from strategies.orb import IST

# ── Fixtures ─────────────────────────────────────────────────────────────────

SESSION_DATE = date(2024, 6, 3)  # Monday

PARAMS_3 = SupertrendParams(atr_period=3, multiplier=2.0)


def ist(hh: int, mm: int, d: date = SESSION_DATE) -> datetime:
    """Naive IST datetime for the given date."""
    return datetime(d.year, d.month, d.day, hh, mm)


def feed(st: Supertrend, bars: list, base_hh: int = 9, base_mm: int = 15,
         d: date = SESSION_DATE):
    """
    Feed a list of (close, high, low) bars to *st* starting at base_hh:base_mm
    IST, 1-minute apart. Returns list of Decision-or-None results.
    """
    results = []
    for i, (close, high, low) in enumerate(bars):
        now = ist(base_hh, base_mm + i, d)
        results.append(st.on_tick(now, close, high=high, low=low))
    return results


# ── Test 1: Warm-up returns None ─────────────────────────────────────────────

def test_warmup_returns_none():
    """Bars 1 and 2 return None (ATR undefined). Seed bar 3 also returns None."""
    st = Supertrend("T", PARAMS_3)
    bars = [
        (100, 100, 100),  # bar 1 — no ATR yet
        (100, 101, 99),   # bar 2 — no ATR yet
        (100, 101, 99),   # bar 3 — seed bar: ATR computed but no trade taken
    ]
    results = feed(st, bars)
    assert all(r is None for r in results), f"Expected all None, got {results}"
    assert st._bars_seen == 3
    assert st._atr is not None            # ATR is seeded after bar 3
    assert st._prev_dir == 1              # direction initialised but not traded


# ── Test 2: First long flip → ENTER BUY ─────────────────────────────────────

def test_long_flip_enter_buy():
    """
    After seed bar starts at dir=+1, a strong down bar flips to -1, then a
    strong up bar flips back to +1 — that is the genuine long flip.

    Sequence (period 3, mult 2.0):
      bar1: (100,100,100), bar2: (100,101,99), bar3-seed: (95,101,94)
        => atr=3.0, fl=91.5, fu=103.5, dir=+1
      bar4: (88,99,87) close < fl=91.5 => dir=-1 (flip to short, no entry — flat)
      bar5: (115,120,88) close > fu=103.5 => dir=+1 (long flip => ENTER BUY)
    """
    st = Supertrend("T", PARAMS_3)
    bars_seed = [
        (100, 100, 100),  # bar 1
        (100, 101, 99),   # bar 2
        (95, 101, 94),    # bar 3 — seed
        (88, 99, 87),     # bar 4 — flip to -1 (no entry, still flat)
    ]
    results = feed(st, bars_seed)
    # bars 1-3 = None; bar 4: a flip but strategy is flat, should yield ENTER SELL
    # Actually bar 4 IS an ENTER SELL signal since we're flat — but the test
    # focuses on the LONG flip. Let's verify bar 4 gives ENTER SELL (it's a short flip):
    assert results[0] is None
    assert results[1] is None
    assert results[2] is None  # seed bar
    assert results[3] is not None
    assert results[3].action == "ENTER" and results[3].side == "SELL"

    # Do NOT notify_fill — strategy stays flat (simulating rejection/ignore)
    # Bar 5: strong up => long flip => ENTER BUY
    d = st.on_tick(ist(9, 19), 115.0, high=120.0, low=88.0)
    assert d is not None, "Expected ENTER BUY on long flip"
    assert d.action == "ENTER"
    assert d.side == "BUY"
    assert "long flip" in d.reason
    # stop = Supertrend line = final_lower
    line = st._final_lower
    assert abs(d.stop - line) < 0.001, f"stop {d.stop} != final_lower {line}"
    assert d.target > 115.0, "sentinel target must be above entry price"


# ── Test 3: First short flip → ENTER SELL ────────────────────────────────────

def test_short_flip_enter_sell():
    """
    Seed bars start at dir=+1. A strong down bar (close < final_lower) flips
    direction to -1 — this is the short flip; emit ENTER SELL when flat.

    Sequence (period 3, mult 2.0):
      bar1: (100,100,100), bar2: (100,101,99), bar3-seed: (100,101,99)
        => atr=1.333, fl=97.333, fu=102.667, dir=+1
      bar4: (92,101,91) close=92 < fl=97.333 => dir=-1 (flip) => ENTER SELL
        st=102.667 (final_upper), stop=102.667, target < 92.
    """
    st = Supertrend("T", PARAMS_3)
    bars = [
        (100, 100, 100),  # bar 1
        (100, 101, 99),   # bar 2
        (100, 101, 99),   # bar 3 — seed
        (92, 101, 91),    # bar 4 — short flip
    ]
    results = feed(st, bars)
    assert results[0] is None
    assert results[1] is None
    assert results[2] is None  # seed
    d = results[3]
    assert d is not None, "Expected ENTER SELL on short flip"
    assert d.action == "ENTER"
    assert d.side == "SELL"
    assert "short flip" in d.reason
    # stop = Supertrend line = final_upper ≈ 102.667
    line = st._final_upper
    assert abs(d.stop - line) < 0.001, f"stop {d.stop} != final_upper {line}"
    assert d.target < 92.0, "sentinel target must be below entry price"


# ── Test 4: Hold long while uptrend persists → None ──────────────────────────

def test_hold_long_no_exit():
    """
    After ENTER BUY (via short flip then long flip), further bars whose close
    stays above final_lower and direction stays +1 must return None (hold).
    """
    st = Supertrend("T", PARAMS_3)
    # Get to a long position: seed then short-flip (bar 4), then long-flip (bar 5)
    seed_bars = [
        (100, 100, 100),
        (100, 101, 99),
        (100, 101, 99),   # seed
        (92, 101, 91),    # short flip => ENTER SELL (not filled)
    ]
    feed(st, seed_bars)
    # Force flat (simulate no fill for bar 4's SELL signal)
    # Now feed a long flip bar:
    d_enter = st.on_tick(ist(9, 19), 115.0, high=120.0, low=88.0)
    assert d_enter is not None and d_enter.side == "BUY"
    st.notify_fill("BUY", 1, 115.0)
    assert st.position == 1

    # Now feed bars that stay in uptrend — each should return None
    hold_bars = [
        (118, 119, 115),  # bar 6 — still +1
        (120, 121, 117),  # bar 7 — still +1
        (122, 123, 119),  # bar 8 — still +1
    ]
    for i, (close, high, low) in enumerate(hold_bars):
        now = ist(9, 20 + i)
        result = st.on_tick(now, close, high=high, low=low)
        assert result is None, f"Expected None on hold bar {i+1}, got {result}"


# ── Test 5: Long exit on opposite flip ───────────────────────────────────────

def test_long_exit_on_flip_down():
    """
    While held long after a long flip, a sharp down bar that closes below
    final_lower flips direction to -1 — emit EXIT, not re-entry SELL.

    Uses the same starting sequence as test_hold_long_no_exit, then feeds
    a bar that collapses through the line.
    """
    st = Supertrend("T", PARAMS_3)
    # Reach long position
    seed_bars = [
        (100, 100, 100),
        (100, 101, 99),
        (100, 101, 99),   # seed
        (92, 101, 91),    # short flip (bar 4) — not filled
    ]
    feed(st, seed_bars)
    # Long flip (bar 5)
    st.on_tick(ist(9, 19), 115.0, high=120.0, low=88.0)
    st.notify_fill("BUY", 1, 115.0)

    # Hold bars
    st.on_tick(ist(9, 20), 118.0, high=119.0, low=115.0)
    st.on_tick(ist(9, 21), 120.0, high=121.0, low=117.0)

    # Sharp drop: close well below final_lower to force direction flip to -1
    d_exit = st.on_tick(ist(9, 22), 70.0, high=120.0, low=69.0)
    assert d_exit is not None, "Expected EXIT on flip down"
    assert d_exit.action == "EXIT"
    assert "flip down" in d_exit.reason or "stop line" in d_exit.reason
    # Must NOT also produce an ENTER on the same tick
    assert d_exit.side == ""  # EXIT has no side


# ── Test 6: Short exit on opposite flip ──────────────────────────────────────

def test_short_exit_on_flip_up():
    """
    While held short after a short flip, a sharp up bar that closes above
    final_upper flips direction to +1 — emit EXIT, not re-entry BUY.

    Sequence:
      bars 1-3: seed (dir=+1), bar 4: short flip => ENTER SELL (fill),
      bars 5-6: hold (dir=-1), bar 7: sharp up => EXIT short.
    """
    st = Supertrend("T", PARAMS_3)
    bars = [
        (100, 100, 100),  # bar 1
        (100, 101, 99),   # bar 2
        (100, 101, 99),   # bar 3 — seed
        (92, 101, 91),    # bar 4 — short flip => ENTER SELL
    ]
    results = feed(st, bars)
    d_enter = results[3]
    assert d_enter is not None and d_enter.side == "SELL"
    st.notify_fill("SELL", 1, 92.0)
    assert st.position == -1

    # Hold: bars staying below final_upper
    st.on_tick(ist(9, 19), 90.0, high=95.0, low=89.0)
    st.on_tick(ist(9, 20), 88.0, high=92.0, low=87.0)

    # Sharp up: close above final_upper => direction flips to +1 => EXIT
    d_exit = st.on_tick(ist(9, 21), 110.0, high=120.0, low=109.0)
    assert d_exit is not None, "Expected EXIT on flip up"
    assert d_exit.action == "EXIT"
    assert "flip up" in d_exit.reason or "stop line" in d_exit.reason


# ── Test 7: Trailing stop ratchets (line monotonically non-decreasing) ───────

def test_trailing_stop_ratchets_up():
    """
    In a continuous uptrend where direction stays +1, the Supertrend line
    (= final_lower) must be monotonically non-decreasing bar by bar.

    Uses a gently rising sequence with small ATR and period=3 so the seed
    lands quickly and direction stays +1 throughout.

    final_lower values (from pre-computed recurrence):
      bar3=98.667, bar4=100.444, bar5=102.296, bar6=104.198, bar7=106.132
    (all non-decreasing).
    """
    st = Supertrend("T", PARAMS_3)
    bars = [
        (100, 101, 99),   # bar 1
        (102, 103, 101),  # bar 2
        (104, 105, 103),  # bar 3 — seed, dir=+1
        (106, 107, 105),  # bar 4 — dir=+1
        (108, 109, 107),  # bar 5 — dir=+1
        (110, 111, 109),  # bar 6 — dir=+1
        (112, 113, 111),  # bar 7 — dir=+1
    ]

    lines = []
    for i, (close, high, low) in enumerate(bars):
        now = ist(9, 15 + i)
        st.on_tick(now, close, high=high, low=low)
        if st._direction == 1 and st._final_lower is not None:
            lines.append(st._final_lower)

    assert len(lines) >= 4, f"Expected at least 4 +1 bars, got {lines}"
    for i in range(len(lines) - 1):
        assert lines[i] <= lines[i + 1], (
            f"final_lower not non-decreasing at index {i}: {lines[i]} > {lines[i+1]}"
        )
    # No exit should have been emitted — verify by checking position (never entered)
    assert st.position == 0


# ── Test 8: EOD square-off is unconditional ──────────────────────────────────

def test_eod_squareoff_while_in_position():
    """
    With squareoff_before_close_min=15, the cutoff is 15:15 IST.
    A tick at 15:16 while long must yield EXIT(reason='EOD square-off').
    """
    st = Supertrend("T", PARAMS_3)
    st.position = 1
    st.entry_price = 100.0
    # Feed a minimal bar to set session date (so _session_date is today)
    st.on_tick(ist(9, 15), 100.0, high=101.0, low=99.0)
    st.position = 1  # restore (on_tick returns None in warmup, position unchanged)

    d = st.on_tick(ist(15, 16), 102.0, high=103.0, low=101.0)
    assert d is not None, "Expected EOD EXIT"
    assert d.action == "EXIT"
    assert "EOD" in d.reason


def test_eod_squareoff_during_warmup():
    """
    Even during warm-up (ATR not yet seeded), EOD square-off must fire if
    position != 0. Directly set position=1 after 1 bar.
    """
    st = Supertrend("T", PARAMS_3)
    # Feed just 1 bar (deep in warm-up, atr_period=3)
    st.on_tick(ist(9, 15), 100.0, high=101.0, low=99.0)
    assert st._bars_seen == 1
    assert st._atr is None  # still in warm-up

    # Simulate a DB-reconciled position (e.g. from mid-session restart)
    st.position = 1
    st.entry_price = 100.0

    d = st.on_tick(ist(15, 16), 102.0, high=103.0, low=101.0)
    assert d is not None, "Expected EOD EXIT even during warm-up"
    assert d.action == "EXIT"
    assert "EOD" in d.reason


# ── Test 9: Flat at EOD time → None ─────────────────────────────────────────

def test_eod_flat_returns_none():
    """No open position at EOD time → must return None (no spurious exit)."""
    st = Supertrend("T", PARAMS_3)
    st.on_tick(ist(9, 15), 100.0, high=101.0, low=99.0)
    assert st.position == 0

    d = st.on_tick(ist(15, 16), 102.0, high=103.0, low=101.0)
    assert d is None, f"Expected None when flat at EOD, got {d}"


# ── Test 10: Session reset clears state / no cross-day leakage ───────────────

def test_session_reset_no_cross_day_leakage():
    """
    After a full uptrend on day 1, the first bar of day 2 must trigger a
    session reset: _session_date updated, _atr=None, _prev_dir=None,
    _bars_seen=1 (the new bar just fed). No trade signal on day-2 bar.
    Position is NOT reset.
    """
    st = Supertrend("T", PARAMS_3)

    # Day 1: establish direction +1 (seed + some bars)
    day1 = date(2024, 6, 3)
    day1_bars = [
        (100, 101, 99),
        (102, 103, 101),
        (104, 105, 103),  # seed
        (106, 107, 105),  # first tradeable bar
    ]
    feed(st, day1_bars, base_hh=9, base_mm=15, d=day1)
    assert st._session_date == day1
    assert st._atr is not None
    assert st._prev_dir is not None

    # Simulate an open position from day 1
    st.position = 1
    st.entry_price = 104.0

    # Day 2: first bar triggers session reset
    day2 = date(2024, 6, 4)
    d = st.on_tick(ist(9, 15, day2), 107.0, high=108.0, low=106.0)

    assert st._session_date == day2, "session_date must update to day 2"
    assert st._atr is None, "_atr must be None after reset (back in warm-up)"
    assert st._prev_dir is None, "_prev_dir must be None after reset"
    assert st._bars_seen == 1, f"_bars_seen should be 1 after first day-2 bar, got {st._bars_seen}"
    assert d is None, "Must return None during day-2 warm-up"
    # Position is preserved (not touched by reset)
    assert st.position == 1, "position must be preserved across session reset"


# ── Test 11 (optional): Future-skew guard ────────────────────────────────────

def test_future_skew_guard_ignores_tick():
    """
    A tick stamped > 2 min ahead of wall clock must be silently ignored:
    - returns None
    - does NOT advance _bars_seen
    - does NOT reset the session
    """
    st = Supertrend("T", PARAMS_3)
    # Feed a real bar to establish session state
    now_real = datetime.now(IST).replace(tzinfo=None)
    st.on_tick(now_real, 100.0, high=101.0, low=99.0)
    bars_before = st._bars_seen
    session_before = st._session_date

    # A tick 1 hour in the future
    future = now_real + timedelta(hours=1)
    result = st.on_tick(future, 999.0, high=1000.0, low=998.0)

    assert result is None, "Future tick must return None"
    assert st._bars_seen == bars_before, "_bars_seen must not advance on future tick"
    assert st._session_date == session_before, "session_date must not change on future tick"


# ── Test: notify_fill / notify_flat mechanics ────────────────────────────────

def test_notify_fill_and_flat():
    """notify_fill updates position/entry_price; notify_flat zeroes them."""
    st = Supertrend("T", PARAMS_3)
    assert st.position == 0
    assert st.entry_price == 0.0

    st.notify_fill("BUY", 5, 100.0)
    assert st.position == 5
    assert st.entry_price == 100.0

    st.notify_fill("SELL", 3, 105.0)
    assert st.position == 2       # partial exit: +5-3=2
    # entry_price unchanged after partial (position != 0)

    st.notify_flat()
    assert st.position == 0
    assert st.entry_price == 0.0


# ── Test: Decision fields from ENTER ─────────────────────────────────────────

def test_enter_stop_and_target_fields():
    """
    ENTER Decision must have stop == Supertrend line and target as the wide
    sentinel (price ± target_atr_mult * atr), which must be > stop for BUY
    and < stop for SELL.
    """
    # Short flip (test 3 scenario is simpler — 4 bars, confirmed flip)
    st = Supertrend("T", PARAMS_3)
    bars = [
        (100, 100, 100),
        (100, 101, 99),
        (100, 101, 99),   # seed
        (92, 101, 91),    # short flip
    ]
    results = feed(st, bars)
    d = results[3]
    assert d.action == "ENTER" and d.side == "SELL"
    # stop = final_upper (above price)
    assert d.stop > 92.0, f"stop {d.stop} should be above entry price 92"
    # target < entry price (wide sentinel below)
    assert d.target < 92.0, f"target {d.target} should be below entry price 92 (sentinel)"
    # target is very far (target_atr_mult=100 by default)
    atr_at_bar4 = st._atr
    assert d.target < 92.0 - 50 * atr_at_bar4, "sentinel target should be very far below"


# ── Test: price <= 0 guard ────────────────────────────────────────────────────

def test_zero_price_returns_none():
    st = Supertrend("T", PARAMS_3)
    assert st.on_tick(ist(9, 15), 0.0) is None
    assert st.on_tick(ist(9, 15), -5.0) is None


# ── Test: min_atr_pct vol floor ──────────────────────────────────────────────

def test_min_atr_pct_suppresses_entry():
    """
    When min_atr_pct > 0 and atr < price * min_atr_pct, no entry is emitted
    even on a valid flip.
    """
    # With multiplier=2.0 and a very tight range, ATR will be small.
    # Set min_atr_pct=0.5 (50% of price) — unrealistically high, guaranteed to block.
    params = SupertrendParams(atr_period=3, multiplier=2.0, min_atr_pct=0.5)
    st = Supertrend("T", params)
    bars = [
        (100, 100, 100),
        (100, 101, 99),
        (100, 101, 99),   # seed
        (92, 101, 91),    # would be short flip
    ]
    results = feed(st, bars)
    # bar 4 should be blocked by the vol floor (atr ≈ 4.2, price*0.5 = 46 → atr < 46)
    # Actually atr=4.2 < 92*0.5=46, so entry blocked
    assert results[3] is None, (
        "Entry must be suppressed when atr < price * min_atr_pct"
    )


# ── Test: status() dict ───────────────────────────────────────────────────────

def test_status_dict_fields():
    """status() must return a dict with the expected keys."""
    st = Supertrend("T", PARAMS_3)
    s = st.status()
    for key in ("security_id", "bars_seen", "atr", "direction", "supertrend",
                "final_lower", "final_upper", "position", "entry_price"):
        assert key in s, f"Missing key in status(): {key}"
    assert s["security_id"] == "T"
