"""
EMA Crossover strategy — unit tests.

All tests drive on_tick() with synthetic bars (no DB / engine needed).
Default params for most tests: n_fast=3, n_slow=5, min_separation_pct=0.0,
entry_start_min=0, stop_mode="pct", stop_pct=0.05.

EMA recurrence reminder:
  k_fast = 2/(3+1) = 0.5,  k_slow = 2/(5+1) = 1/3
  SMA seed = average of first n closes.
"""

from datetime import datetime

from strategies.ema_crossover import EmaCrossover, EmaCrossoverParams

# ── helpers ──────────────────────────────────────────────────────────────────

DAY1 = datetime(2024, 3, 1)
DAY2 = datetime(2024, 3, 4)


def ist(day: datetime, hh: int, mm: int) -> datetime:
    """Naive IST timestamp on the given day."""
    return day.replace(hour=hh, minute=mm, second=0, microsecond=0)


def default_params(**overrides) -> EmaCrossoverParams:
    """Convenience: pct stop, no separation filter, no open delay."""
    base = dict(
        n_fast=3,
        n_slow=5,
        stop_mode="pct",
        stop_pct=0.05,
        min_separation_pct=0.0,
        entry_start_min=0,
        squareoff_before_close_min=15,
        far_target_mult=0.50,
    )
    base.update(overrides)
    return EmaCrossoverParams(**base)


def feed_bars(strategy: EmaCrossover, bars, day: datetime = DAY1, start_min: int = 16):
    """
    Feed a list of (close,) or (close, high, low) tuples and return all Decisions.
    Timestamps start at HH:start_min, one minute apart.
    """
    results = []
    for i, bar in enumerate(bars):
        if isinstance(bar, (int, float)):
            close, high, low = bar, bar, bar
        else:
            close, high, low = bar[0], bar[1], bar[2]
        ts = ist(day, start_min // 60 + 9, start_min % 60 + i)
        d = strategy.on_tick(ts, close, high=high, low=low)
        results.append(d)
    return results


# ── T1: warm-up returns None ──────────────────────────────────────────────────

def test_t1_warmup_returns_none():
    """4 closes: slow EMA (n=5) not yet seeded → every on_tick returns None."""
    s = EmaCrossover("TEST", default_params())
    closes = [100, 101, 102, 103]
    results = feed_bars(s, closes)
    assert all(d is None for d in results), f"Expected all None, got {results}"


# ── T2: first seeded bar emits no cross ───────────────────────────────────────

def test_t2_first_seeded_bar_no_cross():
    """
    5 closes: bar 5 is the first bar where both EMAs are seeded.
    fast seeds at bar 3, slow seeds at bar 5.
    On bar 5: record prev_* for the first time → return None (no prior relationship).
    """
    s = EmaCrossover("TEST", default_params())
    closes = [100, 101, 102, 103, 104]
    results = feed_bars(s, closes)
    # All 5 bars return None: bars 1-4 are warm-up; bar 5 is first-seeded-bar
    assert all(d is None for d in results), f"Expected all None, got {results}"
    # Verify internal prev state is set after bar 5
    assert s._prev_fast is not None
    assert s._prev_slow is not None


# ── T3: bullish cross → ENTER BUY ─────────────────────────────────────────────

def test_t3_bullish_cross_enter_buy():
    """
    Sequence 100,99,98,97,96,110:
      bar 3: fast seeds = (100+99+98)/3 = 99.0
      bar 4: fast = 97*0.5 + 99*0.5 = 98.0
      bar 5: fast = 96*0.5 + 98*0.5 = 97.0, slow seeds = (100+99+98+97+96)/5 = 98.0
      bar 5: first seeded bar → prev_fast=97.0, prev_slow=98.0, return None
      bar 6: fast = 110*0.5 + 97*0.5 = 103.5, slow = 110/3 + 98*2/3 = 102.0
      bull cross: prev_fast(97.0) <= prev_slow(98.0) AND fast(103.5) > slow(102.0) ✓
    """
    s = EmaCrossover("TEST", default_params())
    bars = [100, 99, 98, 97, 96, 110]
    results = feed_bars(s, bars)

    assert results[5] is not None, "Bar 6 should produce a Decision"
    d = results[5]
    assert d.action == "ENTER"
    assert d.side == "BUY"
    assert "cross-up" in d.reason
    # stop = 110 * (1 - 0.05) = 104.5
    assert abs(d.stop - 104.5) < 1e-9, f"Expected stop=104.5, got {d.stop}"
    # target = 110 * 1.5 = 165.0
    assert abs(d.target - 165.0) < 1e-9, f"Expected target=165.0, got {d.target}"
    # Contract: stop < close < target
    assert d.stop < 110 < d.target
    # Bars 1-5 must all be None
    assert all(d is None for d in results[:5])


# ── T4: bearish cross → ENTER SELL ───────────────────────────────────────────

def test_t4_bearish_cross_enter_sell():
    """
    Sequence 96,97,98,99,100,80:
      bar 3: fast seeds = (96+97+98)/3 = 97.0
      bar 4: fast = 99*0.5 + 97*0.5 = 98.0
      bar 5: fast = 100*0.5 + 98*0.5 = 99.0, slow seeds = (96+97+98+99+100)/5 = 98.0
      bar 5: prev_fast=99.0, prev_slow=98.0, return None
      bar 6: fast = 80*0.5 + 99*0.5 = 89.5, slow = 80/3 + 98*2/3 ≈ 92.0
      bear cross: prev_fast(99.0) >= prev_slow(98.0) AND fast(89.5) < slow(92.0) ✓
    """
    s = EmaCrossover("TEST", default_params())
    bars = [96, 97, 98, 99, 100, 80]
    results = feed_bars(s, bars)

    d = results[5]
    assert d is not None
    assert d.action == "ENTER"
    assert d.side == "SELL"
    assert "cross-down" in d.reason
    # stop = 80 * (1 + 0.05) = 84.0
    assert abs(d.stop - 84.0) < 1e-9, f"Expected stop=84.0, got {d.stop}"
    # target = max(80 * 0.5, 0.05) = 40.0
    assert abs(d.target - 40.0) < 1e-9, f"Expected target=40.0, got {d.target}"
    # Contract: target < close < stop
    assert d.target < 80 < d.stop
    assert all(r is None for r in results[:5])


# ── T5: opposite cross exits open long ───────────────────────────────────────

def test_t5_opposite_cross_exits_long():
    """
    Run T3 sequence to get BUY ENTER, notify_fill, then feed a down-move to produce
    a bearish cross → expect EXIT, NOT a new SELL ENTER on the same bar.
    """
    s = EmaCrossover("TEST", default_params())
    # Seed up to the bull cross (bar 6 = index 5)
    bars_phase1 = [100, 99, 98, 97, 96, 110]
    results1 = feed_bars(s, bars_phase1)
    assert results1[5] is not None and results1[5].action == "ENTER"

    # Simulate engine confirming the fill
    s.notify_fill("BUY", 1, 110)
    assert s.position == 1

    # State after bar 6: fast=103.5, slow=102.0, prev_fast=103.5, prev_slow=102.0
    # Feed bar 7: close=80 → fast=80*0.5+103.5*0.5=91.75, slow=80/3+102*2/3≈94.67
    # bear cross: prev_fast(103.5) >= prev_slow(102.0) AND fast(91.75) < slow(94.67) ✓
    d = s.on_tick(ist(DAY1, 9, 22), 80, high=80, low=80)
    assert d is not None
    assert d.action == "EXIT"
    assert "cross-down" in d.reason
    # Must NOT be an ENTER (no flip-in same bar)
    assert d.action != "ENTER"


# ── T6: protective stop on close exits a long ─────────────────────────────────

def test_t6_stop_loss_on_close_exits_long():
    """
    After BUY at close=110 (stop=104.5), feed a bar with close=104.0 where no bear
    cross has yet formed → stop-loss exit fires.
    """
    s = EmaCrossover("TEST", default_params())
    # Build to bull cross at bar 6 (close=110)
    for i, close in enumerate([100, 99, 98, 97, 96]):
        s.on_tick(ist(DAY1, 9, 16 + i), close, high=close, low=close)
    d_enter = s.on_tick(ist(DAY1, 9, 21), 110, high=110, low=110)
    assert d_enter is not None and d_enter.action == "ENTER"
    s.notify_fill("BUY", 1, 110)

    # State: fast=103.5, slow=102.0, prev_fast=103.5, prev_slow=102.0
    # bar 7: close=104.0
    # fast = 104*0.5 + 103.5*0.5 = 103.75, slow = 104/3 + 102*2/3 ≈ 102.67
    # bear cross: prev_fast(103.5) >= prev_slow(102.0) AND fast(103.75) < slow(102.67)? NO
    # stop breach: 104.0 <= 104.5 ✓
    d = s.on_tick(ist(DAY1, 9, 22), 104.0, high=104.0, low=104.0)
    assert d is not None
    assert d.action == "EXIT"
    assert "Stop-loss" in d.reason
    assert "104.50" in d.reason


# ── T7: EOD square-off is unconditional ──────────────────────────────────────

def test_t7_eod_squareoff_unconditional_with_position():
    """
    With open position and EMAs not seeded, tick at 15:16 (≥ 15:30 - 15 min)
    → unconditional EXIT regardless of indicator state.
    """
    s = EmaCrossover("TEST", default_params())
    # Manually set position (bypassing EMA seeding)
    s.position = 1
    s.entry_price = 100.0
    s._session_date = DAY1.date()

    d = s.on_tick(ist(DAY1, 15, 16), 105, high=105, low=105)
    assert d is not None
    assert d.action == "EXIT"
    assert "EOD" in d.reason


def test_t7_eod_squareoff_flat_returns_none():
    """Flat position at EOD time → None."""
    s = EmaCrossover("TEST", default_params())
    s._session_date = DAY1.date()

    d = s.on_tick(ist(DAY1, 15, 16), 105, high=105, low=105)
    assert d is None


def test_t7_eod_dominates_before_emaseeded():
    """EOD fires even when EMAs have never been seeded (no indicators needed)."""
    s = EmaCrossover("TEST", default_params())
    s.position = 1
    # No bars fed — EMAs fully cold
    s._session_date = DAY1.date()
    d = s.on_tick(ist(DAY1, 15, 20), 100, high=100, low=100)
    assert d is not None and d.action == "EXIT" and "EOD" in d.reason


# ── T8: session reset clears EMAs across a date change ────────────────────────

def test_t8_session_reset_clears_emas():
    """
    Seed EMAs fully on day 1 (6 bars reaching a bull cross). On day 2 at 09:16,
    must return None (warm-up again) — proves _reset_session wiped EMA state.
    """
    s = EmaCrossover("TEST", default_params())
    # Day 1: feed the full T3 sequence to reach warm state (fast=103.5, slow=102.0)
    for i, close in enumerate([100, 99, 98, 97, 96, 110]):
        s.on_tick(ist(DAY1, 9, 16 + i), close, high=close, low=close)
    assert s._fast_value is not None and s._slow_value is not None

    # Day 2: first tick must trigger _reset_session
    d = s.on_tick(ist(DAY2, 9, 16), 100, high=100, low=100)
    assert d is None, "After session reset, first tick should be in warm-up → None"
    # Internal state cleared
    assert s._session_date == DAY2.date()
    assert s._fast_count == 1  # first bar of new session
    assert s._fast_value is None
    assert s._slow_value is None
    assert s._prev_fast is None
    assert s._prev_slow is None


def test_t8_day2_reproduces_day1_first_cross_behavior():
    """
    Feeding the same 6-bar bull-cross sequence on day 2 reproduces the day-1
    result — no state leakage across sessions.
    """
    s = EmaCrossover("TEST", default_params())
    # Day 1
    for i, close in enumerate([100, 99, 98, 97, 96, 110]):
        s.on_tick(ist(DAY1, 9, 16 + i), close, high=close, low=close)
    s.notify_flat()  # ensure flat

    # Day 2 — same sequence
    results_d2 = []
    for i, close in enumerate([100, 99, 98, 97, 96, 110]):
        d = s.on_tick(ist(DAY2, 9, 16 + i), close, high=close, low=close)
        results_d2.append(d)

    # Same as day 1: bars 0-4 None, bar 5 ENTER BUY
    assert all(r is None for r in results_d2[:5])
    assert results_d2[5] is not None and results_d2[5].action == "ENTER"
    assert results_d2[5].side == "BUY"


# ── T9: min_separation filter skips marginal cross ───────────────────────────

def test_t9_separation_filter_skips_marginal_cross():
    """
    min_separation_pct=0.01 (1%). Cross where abs(fast-slow) < price*0.01 → None.
    prev_* still advances so the next same-direction bar does NOT re-fire the cross.
    """
    s = EmaCrossover("TEST", default_params(min_separation_pct=0.01))

    # Feed 5 identical closes (100) → fast=100, slow=100; both seeded; prev set
    for i in range(5):
        s.on_tick(ist(DAY1, 9, 16 + i), 100, high=100, low=100)
    assert s._prev_fast is not None

    # bar 6: close=100.3 — tiny bull cross, gap ≈ 0.05 < 100.3*0.01 = 1.003 → filtered
    d6 = s.on_tick(ist(DAY1, 9, 21), 100.3, high=100.3, low=100.3)
    assert d6 is None, "Marginal cross should be filtered by min_separation_pct"

    # bar 7: close=100.3 again — prev_fast > prev_slow now (consumed), no bull cross
    d7 = s.on_tick(ist(DAY1, 9, 22), 100.3, high=100.3, low=100.3)
    assert d7 is None, "Cross should not re-fire after prev_* advanced past it"


# ── T10: entry_start delay ────────────────────────────────────────────────────

def test_t10_entry_start_delay_blocks_early_cross():
    """
    entry_start_min=15: cross at 09:20 (5 min after open) is blocked and consumed.
    A subsequent cross at 09:32 fires normally.
    """
    s = EmaCrossover("TEST", default_params(entry_start_min=15))

    # Feed T3 sequence with timestamps so the bull cross lands at 09:20.
    # bars 1-5 at 09:15..09:19, bar 6 (the bull cross) at 09:20.
    # entry_start = 09:15 + 15min = 09:30, so the 09:20 cross must be BLOCKED.
    times = [(9, 15), (9, 16), (9, 17), (9, 18), (9, 19), (9, 20)]
    bars = [100, 99, 98, 97, 96, 110]
    results = [s.on_tick(ist(DAY1, hh, mm), close, high=close, low=close)
               for (hh, mm), close in zip(times, bars)]

    # The bull cross fires on bar 6 (09:20) but entry_start=09:30 → blocked → None.
    assert results[5] is None, "Cross before entry_start should be blocked"

    # State after bar6: fast=103.5, slow=102.0 (prev advanced to these).
    # For a NEW bull cross after 09:30 we drive fast below slow then back above.
    # bar7 @ 09:21: close=90 → fast=90*0.5+103.5*0.5=96.75, slow=90/3+102*2/3=98
    #   bear cross would be a SELL ENTER, but entry_start (09:30) still blocks it.
    d7 = s.on_tick(ist(DAY1, 9, 21), 90, high=90, low=90)
    assert d7 is None, "SELL entry at 09:21 also blocked by entry_start"

    # bar8 @ 09:22: close=110 → fast=110*0.5+96.75*0.5=103.375, slow=110/3+98*2/3=102.0
    #   prev after bar7: fast=96.75, slow=98.0 → prev_fast < prev_slow
    #   bull cross fires, but 09:22 < 09:30 → still blocked.
    d8 = s.on_tick(ist(DAY1, 9, 22), 110, high=110, low=110)
    assert d8 is None, "Cross at 09:22 still before entry_start (09:30)"

    # State after bar8: fast≈103.375, slow≈102.0, prev advanced.
    # Drive a bear cross at 09:30 (entry_start reached). Flat → this is a SELL
    # ENTER (09:30 >= 09:30). Capture it, then flatten so we can test a bull
    # cross next.
    # bar9 @ 09:30: close=90 → fast=90*0.5+103.375*0.5≈96.6875,
    #   slow=90/3+102*2/3≈98.0 → bear cross → SELL ENTER (time now allowed).
    d9 = s.on_tick(ist(DAY1, 9, 30), 90, high=90, low=90)
    assert d9 is not None and d9.action == "ENTER" and d9.side == "SELL", (
        f"Bear cross at 09:30 (== entry_start) should fire a SELL ENTER, got {d9}"
    )
    # Flatten the simulated position so a subsequent bull cross can ENTER long.
    s.notify_flat()

    # bar10 @ 09:32: close=130 → fast jumps above slow → bull cross while flat.
    #   POSITIVE assertion: a real cross AFTER entry_start is NOT time-blocked.
    d10 = s.on_tick(ist(DAY1, 9, 32), 130, high=130, low=130)
    assert d10 is not None and d10.action == "ENTER" and d10.side == "BUY", (
        f"Bull cross at 09:32 (after entry_start) must fire a BUY ENTER, got {d10}"
    )


def test_t10_entry_after_delay_fires():
    """
    Minimal entry-delay test: with entry_start_min=0, the cross fires immediately.
    Switch to entry_start_min=5 and verify bar at 09:19 (4 min after open) is blocked.
    """
    # First confirm that with entry_start_min=0, the T3 cross fires at 09:21
    s0 = EmaCrossover("TEST", default_params(entry_start_min=0))
    bars = [100, 99, 98, 97, 96, 110]
    times = [(9, 16), (9, 17), (9, 18), (9, 19), (9, 20), (9, 21)]
    for (hh, mm), close in zip(times, bars):
        d = s0.on_tick(ist(DAY1, hh, mm), close, high=close, low=close)
    assert d is not None and d.action == "ENTER", "Cross should fire with entry_start_min=0"

    # With entry_start_min=5, the cross at 09:20 (5 min after open, i.e. at 09:20 exactly)
    # entry_start = 09:15 + 5 = 09:20 → 09:20 >= 09:20 means it IS allowed
    # Move to 09:19 (4 min after open) to verify blocking
    # Re-run with bar 6 cross at 09:19 instead of 09:21
    s5 = EmaCrossover("TEST", default_params(entry_start_min=5))
    times5 = [(9, 14), (9, 15), (9, 16), (9, 17), (9, 18), (9, 19)]
    for (hh, mm), close in zip(times5, bars):
        d5 = s5.on_tick(ist(DAY1, hh, mm), close, high=close, low=close)
    # 09:19 < 09:20 (entry_start) → should be blocked
    assert d5 is None, "Cross at 09:19 with entry_start_min=5 should be blocked"


# ── T11 (optional): swing-stop exact value ────────────────────────────────────

def test_t11_swing_stop_exact_value():
    """
    stop_mode='swing', swing_lookback=3, sl_buffer_pct=0.002.
    Feed bars with known lows. At the bull cross, verify stop = min(last 3 lows) * 0.998.

    Sequence: (close=100,h=101,l=99), (99,h=100,l=98), (98,h=99,l=97),
              (97,h=98,l=96), (96,h=97,l=95), (110,h=111,l=109)

    swing deque at bar 6 (after _push_swing for bar 6, before EMA returns):
      After bar1: lows=[99]
      After bar2: lows=[99,98]
      After bar3: lows=[99,98,97]   (full, lookback=3)
      After bar4: lows=[98,97,96]   (popleft 99)
      After bar5: lows=[97,96,95]   (popleft 98)
      After bar6: lows=[96,95,109]  (popleft 97; bar6 low=109)
    min(96, 95, 109) = 95
    stop = 95 * (1 - 0.002) = 94.81
    """
    p = EmaCrossoverParams(
        n_fast=3,
        n_slow=5,
        stop_mode="swing",
        swing_lookback=3,
        sl_buffer_pct=0.002,
        min_separation_pct=0.0,
        entry_start_min=0,
    )
    s = EmaCrossover("TEST", p)

    bars = [
        (100, 101, 99),
        (99, 100, 98),
        (98, 99, 97),
        (97, 98, 96),
        (96, 97, 95),
        (110, 111, 109),
    ]
    results = feed_bars(s, bars)

    d = results[5]
    assert d is not None and d.action == "ENTER" and d.side == "BUY"
    expected_stop = 95 * (1 - 0.002)  # = 94.81
    assert abs(d.stop - expected_stop) < 1e-9, (
        f"Expected swing stop {expected_stop:.4f}, got {d.stop:.4f}"
    )


# ── Additional edge-case tests ─────────────────────────────────────────────────

def test_price_zero_ignored():
    """price <= 0 must return None without touching any state."""
    s = EmaCrossover("TEST", default_params())
    d = s.on_tick(ist(DAY1, 10, 0), 0, high=0, low=0)
    assert d is None


def test_notify_flat_resets_position():
    """notify_flat sets position=0 and entry_price=0."""
    s = EmaCrossover("TEST", default_params())
    s.notify_fill("BUY", 1, 100)
    assert s.position == 1
    s.notify_flat()
    assert s.position == 0
    assert s.entry_price == 0.0


def test_notify_fill_tracks_position():
    """notify_fill correctly tracks cumulative position and entry_price."""
    s = EmaCrossover("TEST", default_params())
    s.notify_fill("BUY", 5, 200.0)
    assert s.position == 5
    assert s.entry_price == 200.0
    s.notify_fill("SELL", 5, 210.0)
    assert s.position == 0
    assert s.entry_price == 0.0


def test_short_stop_breach_exits_short():
    """Stop-loss on close for a short position: close >= stop_level → EXIT."""
    s = EmaCrossover("TEST", default_params())
    # Build to bear cross (T4 sequence) and enter SELL
    bars = [96, 97, 98, 99, 100, 80]
    for i, close in enumerate(bars):
        d = s.on_tick(ist(DAY1, 9, 16 + i), close, high=close, low=close)
    assert d is not None and d.action == "ENTER" and d.side == "SELL"
    s.notify_fill("SELL", 1, 80)

    # Feed a bar at 85 (above stop) without forming a bull cross
    # After bar6: fast=89.5, slow=92.0, prev=(89.5, 92.0)
    # bar7 close=85: fast=85*0.5+89.5*0.5=87.25, slow=85/3+92*2/3≈89.67
    # bull cross: prev_fast(89.5) <= prev_slow(92.0) AND fast(87.25) > slow(89.67)? NO
    # stop breach: 85 >= 84.0 ✓
    d_exit = s.on_tick(ist(DAY1, 9, 22), 85, high=85, low=85)
    assert d_exit is not None
    assert d_exit.action == "EXIT"
    assert "Stop-loss" in d_exit.reason


def test_no_enter_while_position_open():
    """While long, an opposite exit fires but no re-enter on same bar."""
    s = EmaCrossover("TEST", default_params())
    bars = [100, 99, 98, 97, 96, 110]
    for i, close in enumerate(bars):
        d = s.on_tick(ist(DAY1, 9, 16 + i), close, high=close, low=close)

    s.notify_fill("BUY", 1, 110)

    # Feed a bar that produces a bearish cross — should EXIT, not SELL ENTER
    d = s.on_tick(ist(DAY1, 9, 22), 80, high=80, low=80)
    assert d is not None
    assert d.action == "EXIT"
    # Definitely not entering a new position in the same tick
    assert d.side == ""  # EXIT decisions have no side


def test_decision_is_reused_from_orb():
    """EmaCrossoverParams exists with exact class name; Decision imported in ema_crossover is same object."""
    import strategies.ema_crossover as ema_mod
    from strategies.orb import Decision as OrbDecision
    # The module should use the same Decision class (imported from orb, not redefined)
    assert ema_mod.Decision is OrbDecision
