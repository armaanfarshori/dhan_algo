"""Pure, index-agnostic weekly-expiry derivation (cutover-aware).

WHY THIS EXISTS
---------------
The Dhan rollingoption ingester (``core/dhan_option_history``) must attach an
*expiry date* to every historical IV bar so the condor backtest can join
``option_atm_iv`` by ``(date, expiry)``. The only on-DB expiry source —
``expiry_calendar`` — is **forward-only** (it currently starts 2026-06-23). A real
5-year pull therefore mis-attached 2021–2025 IV rows to far-future 2026 expiries
(the rolling-code rule "k-th calendar expiry ≥ day" picks the nearest *available*
calendar entry, which for any pre-2026 day is a 2026 expiry). The condor then
joined those rows under the wrong key → wrong backtests.

THE FIX
-------
For ANY date (historical or forward) derive the front (and code-N) WEEKLY expiry
*analytically* from the index's expiry-weekday rule instead of trusting the
forward-only calendar. NSE's NIFTY weekly expiry moved Thursday → Tuesday on a
cutover; this module mirrors the rule already encoded by
``research.backtest.fno_condor.IndexConfig`` / ``expiry_weekday_for`` but
**replicated here as a tiny pure function** — ``core/`` must NOT import
``research/*`` (layering violation). The replica is parameterised so it is
index-agnostic: any underlying drives its own weekday + cutover.

The expiry_calendar is then used ONLY as a *refinement* where it actually covers
the date (snap an analytic expiry to a real holiday-adjusted calendar entry within
a few days); it is NEVER allowed to select a far-future entry for a historical day.

Project date convention: this repo runs one year ahead of the real-world calendar
(real-life NSE Tuesday cutover 2025-09-01 is represented as 2026-09-01).

Python ``date.weekday()``: Monday=0 … Sunday=6 → Tuesday=1, Thursday=3.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

# Weekday ints (mirror research/backtest/fno_condor.py; replicated, not imported).
TUESDAY = 1
THURSDAY = 3

# NIFTY weekly-expiry Thursday→Tuesday cutover (project convention: real 2025-09-01
# represented as 2026-09-01). Mirrors fno_condor.NIFTY_TUESDAY_EXPIRY_CUTOVER.
NIFTY_TUESDAY_EXPIRY_CUTOVER = date(2026, 9, 1)

# A weekly front expiry is always within 7 days of the bar date; anything beyond
# this (with slack) signals a mis-attachment (e.g. a forward-only calendar entry
# picked for a historical day) and is flagged/skipped rather than stored.
WEEKLY_EXPIRY_MAX_DAYS = 10


@dataclass(frozen=True)
class ExpiryRule:
    """Index-agnostic weekly-expiry weekday rule (cutover-aware).

    Mirrors the expiry fields of ``research.backtest.fno_condor.IndexConfig``
    (replicated in ``core`` to avoid a research→core import). Defaults to NIFTY:
    Thursday before the cutover, Tuesday on/after it.

    Attributes
    ----------
    expiry_weekday:
        Python weekday int (Mon=0..Sun=6) of the *current* weekly expiry. For
        NIFTY this is the POST-cutover weekday (Tuesday=1).
    pre_cutover_weekday:
        Weekly-expiry weekday BEFORE ``cutover_date`` (NIFTY = Thursday=3).
        ``None`` when the index never changed its expiry weekday.
    cutover_date:
        Date on/after which ``expiry_weekday`` applies and before which
        ``pre_cutover_weekday`` applies. ``None`` when there is no cutover.
    """

    expiry_weekday: int = TUESDAY
    pre_cutover_weekday: Optional[int] = THURSDAY
    cutover_date: Optional[date] = NIFTY_TUESDAY_EXPIRY_CUTOVER


# Convenience default mirroring the NIFTY IndexConfig in fno_condor.
NIFTY_EXPIRY_RULE = ExpiryRule()


def expiry_weekday_for(day: date, rule: ExpiryRule = NIFTY_EXPIRY_RULE) -> int:
    """Weekly-expiry weekday (Python weekday int) effective on ``day``.

    Cutover-aware: when ``rule`` defines a ``cutover_date`` + ``pre_cutover_weekday``
    the pre-cutover weekday applies before the cutover and ``expiry_weekday`` on/
    after it. No cutover → ``expiry_weekday`` unconditionally. This is the pure
    ``core`` replica of ``research.backtest.fno_condor.expiry_weekday_for``.
    """
    if (
        rule.cutover_date is not None
        and rule.pre_cutover_weekday is not None
        and day < rule.cutover_date
    ):
        return rule.pre_cutover_weekday
    return rule.expiry_weekday


def next_weekly_expiry_on_or_after(day: date, rule: ExpiryRule = NIFTY_EXPIRY_RULE) -> date:
    """The next weekly-expiry weekday on/after ``day`` (the front series that day).

    Honours the cutover by evaluating the weekday rule at the RESOLVED EXPIRY DATE,
    not at ``day``. This matters when ``day`` is before the cutover but the next
    expiry lands on/after it: e.g. a bar on Fri 2026-08-28 (pre-cutover → Thursday
    rule) must NOT resolve to Thu 2026-09-03 (a date that is post-cutover, where
    Thursday expiries no longer exist) — the correct front is Tue 2026-09-08.

    Implemented as a small fixed-point: compute a candidate from ``day``'s weekday,
    then if the candidate's OWN weekday rule differs (it crossed the cutover)
    recompute from the candidate. Converges in ≤2 steps (there is a single cutover).
    """
    cur = day
    for _ in range(2):  # ≤1 cutover → at most one recompute; cap defensively
        target_wd = expiry_weekday_for(cur, rule)
        delta = (target_wd - day.weekday()) % 7
        cand = day + timedelta(days=delta)
        # If the candidate's own weekday rule matches the weekday we used, it is
        # self-consistent (did not straddle the cutover) → done.
        if expiry_weekday_for(cand, rule) == target_wd:
            return cand
        # Candidate crossed the cutover: re-evaluate using the candidate's date so
        # the post-cutover weekday is applied. Search from the candidate forward.
        cur = cand
    # Fixed point on the candidate's own weekday (post-cutover side).
    target_wd = expiry_weekday_for(cur, rule)
    delta = (target_wd - day.weekday()) % 7
    return day + timedelta(days=delta)


def nth_weekly_expiry_on_or_after(
    day: date, code: int, rule: ExpiryRule = NIFTY_EXPIRY_RULE
) -> date:
    """The ``code``-th (1-based) weekly expiry on/after ``day``.

    code=1 → the front weekly (``next_weekly_expiry_on_or_after``); code=2 → the
    following week's expiry; etc. Each subsequent code steps a full week from the
    front — except that the *front* week's weekday is re-evaluated per step so a
    code that lands on/after the cutover picks up the new weekday automatically.
    """
    if code < 1:
        raise ValueError(f"expiry code is 1-based (≥1); got {code}")
    exp = next_weekly_expiry_on_or_after(day, rule)
    for _ in range(code - 1):
        # Step to the day after this expiry, then snap to the next weekly weekday
        # (re-evaluating the cutover so a week crossing it flips Thu→Tue).
        exp = next_weekly_expiry_on_or_after(exp + timedelta(days=1), rule)
    return exp


def _refine_with_calendar(
    analytic: date, day: date, calendar: list[date], *, slack_days: int = 4
) -> date:
    """Snap an ``analytic`` expiry to the nearest ``calendar`` entry that actually
    COVERS the bar — i.e. a real (holiday-adjusted) expiry within ``slack_days`` of
    the analytic date. Returns ``analytic`` unchanged when the calendar has no such
    nearby entry (so a forward-only calendar can NEVER pull a historical day to a
    far-future expiry).

    Eligibility: only entries on/after ``day`` (an expiry strictly before the bar
    cannot be that bar's front; in this ingester ``day`` is always a TRADING day —
    bars never fall on a market holiday — so the genuine holiday-rolled expiry, which
    moves BACK to a prior trading day, is still ≥ ``day`` and remains eligible).
    Ties (equidistant entries) break to the EARLIER date deterministically by sorting
    the candidates, so an unsorted ``calendar`` cannot yield nondeterministic picks."""
    if not calendar:
        return analytic
    best: Optional[date] = None
    best_dist = slack_days + 1
    for e in sorted(calendar):  # deterministic tie-break (earlier wins) regardless of input order
        if e < day:
            continue
        dist = abs((e - analytic).days)
        if dist <= slack_days and dist < best_dist:
            best, best_dist = e, dist
    return best if best is not None else analytic


def derive_front_expiry(
    day: date,
    code: int = 1,
    *,
    rule: ExpiryRule = NIFTY_EXPIRY_RULE,
    calendar: Optional[list[date]] = None,
    max_days: int = WEEKLY_EXPIRY_MAX_DAYS,
) -> Optional[date]:
    """Resolve the expiry to ATTACH to a bar dated ``day`` under rolling ``code``.

    1. Compute the analytic ``code``-th weekly expiry ≥ ``day`` from the cutover-
       aware weekday rule (correct for ANY date, historical or forward).
    2. Refine with ``calendar`` ONLY where it actually covers the date (snap to a
       nearby real/holiday-adjusted entry within a few days). A forward-only
       calendar with no nearby entry leaves the analytic date untouched — it can
       NEVER select a far-future expiry for a historical day.
    3. SANITY GUARD: a weekly front is ≤7 days out; if the resolved expiry is more
       than ``max_days`` (~10) from ``day`` something mis-attached → return ``None``
       so the caller SKIPS/flags the row rather than storing a wrong key.

    Returns the expiry ``date`` or ``None`` when the guard trips.
    """
    analytic = nth_weekly_expiry_on_or_after(day, code, rule)
    resolved = _refine_with_calendar(analytic, day, calendar or [])
    # For code>1 the natural span grows ~7 days per code; scale the guard so a
    # legitimate term-structure code is not falsely rejected, but a forward-only
    # mis-attach (years out) still trips it.
    guard = max_days + 7 * (code - 1)
    if (resolved - day).days > guard or resolved < day:
        return None
    return resolved
