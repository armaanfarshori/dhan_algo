"""
CPR / Pivot Points — unit tests.

All tests use security_id="T", default CprPivotParams() unless noted.
Bars are driven via on_tick(now, close, high=high, low=low); seed_prior_day
is called in setup where required. No DB, no I/O.

Convenient seed: seed_prior_day(110, 90, 105)
  P = (110+90+105)/3 = 101.6667
  BC = (110+90)/2 = 100
  TC = 2*101.6667 - 100 = 103.3333
  cpr_top = 103.3333, cpr_bottom = 100, cpr_width = 3.3333
  R1 = 2*101.6667 - 90 = 113.3333
  S1 = 2*101.6667 - 110 = 93.3333
  R2 = 101.6667 + 20 = 121.6667
  S2 = 101.6667 - 20 = 81.6667
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from strategies.orb import IST
from strategies.cpr_pivot import CprPivot, CprPivotParams

# ── Helpers ───────────────────────────────────────────────────────────────────

DATE = "2024-06-03"


def ist(hh: int, mm: int, date_str: str = DATE) -> datetime:
    """Return a naive IST datetime for a given date and time."""
    y, mo, d = (int(x) for x in date_str.split("-"))
    return datetime(y, mo, d, hh, mm)


def fresh(params: CprPivotParams | None = None) -> CprPivot:
    return CprPivot("T", params)


def seeded(params: CprPivotParams | None = None) -> CprPivot:
    s = fresh(params)
    s.seed_prior_day(110, 90, 105)
    return s


# ── Expected numeric values from the worked example ───────────────────────────

P_APPROX = pytest.approx(101.6667, abs=0.01)
CPR_TOP_APPROX = pytest.approx(103.3333, abs=0.01)
CPR_BOTTOM_APPROX = pytest.approx(100.0, abs=0.01)
CPR_WIDTH_APPROX = pytest.approx(3.3333, abs=0.01)
R1_APPROX = pytest.approx(113.3333, abs=0.01)
S1_APPROX = pytest.approx(93.3333, abs=0.01)
R2_APPROX = pytest.approx(121.6667, abs=0.01)
S2_APPROX = pytest.approx(81.6667, abs=0.01)


# ── Test 0: Level math sanity check ──────────────────────────────────────────

def test_level_math():
    """Verify the worked numeric example from the spec."""
    s = seeded()
    assert s._P == P_APPROX
    assert s._cpr_top == CPR_TOP_APPROX
    assert s._cpr_bottom == CPR_BOTTOM_APPROX
    assert s._cpr_width == CPR_WIDTH_APPROX
    assert s._R1 == R1_APPROX
    assert s._S1 == S1_APPROX
    assert s._R2 == R2_APPROX
    assert s._S2 == S2_APPROX
    assert s._seeded is True


# ── Test 1: Not seeded → no trade ─────────────────────────────────────────────

def test_not_seeded_returns_none():
    """Without seed_prior_day, every on_tick returns None."""
    s = fresh()
    assert s.on_tick(ist(10, 0), 200, high=201, low=199) is None
    assert s.on_tick(ist(10, 1), 210, high=211, low=209) is None
    assert s.on_tick(ist(10, 2), 50, high=51, low=49) is None


# ── Test 2: Price inside CPR → None ──────────────────────────────────────────

def test_price_inside_cpr_returns_none():
    """Close between cpr_bottom and cpr_top triggers no entry."""
    s = seeded()
    # cpr_bottom=100, cpr_top=103.33; 102 is inside
    d = s.on_tick(ist(10, 0), 102, high=102.5, low=101.5)
    assert d is None


# ── Test 3: Long breakout above CPR top ──────────────────────────────────────

def test_long_breakout_enter():
    """Close > cpr_top → ENTER BUY with correct stop and R1 target."""
    s = seeded()
    d = s.on_tick(ist(10, 0), 104, high=104.2, low=102.5)
    assert d is not None
    assert d.action == "ENTER"
    assert d.side == "BUY"
    assert d.target == R1_APPROX
    # stop = cpr_bottom * (1 - 0.002) = 100 * 0.998 = 99.8
    assert d.stop == pytest.approx(99.8, abs=0.01)
    assert "CPR long breakout" in d.reason
    assert s._long_tried is True


# ── Test 4: Short breakdown below CPR bottom ─────────────────────────────────

def test_short_breakdown_enter():
    """Close < cpr_bottom → ENTER SELL with correct stop and S1 target."""
    s = seeded()
    d = s.on_tick(ist(10, 0), 99, high=100.5, low=98.8)
    assert d is not None
    assert d.action == "ENTER"
    assert d.side == "SELL"
    assert d.target == S1_APPROX
    # stop = cpr_top * (1 + 0.002) = 103.3333 * 1.002 ≈ 103.54
    assert d.stop == pytest.approx(103.3333 * 1.002, abs=0.01)
    assert "CPR short breakdown" in d.reason
    assert s._short_tried is True


# ── Test 5: One attempt per side — no re-entry after long tried ───────────────

def test_no_reentry_long_tried():
    """After _long_tried is set, a subsequent up-cross returns None."""
    s = seeded()
    d1 = s.on_tick(ist(10, 0), 104, high=104.2, low=102.5)
    assert d1 is not None and d1.action == "ENTER"
    # Do NOT fill; long_tried is already set
    assert s._long_tried is True
    d2 = s.on_tick(ist(10, 1), 105, high=105.1, low=104.9)
    assert d2 is None


# ── Test 6: Long target hit → EXIT ────────────────────────────────────────────

def test_long_target_hit():
    """After fill, reaching R1 triggers EXIT with 'Target hit'."""
    s = seeded()
    d = s.on_tick(ist(10, 0), 104, high=104.2, low=102.5)
    assert d is not None and d.action == "ENTER"
    s.notify_fill("BUY", 10, 104.0)
    assert s.position == 10
    assert s._entry_target == R1_APPROX

    # Price reaches R1 (≈ 113.33)
    d2 = s.on_tick(ist(10, 5), 114, high=114.2, low=113.0)
    assert d2 is not None
    assert d2.action == "EXIT"
    assert "Target hit" in d2.reason
    assert "113.33" in d2.reason


# ── Test 7: Long stop hit → EXIT ─────────────────────────────────────────────

def test_long_stop_hit():
    """After fill, price dropping to/below stop_lvl triggers EXIT with 'Stop-loss'."""
    s = seeded()
    s.on_tick(ist(10, 0), 104, high=104.2, low=102.5)
    s.notify_fill("BUY", 10, 104.0)

    # stop = 100 * (1 - 0.002) = 99.8; price 99.5 <= 99.8
    d = s.on_tick(ist(10, 5), 99.5, high=102.0, low=99.4)
    assert d is not None
    assert d.action == "EXIT"
    assert "Stop-loss" in d.reason
    assert "99.80" in d.reason


# ── Test 8: Short target hit → EXIT ──────────────────────────────────────────

def test_short_target_hit():
    """After fill, price reaching S1 triggers EXIT with 'Target hit'."""
    s = seeded()
    d = s.on_tick(ist(10, 0), 99, high=100.5, low=98.8)
    assert d is not None and d.action == "ENTER" and d.side == "SELL"
    s.notify_fill("SELL", 10, 99.0)
    assert s.position == -10
    assert s._entry_target == S1_APPROX

    # Price drops to 93.0 <= S1 ≈ 93.33
    d2 = s.on_tick(ist(10, 5), 93, high=95.0, low=92.8)
    assert d2 is not None
    assert d2.action == "EXIT"
    assert "Target hit" in d2.reason
    assert "93.33" in d2.reason


# ── Test 9a: EOD square-off — seeded + long held ─────────────────────────────

def test_eod_squareoff_seeded_long():
    """At 15:16 IST a long position exits unconditionally."""
    s = seeded()
    s.on_tick(ist(10, 0), 104, high=104.2, low=102.5)
    s.notify_fill("BUY", 10, 104.0)

    d = s.on_tick(ist(15, 16), 104)
    assert d is not None
    assert d.action == "EXIT"
    assert "EOD square-off" in d.reason


# ── Test 9b: EOD square-off — not seeded but position present (DB-reconciled) ─

def test_eod_squareoff_unseeded_with_position():
    """Unconditional: fires even when pivots were never seeded."""
    s = fresh()
    # Simulate a position reconciled from DB without seed
    s.position = 1
    s._session_date = ist(15, 16).date()

    d = s.on_tick(ist(15, 16), 104)
    assert d is not None
    assert d.action == "EXIT"
    assert "EOD square-off" in d.reason


# ── Test 10: Flat at EOD time → None ─────────────────────────────────────────

def test_flat_at_eod_returns_none():
    """Position == 0 at EOD time → None (no spurious EXIT)."""
    s = seeded()
    d = s.on_tick(ist(15, 16), 102)
    assert d is None


# ── Test 11: Bad prior-day data → not seeded → no trade ─────────────────────

def test_bad_prior_day_zeros():
    """seed_prior_day(0, 0, 0) leaves strategy unseeded."""
    s = fresh()
    s.seed_prior_day(0, 0, 0)
    assert s._seeded is False
    # Big breakout bar still returns None
    assert s.on_tick(ist(10, 0), 200, high=201, low=199) is None


def test_bad_prior_day_inverted():
    """seed_prior_day with high < low leaves strategy unseeded."""
    s = fresh()
    s.seed_prior_day(90, 110, 100)  # high < low
    assert s._seeded is False
    assert s.on_tick(ist(10, 0), 200, high=201, low=199) is None


# ── Test 12: Session reset clears pivots (no cross-day leakage) ───────────────

def test_session_reset_clears_pivots():
    """A date change resets all pivot state; the new day has no levels."""
    s = seeded()
    # Take a long on day 1
    d = s.on_tick(ist(10, 0), 104, high=104.2, low=102.5)
    assert d is not None and d.action == "ENTER"
    assert s._seeded is True
    assert s._long_tried is True

    # First bar of day 2 (2024-06-04) with breakout-size move
    day2 = ist(9, 16, "2024-06-04")
    d2 = s.on_tick(day2, 120, high=121, low=119)

    assert s._session_date == day2.date()
    assert s._seeded is False
    assert s._long_tried is False
    assert d2 is None  # no pivots until fresh seed_prior_day


# ── Test 13: Future-skew guard ────────────────────────────────────────────────

def test_future_tick_ignored():
    """A tick > 2 min in the future is ignored; state and seeding are preserved."""
    s = seeded()
    # Capture state before the bad tick
    seeded_before = s._seeded

    future = datetime.now(IST).replace(tzinfo=None) + timedelta(hours=1)
    result = s.on_tick(future, 500, high=600, low=400)

    assert result is None
    # Must NOT have reset session or cleared seeded flag
    assert s._seeded == seeded_before


def test_future_tick_tz_aware():
    """Future-skew guard also handles tz-aware timestamps."""
    s = fresh()
    future = datetime.now(IST) + timedelta(minutes=10)
    assert s.on_tick(future, 200, high=201, low=199) is None
    assert s._seeded is False  # nothing changed


# ── Test 14: target_level="R2S2" retargets ───────────────────────────────────

def test_target_level_r2s2_long():
    """With target_level='R2S2', long breakout targets R2 instead of R1."""
    s = CprPivot("T", CprPivotParams(target_level="R2S2"))
    s.seed_prior_day(110, 90, 105)
    d = s.on_tick(ist(10, 0), 104, high=104.2, low=102.5)
    assert d is not None
    assert d.action == "ENTER" and d.side == "BUY"
    assert d.target == R2_APPROX  # ≈ 121.67


def test_target_level_r2s2_short():
    """With target_level='R2S2', short breakdown targets S2 instead of S1."""
    s = CprPivot("T", CprPivotParams(target_level="R2S2"))
    s.seed_prior_day(110, 90, 105)
    d = s.on_tick(ist(10, 0), 99, high=100.5, low=98.8)
    assert d is not None
    assert d.action == "ENTER" and d.side == "SELL"
    assert d.target == S2_APPROX  # ≈ 81.67


# ── Test 15: notify_flat resets entry_target ─────────────────────────────────

def test_notify_flat_resets_entry_target():
    """notify_flat clears _entry_target."""
    s = seeded()
    d = s.on_tick(ist(10, 0), 104, high=104.2, low=102.5)
    assert d is not None
    s.notify_fill("BUY", 10, 104.0)
    assert s._entry_target != 0.0
    s.notify_flat()
    assert s._entry_target == 0.0
    assert s.position == 0


# ── Test 16: Both sides can trigger in the same session ──────────────────────

def test_both_sides_independent():
    """Long and short are independent; both can trigger once per session."""
    s = seeded()
    # Long first
    d_long = s.on_tick(ist(10, 0), 104, high=104.2, low=102.5)
    assert d_long is not None and d_long.side == "BUY"
    # Do not fill — stay flat

    # Short attempt on a later bar
    d_short = s.on_tick(ist(10, 5), 99, high=100.5, low=98.8)
    assert d_short is not None and d_short.side == "SELL"
    assert s._long_tried is True
    assert s._short_tried is True


# ── Test 17: Short stop hit → EXIT ───────────────────────────────────────────

def test_short_stop_hit():
    """After short fill, price rising to/above stop triggers EXIT with 'Stop-loss'."""
    s = seeded()
    s.on_tick(ist(10, 0), 99, high=100.5, low=98.8)
    s.notify_fill("SELL", 10, 99.0)
    assert s.position == -10

    # stop = cpr_top * (1 + 0.002) = 103.3333 * 1.002 ≈ 103.54
    stop_lvl = 103.3333 * 1.002
    d = s.on_tick(ist(10, 5), stop_lvl + 0.1, high=stop_lvl + 0.5, low=99.5)
    assert d is not None
    assert d.action == "EXIT"
    assert "Stop-loss" in d.reason


# ── Test 18: max_cpr_width_pct filter ────────────────────────────────────────

def test_max_cpr_width_pct_filters_wide_cpr():
    """When cpr_width_pct > max_cpr_width_pct, no entry is taken."""
    # cpr_width_pct = 3.3333 / 101.6667 ≈ 0.0328 = 3.28%
    # Set max_cpr_width_pct to 1% (very tight) → should filter out this day
    s = CprPivot("T", CprPivotParams(max_cpr_width_pct=0.01))
    s.seed_prior_day(110, 90, 105)
    d = s.on_tick(ist(10, 0), 104, high=104.2, low=102.5)
    assert d is None  # filtered by wide-CPR guard


def test_max_cpr_width_pct_disabled():
    """With max_cpr_width_pct=0 (default), no filtering occurs."""
    s = seeded()  # default params, max_cpr_width_pct=0
    d = s.on_tick(ist(10, 0), 104, high=104.2, low=102.5)
    assert d is not None and d.action == "ENTER"


# ── Test 19: Seeded idempotency — re-seeding recomputes levels ───────────────

def test_reseed_recomputes():
    """A second call to seed_prior_day recomputes levels (idempotent within session)."""
    s = fresh()
    s.seed_prior_day(110, 90, 105)
    top_first = s._cpr_top

    # Re-seed with different values
    s.seed_prior_day(120, 80, 100)
    assert s._seeded is True
    assert s._cpr_top != pytest.approx(top_first, abs=0.01)
