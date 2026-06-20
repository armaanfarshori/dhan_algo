"""
Market-session abstraction — decouples the platform from hardcoded equity hours.

The equity engine had 09:15–15:30 IST (and the 09:00–15:35 poll window) duplicated
across ~13 files. That hardcoding is the #1 blocker for a commodities (MCX) track,
whose evening session runs to ~23:30/23:55 IST — a runner gated on 15:35 would go
silent exactly when MCX is hot.

This module exposes named `MarketSession` profiles and a `SessionClock` of helpers
(`is_open`, `in_poll_window`, `squareoff_time`, `session_date`). The EQUITY profile
reproduces the legacy constants *byte-identically* so the existing engine behaves
exactly as before; the MCX profile is added but not yet wired to a live runner.

MCX subtlety: the evening close flips with US Daylight Saving Time (India observes
none). When New York is on DST the close is 23:30 IST; on US standard time it is
23:55 IST. We derive the DST state from `zoneinfo` for the *given date* rather than
hardcode a season — so it is always correct, including the changeover weeks.

All wall-clock reads use `datetime.now(Asia/Kolkata)` to avoid the CI IST-date trap
(naive `now()`/`date.today()` pass on the IST dev Mac but flip the session date
under UTC CI).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_NY = ZoneInfo("America/New_York")


def _ny_is_dst(on: date) -> bool:
    """Is New York observing Daylight Saving Time on this date?

    Sampled at local noon to stay clear of the 02:00 spring-forward / fall-back
    transition instants. India has no DST, so this is the only thing that moves
    the MCX evening close.
    """
    dt = datetime(on.year, on.month, on.day, 12, 0, tzinfo=_NY)
    return bool(dt.dst())


@dataclass(frozen=True)
class MarketSession:
    """A named trading session profile.

    open_time / close_time      — the tradable window (IST).
    poll_open / poll_close      — the (wider) window in which a runner should poll
                                  (pre/post margin around the tradable window).
    squareoff_before_close_min  — minutes before close to flatten intraday.
    weekdays                    — set of weekday ints (Mon=0 … Sun=6) the session runs.
    dst_close_time              — alternate close for when New York is on US standard
                                  time (DST off). None means the close never flips.
    poll_close_dst_off          — alternate poll-close for US-standard time.

    For a fixed-close session (EQUITY) `close_time` is used unconditionally. For a
    DST-sensitive session (MCX) `close_time` is the US-DST close and
    `dst_close_time` is the US-standard close; `close_time_for(date)` resolves it.
    """

    name: str
    open_time: dtime
    close_time: dtime
    poll_open: dtime
    poll_close: dtime
    squareoff_before_close_min: int
    weekdays: frozenset[int] = frozenset({0, 1, 2, 3, 4})
    dst_close_time: Optional[dtime] = None
    poll_close_dst_off: Optional[dtime] = None

    # ── DST-aware resolution ───────────────────────────────────────────────────

    def close_time_for(self, on: date) -> dtime:
        """The effective close on a given date (handles the US-DST flip)."""
        if self.dst_close_time is None:
            return self.close_time
        # When NY is on DST → earlier close (close_time); otherwise the later
        # US-standard close (dst_close_time).
        return self.close_time if _ny_is_dst(on) else self.dst_close_time

    def poll_close_for(self, on: date) -> dtime:
        if self.poll_close_dst_off is None:
            return self.poll_close
        return self.poll_close if _ny_is_dst(on) else self.poll_close_dst_off


class SessionClock:
    """Thin stateless helper bound to one `MarketSession` profile."""

    def __init__(self, session: MarketSession):
        self.session = session

    # ── Wall clock ─────────────────────────────────────────────────────────────

    @staticmethod
    def now() -> datetime:
        return datetime.now(IST)

    def _ist(self, now: Optional[datetime]) -> datetime:
        if now is None:
            return self.now()
        if now.tzinfo is None:
            # Treat naive as IST (the engine's convention).
            return now.replace(tzinfo=IST)
        return now.astimezone(IST)

    # ── Queries ────────────────────────────────────────────────────────────────

    def is_trading_day(self, on: date) -> bool:
        return on.weekday() in self.session.weekdays

    def is_open(self, now: Optional[datetime] = None) -> bool:
        """Inside the tradable window on a trading day (open inclusive, close exclusive)."""
        n = self._ist(now)
        if not self.is_trading_day(n.date()):
            return False
        t = n.time()
        return self.session.open_time <= t < self.session.close_time_for(n.date())

    def in_poll_window(self, now: Optional[datetime] = None) -> bool:
        """Inside the (wider) poll window on a trading day — both ends inclusive,
        matching the legacy equity runner gate."""
        n = self._ist(now)
        if not self.is_trading_day(n.date()):
            return False
        t = n.time()
        return self.session.poll_open <= t <= self.session.poll_close_for(n.date())

    def squareoff_time(self, on: date) -> dtime:
        """The intraday flatten time for a given date (close − squareoff margin)."""
        close = self.session.close_time_for(on)
        return (datetime.combine(on, close)
                - timedelta(minutes=self.session.squareoff_before_close_min)).time()

    def session_date(self, now: Optional[datetime] = None) -> date:
        """The trading date for `now`, in IST."""
        return self._ist(now).date()


# ── Named profiles ─────────────────────────────────────────────────────────────

# EQUITY reproduces the legacy hardcoded constants byte-identically:
#   open 09:15, close 15:30, poll window 09:00–15:35, 15-min squareoff, Mon–Fri.
EQUITY = MarketSession(
    name="EQUITY",
    open_time=dtime(9, 15),
    close_time=dtime(15, 30),
    poll_open=dtime(9, 0),
    poll_close=dtime(15, 35),
    squareoff_before_close_min=15,
    weekdays=frozenset({0, 1, 2, 3, 4}),
)

# MCX commodities evening session. Open 09:00 IST. Close flips with US DST:
#   NY on DST   → 23:30 IST (close_time)
#   NY standard → 23:55 IST (dst_close_time)
# Poll window mirrors the close flip (close + ~10-min post margin).
MCX = MarketSession(
    name="MCX",
    open_time=dtime(9, 0),
    close_time=dtime(23, 30),
    dst_close_time=dtime(23, 55),
    poll_open=dtime(8, 45),
    poll_close=dtime(23, 40),
    poll_close_dst_off=dtime(23, 59),
    squareoff_before_close_min=15,
    weekdays=frozenset({0, 1, 2, 3, 4}),
)

PROFILES = {EQUITY.name: EQUITY, MCX.name: MCX}


def get_session(name: str) -> MarketSession:
    """Look up a named profile (case-insensitive)."""
    try:
        return PROFILES[name.upper()]
    except KeyError as exc:
        raise KeyError(
            f"unknown market session {name!r}; known: {sorted(PROFILES)}") from exc
