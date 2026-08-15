"""Tests for core/expiry.py — cutover-aware, index-agnostic front-expiry derivation.

This is the pure ``core`` replica of the expiry-weekday rule in
``research/backtest/fno_condor.py`` (IndexConfig / expiry_weekday_for). It exists so
``core/dhan_option_history`` can attach a historical bar's expiry ANALYTICALLY (the
k-th weekly weekday ≥ day, holiday-refined where the calendar covers it) instead of
selecting a far-future entry from the forward-only expiry_calendar — the
mis-attachment bug this fix closes.

NO network, NO database, NO date.today()/naive now() (ci-ist-date-trap): every date
is explicit, so the suite is identical on a UTC CI runner and the IST dev Mac.
"""
from datetime import date

import pytest

from core import expiry as ex


# ── weekday rule (cutover-aware) ──────────────────────────────────────────────────
def test_expiry_weekday_pre_cutover_is_thursday():
    # Before the 2025-09-01 cutover NIFTY weeklies expire Thursday (=3).
    assert ex.expiry_weekday_for(date(2021, 3, 10)) == ex.THURSDAY
    assert ex.expiry_weekday_for(date(2025, 8, 31)) == ex.THURSDAY


def test_expiry_weekday_on_or_after_cutover_is_tuesday():
    # On/after the cutover the weekday flips to Tuesday (=1).
    assert ex.expiry_weekday_for(date(2026, 9, 1)) == ex.TUESDAY
    assert ex.expiry_weekday_for(date(2027, 1, 1)) == ex.TUESDAY


def test_expiry_weekday_no_cutover_rule_uses_fixed_weekday():
    rule = ex.ExpiryRule(expiry_weekday=2, pre_cutover_weekday=None, cutover_date=None)
    assert ex.expiry_weekday_for(date(2021, 1, 1), rule) == 2
    assert ex.expiry_weekday_for(date(2030, 1, 1), rule) == 2


# ── next/nth weekly expiry on-or-after ────────────────────────────────────────────
def test_next_weekly_on_day_returns_same_day():
    # 2021-03-11 is a Thursday → front expiry is that very day.
    assert ex.next_weekly_expiry_on_or_after(date(2021, 3, 11)) == date(2021, 3, 11)


def test_next_weekly_rolls_forward_pre_cutover_thursday():
    # Wed 2021-03-10 → next Thursday 2021-03-11.
    assert ex.next_weekly_expiry_on_or_after(date(2021, 3, 10)) == date(2021, 3, 11)
    # Fri 2021-03-12 → next Thursday is the following week 2021-03-18.
    assert ex.next_weekly_expiry_on_or_after(date(2021, 3, 12)) == date(2021, 3, 18)


def test_next_weekly_post_cutover_tuesday():
    # Wed 2026-09-09 → next Tuesday 2026-09-15.
    assert ex.next_weekly_expiry_on_or_after(date(2026, 9, 9)) == date(2026, 9, 15)


def test_nth_weekly_steps_a_week_per_code():
    d = date(2021, 3, 10)  # Wed
    assert ex.nth_weekly_expiry_on_or_after(d, 1) == date(2021, 3, 11)
    assert ex.nth_weekly_expiry_on_or_after(d, 2) == date(2021, 3, 18)
    assert ex.nth_weekly_expiry_on_or_after(d, 3) == date(2021, 3, 25)


def test_nth_weekly_rejects_code_below_one():
    with pytest.raises(ValueError, match="1-based"):
        ex.nth_weekly_expiry_on_or_after(date(2021, 3, 10), 0)


def test_nth_weekly_crossing_cutover_flips_weekday():
    # Around the cutover, a code that lands on/after 2025-09-01 picks up Tuesday.
    # 2025-08-26 (Tue, pre-cutover) → code1 Thursday 8/28, later codes cross to Tuesdays.
    d = date(2025, 8, 26)
    c1 = ex.nth_weekly_expiry_on_or_after(d, 1)
    assert c1 == date(2025, 8, 28) and c1.weekday() == ex.THURSDAY  # pre-cutover Thu
    # A code well past the cutover must be a Tuesday.
    c5 = ex.nth_weekly_expiry_on_or_after(d, 5)
    assert c5.weekday() == ex.TUESDAY
    assert c5 >= ex.NIFTY_TUESDAY_EXPIRY_CUTOVER


# ── derive_front_expiry: analytic + calendar refinement + guard ───────────────────
def test_derive_front_historical_pre_cutover():
    assert ex.derive_front_expiry(date(2021, 3, 10), 1) == date(2021, 3, 11)


def test_derive_front_post_cutover():
    assert ex.derive_front_expiry(date(2026, 9, 10), 1) == date(2026, 9, 15)


def test_derive_front_calendar_refines_to_holiday_adjusted():
    # Analytic front 6/18 (Thu) snaps to a real 6/17 calendar entry within slack.
    assert ex.derive_front_expiry(
        date(2026, 6, 15), 1, calendar=[date(2026, 6, 17)]
    ) == date(2026, 6, 17)


def test_derive_front_forward_only_calendar_does_not_pull_historical():
    # THE BUG: a forward-only calendar (years out) must NOT pull a 2021 day forward.
    exp = ex.derive_front_expiry(
        date(2021, 3, 10), 1, calendar=[date(2026, 6, 23), date(2026, 6, 30)]
    )
    assert exp == date(2021, 3, 11)  # analytic 2021 Thursday, calendar ignored


def test_derive_front_calendar_only_refines_when_within_slack():
    # A calendar entry far from the analytic date is NOT used (kept analytic).
    # 2026-06-15 is post-cutover → analytic front is Tuesday 2026-06-16.
    assert ex.derive_front_expiry(
        date(2026, 6, 15), 1, calendar=[date(2026, 7, 30)]
    ) == date(2026, 6, 16)


def test_derive_front_guard_returns_none_for_implausible_gap():
    # If a (bad) refinement landed >max_days from the bar, the guard skips it. We
    # force it with a tiny max_days so even the legit ≤7-day front trips the guard.
    assert ex.derive_front_expiry(date(2021, 3, 10), 1, max_days=0) is None


def test_refine_with_calendar_rejects_far_future_entry():
    # THE BUG, at the refinement level: a 2021 analytic with ONLY a far-future 2026
    # calendar entry must keep the analytic (the far entry is >slack_days away).
    analytic = ex.next_weekly_expiry_on_or_after(date(2021, 3, 10))  # 2021-03-11
    out = ex._refine_with_calendar(analytic, date(2021, 3, 10), [date(2026, 6, 23)])
    assert out == analytic  # far entry ignored, analytic kept


def test_derive_front_forward_only_calendar_keeps_realistic_max_days():
    # End-to-end: a 2021 bar with a forward-only 2026 calendar resolves to the 2021
    # analytic (NOT None, NOT 2026) under the REAL guard (default max_days=10) — the
    # far calendar entry is neither selected (slack) nor able to trip the guard.
    exp = ex.derive_front_expiry(date(2021, 3, 10), 1, calendar=[date(2026, 6, 23)])
    assert exp == date(2021, 3, 11)


def test_next_weekly_crossing_cutover_resolves_to_post_cutover_weekday():
    # REGRESSION (QA find): a pre-cutover bar whose next expiry lands ON/AFTER the
    # cutover must resolve to the POST-cutover weekday (Tuesday), NOT a Thursday that
    # no longer exists. Fri 2026-08-28 → Tue 2026-09-01, not Thu 2026-09-03.
    exp = ex.next_weekly_expiry_on_or_after(date(2026, 8, 28))
    assert exp == date(2026, 9, 1)
    assert exp.weekday() == ex.TUESDAY


def test_nth_weekly_strictly_increasing_correct_weekday_through_cutover():
    # REGRESSION: codes spanning the cutover are strictly increasing AND each lands
    # on the weekday its OWN date prescribes (Thursday pre, Tuesday post).
    d = date(2025, 8, 28)  # last pre-cutover Thursday
    seq = [ex.nth_weekly_expiry_on_or_after(d, k) for k in range(1, 5)]
    assert seq == [date(2025, 8, 28), date(2025, 9, 2), date(2025, 9, 9), date(2025, 9, 16)]
    for e in seq:
        assert e.weekday() == ex.expiry_weekday_for(e)
    assert all(b > a for a, b in zip(seq, seq[1:]))


def test_derive_front_guard_scales_with_code():
    # Term-structure codes legitimately span ~7 days each; the guard scales so they
    # are not falsely rejected.
    assert ex.derive_front_expiry(date(2021, 3, 10), 3) == date(2021, 3, 25)


def test_derive_front_index_agnostic_custom_weekday_no_cutover():
    rule = ex.ExpiryRule(expiry_weekday=ex.TUESDAY, pre_cutover_weekday=None, cutover_date=None)
    exp = ex.derive_front_expiry(date(2021, 3, 10), 1, rule=rule)  # Wed
    assert exp == date(2021, 3, 16)  # next Tuesday
    assert exp.weekday() == ex.TUESDAY
