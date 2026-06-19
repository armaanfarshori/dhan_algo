"""Opening Gap strategy — unit tests.

All tests drive on_tick() directly (no DB, no IO). Dates are on 2024-06-03
(a Monday). seed_prior_day is called with positional args (prior_high,
prior_low, prior_close) per the standardised engine interface.

Confirm window: 09:15–09:20 (confirm_min=5, default). Gap thresholds:
go_min=1%, fade_min=4% (defaults).
"""
import math
from datetime import datetime, timedelta

import pytest

from strategies.opening_gap import OpeningGap, OpeningGapParams
from strategies.orb import IST

# ── Helpers ───────────────────────────────────────────────────────────────────

SESSION_DATE = 2024, 6, 3   # arbitrary weekday


def ist(hh: int, mm: int, *, date=SESSION_DATE):
    """Return naive IST datetime (matches what tests/test_orb.py uses)."""
    y, mo, d = date
    return datetime(y, mo, d, hh, mm)


def make_strat(params: OpeningGapParams | None = None) -> OpeningGap:
    return OpeningGap("T", params)


def feed_window(strat: OpeningGap, *, open_close: float,
                win_high: float, win_low: float) -> list:
    """Feed the 09:15 bar (open capture) and one more bar to fill the window,
    returning all decisions. Leaves the window built but NOT locked.

    09:15 bar  → open capture + first cw tick
    09:17 bar  → second cw tick (still inside window for confirm_min=5)
    """
    results = []
    results.append(strat.on_tick(ist(9, 15), open_close,
                                 high=win_high, low=win_low))
    results.append(strat.on_tick(ist(9, 17), open_close,
                                 high=win_high, low=win_low))
    return results


# ── Test 1: Missing prior-day seed → inert all session ───────────────────────

def test_missing_seed_is_inert():
    """Strategy must return None for every tick when no seed was provided."""
    strat = make_strat()
    # Do NOT call seed_prior_day
    decisions = []
    # Feed: open capture bar + window + post-window bars including a big move
    decisions.append(strat.on_tick(ist(9, 15), 102.0, high=102.5, low=101.5))
    decisions.append(strat.on_tick(ist(9, 17), 102.0, high=102.5, low=101.5))
    # Post-window — would trigger gap-and-go long if seeded
    decisions.append(strat.on_tick(ist(9, 21), 103.5, high=103.5, low=102.0))
    decisions.append(strat.on_tick(ist(10, 0), 104.0, high=104.0, low=103.0))
    assert all(d is None for d in decisions), decisions
    assert strat.position == 0


# ── Test 2: Gap too small → no trade ─────────────────────────────────────────

def test_gap_too_small_no_trade():
    """Gap < go_min_pct → regime 'none', all ticks return None."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)   # prior_high, prior_low, prior_close
    # 100.5 / 100.0 − 1 = 0.5% < 1% threshold
    strat.on_tick(ist(9, 15), 100.5, high=100.8, low=100.2)
    assert strat._regime == "none"
    # Feed a big breakout — must still be None
    d = strat.on_tick(ist(9, 21), 103.0, high=103.0, low=100.5)
    assert d is None
    d = strat.on_tick(ist(10, 0), 105.0)
    assert d is None
    assert strat.position == 0


# ── Test 3: Gap-and-go LONG (moderate up-gap, holds, breaks out) ─────────────

def test_gap_and_go_long():
    """Moderate up-gap (+2%), holds, breakout above cw_high → ENTER BUY."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)

    # Open capture: close=102 (gap=+2% → "go"), high/low build the window
    # fill_level (up-gap) = 100 + (1-0.5)*(102-100) = 101.0
    # cw_low must be ≥ 101.0 for the hold test to pass
    strat.on_tick(ist(9, 15), 102.0, high=102.5, low=101.5)   # low 101.5 ≥ 101 ✓
    strat.on_tick(ist(9, 17), 102.0, high=102.5, low=101.5)   # cw_high=102.5 cw_low=101.5

    assert strat._regime == "go"
    assert not strat._confirm_locked

    # Post-window bar (09:21 ≥ confirm_end 09:20) with price above cw_high
    d = strat.on_tick(ist(9, 21), 103.0, high=103.0, low=102.5)
    assert d is not None
    assert d.action == "ENTER"
    assert d.side == "BUY"
    assert "gap-and-go LONG" in d.reason

    # Note: after the lock tick, cw_low may have absorbed the 09:21 bar's low if
    # the lock happens on the same tick as the entry.  The stored _stop is what
    # matters for the engine.
    assert math.isclose(d.stop, strat._stop, rel_tol=1e-9)
    expected_risk = 103.0 - d.stop
    expected_target = 103.0 + 1.5 * expected_risk
    assert math.isclose(d.target, expected_target, rel_tol=1e-6)
    assert strat._tried is True


# ── Test 4: Gap-and-go SHORT (moderate down-gap, holds, breaks down) ──────────

def test_gap_and_go_short():
    """Moderate down-gap (−2%), holds, breakdown below cw_low → ENTER SELL."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)

    # Open at 98 → gap = −2% → "go" (down-gap)
    # fill_level (down-gap) = 100 + (1-0.5)*(98-100) = 99.0
    # cw_high must be ≤ 99.0 for hold test to pass
    strat.on_tick(ist(9, 15), 98.0, high=98.5, low=97.5)    # high 98.5 ≤ 99 ✓
    strat.on_tick(ist(9, 17), 98.0, high=98.5, low=97.5)    # cw_high=98.5 cw_low=97.5

    assert strat._regime == "go"

    # Post-window: price < cw_low (97.5) → SELL trigger
    d = strat.on_tick(ist(9, 21), 97.0, high=97.5, low=96.5)
    assert d is not None
    assert d.action == "ENTER"
    assert d.side == "SELL"
    assert "gap-and-go SHORT" in d.reason

    assert math.isclose(d.stop, strat._stop, rel_tol=1e-9)
    expected_risk = d.stop - 97.0
    expected_target = 97.0 - 1.5 * expected_risk
    assert math.isclose(d.target, expected_target, rel_tol=1e-6)
    assert strat._tried is True


# ── Test 5: Gap-and-go hold test fails → no trade ────────────────────────────

def test_gap_and_go_hold_fails_no_trade():
    """Window bar dips below fill_level → hold test fails → regime → 'none'."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)

    # Up-gap +2%, fill_level = 101.0
    # cw_low = 100.5 < 101.0 → hold test fails
    strat.on_tick(ist(9, 15), 102.0, high=102.5, low=101.5)
    strat.on_tick(ist(9, 17), 102.0, high=102.5, low=100.5)  # low 100.5 < 101.0 → FAIL

    assert strat._regime == "go"   # still "go" until window locks
    assert not strat._confirm_locked

    # Lock the window: hold test runs, regime → "none"
    d = strat.on_tick(ist(9, 21), 103.0, high=103.0, low=102.0)
    assert strat._confirm_locked
    assert strat._regime == "none"
    assert d is None

    # Further breakout — still None
    d = strat.on_tick(ist(9, 25), 105.0)
    assert d is None
    assert strat.position == 0


# ── Test 6: Gap-fade SHORT (extreme up-gap → fade toward prior close) ─────────

def test_gap_fade_short():
    """Extreme up-gap (+5% ≥ 4%), price rolls over below cw_low → ENTER SELL."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)

    # Open at 105 → gap = +5% → "fade"
    # cw_high=105.5 cw_low=104.0 (no hold test for fade)
    strat.on_tick(ist(9, 15), 105.0, high=105.5, low=104.0)
    strat.on_tick(ist(9, 17), 105.0, high=105.5, low=104.0)

    assert strat._regime == "fade"

    # Post-window: price < cw_low (104.0) → fade SHORT trigger
    d = strat.on_tick(ist(9, 21), 103.5, high=104.0, low=103.5)
    assert d is not None
    assert d.action == "ENTER"
    assert d.side == "SELL"
    assert "gap-fade SHORT" in d.reason

    # stop = cw_high * (1 + 0.002) ≈ 105.5 * 1.002
    assert math.isclose(d.stop, 105.5 * 1.002, rel_tol=1e-6)
    # raw_target = 100 + (1-0.5)*(105-100) = 100 + 2.5 = 102.5
    assert math.isclose(d.target, 102.5, rel_tol=1e-6)


# ── Test 7: Gap-fade LONG (extreme down-gap → fade toward prior close) ────────

def test_gap_fade_long():
    """Extreme down-gap (−5%), price bounces above cw_high → ENTER BUY."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)

    # Open at 95 → gap = −5% → "fade"
    # cw_low=94.5 cw_high=96.0
    strat.on_tick(ist(9, 15), 95.0, high=96.0, low=94.5)
    strat.on_tick(ist(9, 17), 95.0, high=96.0, low=94.5)

    assert strat._regime == "fade"

    # Post-window: price > cw_high (96.0) → fade LONG trigger
    d = strat.on_tick(ist(9, 21), 96.5, high=96.5, low=95.5)
    assert d is not None
    assert d.action == "ENTER"
    assert d.side == "BUY"
    assert "gap-fade LONG" in d.reason

    # stop = cw_low * (1 - 0.002) ≈ 94.5 * 0.998
    assert math.isclose(d.stop, 94.5 * 0.998, rel_tol=1e-6)
    # raw_target = 100 + (1-0.5)*(95-100) = 100 - 2.5 = 97.5
    assert math.isclose(d.target, 97.5, rel_tol=1e-6)


# ── Test 8: No entry before window locks ──────────────────────────────────────

def test_no_entry_before_window_locks():
    """Price breaks above window high at 09:17 (inside window) → None."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)

    strat.on_tick(ist(9, 15), 102.0, high=102.0, low=101.5)   # open capture, cw starts
    # Inside window (09:17 < 09:20): a price that exceeds what will be cw_high
    d = strat.on_tick(ist(9, 17), 103.0, high=103.0, low=102.0)
    assert d is None          # window not yet locked → no entry
    assert not strat._confirm_locked
    # cw_high still updated
    assert strat._cw_high == 103.0


# ── Test 9: Stop / target exit after entry ────────────────────────────────────

def _setup_long_entry():
    """Return strat with a gap-and-go LONG entry issued and fill notified."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)
    strat.on_tick(ist(9, 15), 102.0, high=102.5, low=101.5)
    strat.on_tick(ist(9, 17), 102.0, high=102.5, low=101.5)
    d = strat.on_tick(ist(9, 21), 103.0, high=103.0, low=102.5)
    assert d is not None and d.action == "ENTER" and d.side == "BUY"
    strat.notify_fill("BUY", 1, 103.0)
    assert strat.position == 1
    return strat, d


def test_target_hit_exit():
    strat, entry = _setup_long_entry()
    target_px = strat._target
    d = strat.on_tick(ist(10, 0), target_px)
    assert d is not None
    assert d.action == "EXIT"
    assert "Target" in d.reason


def test_stop_loss_exit():
    strat, entry = _setup_long_entry()
    stop_px = strat._stop
    d = strat.on_tick(ist(10, 0), stop_px)
    assert d is not None
    assert d.action == "EXIT"
    assert "Stop" in d.reason


# ── Test 10: EOD square-off is unconditional ──────────────────────────────────

def test_eod_squareoff_fires_with_position():
    """EOD square-off must fire regardless of regime and seed."""
    strat, _ = _setup_long_entry()
    # 15:15 ≥ squareoff threshold (15:30 − 15 min = 15:15)
    d = strat.on_tick(ist(15, 16), 104.0)
    assert d is not None
    assert d.action == "EXIT"
    assert "EOD square-off" in d.reason


def test_eod_squareoff_without_seed_if_position_set_directly():
    """EOD must fire even with no seed if a position was injected directly."""
    strat = make_strat()
    # No seed_prior_day, no on_tick — simulate a position reconciled from DB
    strat.position = 1
    strat.entry_price = 100.0
    # First on_tick will trigger session reset (no prior session), then EOD check
    d = strat.on_tick(ist(15, 16), 104.0)
    assert d is not None
    assert d.action == "EXIT"
    assert "EOD square-off" in d.reason


# ── Test 11: Flat at EOD time → None ─────────────────────────────────────────

def test_eod_flat_returns_none():
    """At EOD time with no position, return None (not an EXIT decision)."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)
    strat.on_tick(ist(9, 15), 102.0, high=102.5, low=101.5)
    d = strat.on_tick(ist(15, 16), 102.0)
    assert d is None
    assert strat.position == 0


# ── Test 12: Session reset / no cross-day leakage ────────────────────────────

def test_session_reset_no_leakage():
    """After a full day, reset on new date clears all per-session state."""
    strat, entry = _setup_long_entry()
    assert strat._tried is True

    # Simulate EOD close of day-1 position before crossing into day 2
    strat.notify_flat()
    assert strat.position == 0

    # Seed day 2 and fire a new-date tick
    new_prior_close = 103.5
    strat.seed_prior_day(104.0, 103.0, new_prior_close)

    day2 = (2024, 6, 4)
    # First tick of day 2 triggers reset then open capture
    strat.on_tick(ist(9, 15, date=day2), 105.0, high=105.5, low=104.5)

    # Session state reset
    assert strat._session_date == datetime(2024, 6, 4).date()
    assert strat._open_px == 105.0          # captured on first tick
    assert strat._regime is not None        # classified from open (not None anymore)
    assert strat._tried is False
    assert strat._confirm_locked is False
    # cw_high was updated from the 09:15 bar's high=105.5 (inside confirm window)
    assert strat._cw_high == 105.5
    assert strat._cw_low == 104.5
    # gap_ref must equal the seeded prior close for day 2
    assert math.isclose(strat._gap_ref, new_prior_close, rel_tol=1e-9)
    # Position state NOT touched by reset
    assert strat.position == 0


# ── Test 12b: Reset clears cw_low to inf, not to 0 ──────────────────────────

def test_session_reset_cw_low_is_inf_before_first_tick():
    """On a fresh session before any tick, cw_low == inf."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)
    # Force a session reset without a tick by setting _session_date to yesterday
    from datetime import date
    strat._session_date = date(2024, 6, 2)
    strat._reset_session(date(2024, 6, 3))
    assert strat._cw_high == 0.0
    assert strat._cw_low == float("inf")


# ── Test 13 (optional): Future-skew guard ────────────────────────────────────

def test_future_tick_ignored():
    """A tick stamped > 2 min ahead of wall clock is silently ignored."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)

    # Build a real-time session first
    now = datetime.now(IST).replace(tzinfo=None)
    strat.on_tick(now, 102.0, high=102.5, low=101.5)
    captured_date = strat._session_date
    captured_cw_high = strat._cw_high

    # A tick one hour in the future must be rejected
    future = now + timedelta(hours=1)
    result = strat.on_tick(future, 500.0, high=600.0, low=400.0)
    assert result is None
    # Session date and cw_high unchanged
    assert strat._session_date == captured_date
    assert strat._cw_high == captured_cw_high


# ── Test: Single-attempt (tried flag blocks re-entry) ─────────────────────────

def test_single_attempt_per_session():
    """After one ENTER decision, further triggers return None (tried flag)."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)
    strat.on_tick(ist(9, 15), 102.0, high=102.5, low=101.5)
    strat.on_tick(ist(9, 17), 102.0, high=102.5, low=101.5)

    # First post-window tick triggers entry
    d1 = strat.on_tick(ist(9, 21), 103.0, high=103.0, low=102.5)
    assert d1 is not None and d1.action == "ENTER"
    assert strat._tried is True

    # Second breakout — still flat (fill not called) — no re-entry
    d2 = strat.on_tick(ist(9, 22), 103.5)
    assert d2 is None


# ── Test: Regime classification boundaries ────────────────────────────────────

def test_regime_at_go_min_boundary():
    """A gap exactly at go_min_pct (1.0%) is classified 'go', not 'none'."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)
    # 101.0 / 100.0 − 1 = 1.0% == go_min_pct → should be "go"
    strat.on_tick(ist(9, 15), 101.0, high=101.5, low=100.8)
    assert strat._regime == "go"


def test_regime_at_fade_min_boundary():
    """A gap exactly at fade_min_pct (4.0%) is classified 'fade'."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 100.0)
    # 104.0 / 100.0 − 1 = 4.0% == fade_min_pct → should be "fade"
    strat.on_tick(ist(9, 15), 104.0, high=104.5, low=103.5)
    assert strat._regime == "fade"


# ── Test: notify_flat clears position ─────────────────────────────────────────

def test_notify_flat():
    strat, _ = _setup_long_entry()
    assert strat.position == 1
    strat.notify_flat()
    assert strat.position == 0
    assert strat.entry_price == 0.0
    assert strat._stop == 0.0
    assert strat._target == 0.0


# ── Test: Prior-day with non-positive close is ignored ───────────────────────

def test_seed_nonpositive_close_ignored():
    """A non-positive prior_close leaves the strategy unseeded (inert)."""
    strat = make_strat()
    strat.seed_prior_day(100.0, 99.0, 0.0)    # prior_close=0.0 → ignored
    assert strat._prior_close == 0.0
    d = strat.on_tick(ist(9, 15), 102.0, high=102.5, low=101.5)
    assert d is None
    assert strat._gap_ref == 0.0   # gap_ref stays 0 (unseeded)


# ── Test: params assertion ────────────────────────────────────────────────────

def test_params_assertion_bad_thresholds():
    """go_min_pct >= fade_min_pct must raise AssertionError."""
    with pytest.raises(AssertionError):
        OpeningGapParams(go_min_pct=0.05, fade_min_pct=0.04)
