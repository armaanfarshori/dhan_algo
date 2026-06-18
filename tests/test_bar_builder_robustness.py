"""QA findings M2, M4 — BarBuilder data integrity & memory safety.

M2 (Medium): on_tick stamps a bar with datetime.now(IST) by default — server
             receive time, not the exchange tick timestamp. on_tick *accepts*
             an explicit ts, but the LiveFeed callback never forwards it, so
             clock drift / GC pauses can mis-bucket bars that feed Kronos and
             the backtest.
M4 (Medium): On a sustained flush failure, _pending re-queues without bound.
"""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


from engine.bar_builder import BarBuilder

IST = ZoneInfo("Asia/Kolkata")


def test_future_stamped_tick_is_dropped():
    """Regression (2026-06-18 prod incident): a tick stamped far in the future
    must be dropped, not used to open a bar — otherwise every subsequent real
    tick looks 'out-of-order' and the feed freezes for that security."""
    bb = BarBuilder()
    future = datetime.now(IST) + timedelta(minutes=10)
    bb.on_tick("999", 100.0, cum_volume=0, ts=future)
    assert "999" not in bb._current, "future-stamped tick must not open a bar"
    # a normal current-time tick right after still opens a bar
    bb.on_tick("999", 101.0, cum_volume=0, ts=datetime.now(IST))
    assert "999" in bb._current


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


# ── T6: out-of-order tick guard ─────────────────────────────────────────────────

def test_out_of_order_tick_is_dropped():
    """T6 — Regression: a stale WS packet (past-minute timestamp after reconnect)
    must NOT roll the BarBuilder backwards or overwrite an already-closed bar.

    Sequence:
      1. Tick at minute T         → bar for T opens.
      2. Tick at minute T+1       → bar for T is closed; bar for T+1 opens.
      3. Stale tick at minute T   → must be silently dropped; current bar stays T+1.
    """
    from datetime import timedelta

    bb = BarBuilder()
    t0 = datetime(2026, 6, 16, 10, 5, 0, tzinfo=IST)   # minute T
    t1 = t0 + timedelta(minutes=1)                       # minute T+1

    # Step 1: open bar at T
    bb.on_tick("NSE_EQ:1234", 200.0, cum_volume=1000, ts=t0)
    assert bb._current["NSE_EQ:1234"].minute_start == t0

    # Step 2: advance to T+1 — this closes the T bar and opens T+1
    bb.on_tick("NSE_EQ:1234", 201.0, cum_volume=1100, ts=t1)
    assert bb._current["NSE_EQ:1234"].minute_start == t1, \
        "bar should have rolled forward to T+1"
    assert len(bb._pending) == 1, "T bar should now be in pending"
    t1_open = bb._current["NSE_EQ:1234"].open

    # Step 3: stale tick at minute T — must be dropped
    bb.on_tick("NSE_EQ:1234", 999.0, cum_volume=9999, ts=t0)

    current = bb._current["NSE_EQ:1234"]
    assert current.minute_start == t1, \
        "current bar must still be anchored to T+1 after stale tick"
    assert current.open == t1_open, \
        "stale tick must not alter the open of the T+1 bar"
    assert len(bb._pending) == 1, \
        "no additional bar must have been closed by the stale tick"
