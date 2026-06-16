"""QA findings M2, M4 — BarBuilder data integrity & memory safety.

M2 (Medium): on_tick stamps a bar with datetime.now(IST) by default — server
             receive time, not the exchange tick timestamp. on_tick *accepts*
             an explicit ts, but the LiveFeed callback never forwards it, so
             clock drift / GC pauses can mis-bucket bars that feed Kronos and
             the backtest.
M4 (Medium): On a sustained flush failure, _pending re-queues without bound.
"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo


from engine.bar_builder import BarBuilder

IST = ZoneInfo("Asia/Kolkata")


# ── M2: timestamp handling ──────────────────────────────────────────────────────

def test_on_tick_honors_explicit_timestamp():
    """Regression: when given the exchange timestamp, the bar buckets by it.
    The capability exists — the gap (M2) is that LiveFeed._on_data does not
    pass it. A fix should forward the tick's LTT here."""
    bb = BarBuilder()
    ts = datetime(2026, 6, 15, 10, 5, 37, tzinfo=IST)
    bb.on_tick("111", 100.0, cum_volume=0, ts=ts)
    assert bb._current["111"].minute_start == ts.replace(second=0, microsecond=0)


def test_cumulative_volume_delta_and_reset():
    """Regression: per-bar volume is the delta of cumulative volume, clamped
    to 0 across a daily reset."""
    bb = BarBuilder()
    base = datetime(2026, 6, 15, 10, 0, 0, tzinfo=IST)
    bb.on_tick("111", 100.0, cum_volume=1000, ts=base)            # first tick → delta 0
    bb.on_tick("111", 101.0, cum_volume=1500, ts=base)            # +500
    assert bb._current["111"].volume == 500
    bb.on_tick("111", 101.0, cum_volume=10, ts=base)              # reset → clamp 0
    assert bb._current["111"].volume == 500


# ── M4: unbounded pending on flush failure ──────────────────────────────────────

def test_pending_is_bounded_on_repeated_flush_failure(monkeypatch):
    import db as _db
    def _boom(*a, **k):
        raise RuntimeError("DB down")
    monkeypatch.setattr(_db, "get_session", _boom)   # every flush write fails

    bb = BarBuilder()
    base = datetime(2026, 6, 15, 10, 0, 0, tzinfo=IST)

    async def go():
        for i in range(600):
            # distinct sid, tick in a past minute → flush force-closes it into
            # _pending, the write fails, and it re-queues — forever.
            bb.on_tick(str(i), 100.0, cum_volume=0, ts=base)
            await bb.flush()
    asyncio.run(go())
    # 600 bars accumulate today; a robust buffer would cap retained bars.
    assert len(bb._pending) <= 500, "pending bar buffer should be capped"
