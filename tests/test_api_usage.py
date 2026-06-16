"""Tests for core/api_usage.py — FEAT-02 backend.

Three scenarios:
  (a) flush writes only the DELTA: two successive flushes after more calls
      write incremental deltas, not the absolute count each time.
  (b) IST-midnight counter reset: if calls_today drops (counter rolled over),
      the new value is treated as a fresh delta — no negative deltas, no errors.
  (c) query_today_totals aggregates across processes and includes per_day caps
      for all 4 categories even if some are absent from the table.

Tests are hermetic (SQLite in-memory or tmp file) and fast (<200ms total).
No real DB connection required.
"""
import asyncio
from datetime import date
from unittest.mock import MagicMock, patch


from core.api_usage import ApiUsageFlusher, query_today_totals, _ALL_CATEGORIES


# ── Shared SQLite in-memory helpers ────────────────────────────────────────────

def _make_sqlite_engine(db_path):
    """Create a SQLite engine with the api_usage table."""
    from sqlalchemy import create_engine, text
    eng = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    with eng.begin() as c:
        c.execute(text("""
            CREATE TABLE api_usage (
                ts_date   TEXT    NOT NULL,
                category  TEXT    NOT NULL,
                process   TEXT    NOT NULL,
                calls     INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (ts_date, category, process)
            )
        """))
    return eng


def _rows(eng):
    """Return all api_usage rows as dicts."""
    from sqlalchemy import text
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT ts_date, category, process, calls FROM api_usage ORDER BY category, process"
        )).fetchall()
    return [{"ts_date": r[0], "category": r[1], "process": r[2], "calls": r[3]} for r in rows]


def _make_fake_client(counts: dict):
    """Build a mock DhanClient whose rate_limit_usage() returns the given counts.

    counts: {category: calls_today_int}
    """
    client = MagicMock()
    client.rate_limit_usage.return_value = {
        cat: {"calls_today": n, "per_sec_used": 0, "per_sec": 10, "per_day": None}
        for cat, n in counts.items()
    }
    return client


# ── (a) Delta-only flush ────────────────────────────────────────────────────────

def test_flush_writes_delta_not_absolute(tmp_path):
    """Two flushes after increasing calls_today write incremental deltas,
    not the absolute count on the second flush."""
    db_path = tmp_path / "t.db"
    eng = _make_sqlite_engine(db_path)

    # Patch get_session to use our SQLite engine
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=eng)

    flusher = ApiUsageFlusher(process="trader")

    def run(calls_today_map):
        client = _make_fake_client(calls_today_map)
        # Drive flush via asyncio.run with a patched get_session
        async def go():
            with patch("db.get_session", lambda: _session_ctx(Session)):
                await flusher.flush(client)
        asyncio.run(go())

    from contextlib import contextmanager
    @contextmanager
    def _session_ctx(Session):
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # First flush: 100 data calls
    client1 = _make_fake_client({"data": 100, "orders": 0, "quote": 0, "non_trading": 0})
    async def go1():
        with patch("db.get_session", lambda: _session_ctx(Session)):
            await flusher.flush(client1)
    asyncio.run(go1())

    rows = _rows(eng)
    data_rows = [r for r in rows if r["category"] == "data"]
    assert len(data_rows) == 1
    assert data_rows[0]["calls"] == 100, f"Expected 100, got {data_rows[0]['calls']}"

    # Second flush: 150 data calls (50 more since last flush)
    client2 = _make_fake_client({"data": 150, "orders": 0, "quote": 0, "non_trading": 0})
    async def go2():
        with patch("db.get_session", lambda: _session_ctx(Session)):
            await flusher.flush(client2)
    asyncio.run(go2())

    rows = _rows(eng)
    data_rows = [r for r in rows if r["category"] == "data"]
    assert len(data_rows) == 1
    # Table should now hold 100 + 50 = 150
    assert data_rows[0]["calls"] == 150, (
        f"Expected 150 (100+50 delta), got {data_rows[0]['calls']}"
    )

    # Third flush: no new calls (calls_today still 150) — nothing written
    client3 = _make_fake_client({"data": 150, "orders": 0, "quote": 0, "non_trading": 0})
    async def go3():
        with patch("db.get_session", lambda: _session_ctx(Session)):
            await flusher.flush(client3)
    asyncio.run(go3())

    rows = _rows(eng)
    data_rows = [r for r in rows if r["category"] == "data"]
    assert data_rows[0]["calls"] == 150, "No new calls — table must not change"


# ── (b) IST-midnight counter reset ─────────────────────────────────────────────

def test_midnight_reset_handled_without_negative_delta(tmp_path):
    """If calls_today drops (midnight IST reset), the new value is treated as a
    full delta (not subtracted from the previous value), so no negative count
    is ever written."""
    db_path = tmp_path / "t.db"
    eng = _make_sqlite_engine(db_path)

    from sqlalchemy.orm import sessionmaker
    from contextlib import contextmanager
    Session = sessionmaker(bind=eng)

    @contextmanager
    def _session_ctx():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    flusher = ApiUsageFlusher(process="backfill")

    # Simulate end-of-day state: 5000 data calls already flushed
    async def flush_with(calls_today):
        client = _make_fake_client({"data": calls_today, "orders": 0, "quote": 0, "non_trading": 0})
        with patch("db.get_session", _session_ctx):
            await flusher.flush(client)

    asyncio.run(flush_with(5000))

    rows = _rows(eng)
    assert sum(r["calls"] for r in rows if r["category"] == "data") == 5000

    # Midnight: calls_today resets to 300 (new day's fresh calls)
    asyncio.run(flush_with(300))

    # The 300 should be added as a delta (not 300-5000 = -4700)
    rows = _rows(eng)
    total_data = sum(r["calls"] for r in rows if r["category"] == "data")
    assert total_data == 5300, (
        f"Expected 5000 + 300 = 5300 after midnight reset, got {total_data}"
    )

    # And flusher._last should now reflect the new value (300)
    assert flusher._last.get("data") == 300


# ── (c) query_today_totals aggregates across processes + includes caps ──────────

def test_query_today_totals_all_categories_and_caps(tmp_path):
    """query_today_totals returns all 4 categories, correct per_day caps,
    and aggregates calls correctly across two processes."""
    db_path = tmp_path / "t.db"
    eng = _make_sqlite_engine(db_path)

    from sqlalchemy import text
    today_str = date.today().isoformat()

    # Insert some rows: trader + backfill both had data calls; only trader had orders
    with eng.begin() as c:
        c.execute(text("INSERT INTO api_usage VALUES (:d, 'data',   'trader',   1200, '2026-06-16T10:00:00+05:30')"), {"d": today_str})
        c.execute(text("INSERT INTO api_usage VALUES (:d, 'data',   'backfill', 87000, '2026-06-16T10:00:00+05:30')"), {"d": today_str})
        c.execute(text("INSERT INTO api_usage VALUES (:d, 'orders', 'trader',   42,   '2026-06-16T10:00:00+05:30')"), {"d": today_str})

    from sqlalchemy.orm import sessionmaker
    from contextlib import contextmanager
    Session = sessionmaker(bind=eng)

    @contextmanager
    def _session_ctx():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    with patch("db.get_session", _session_ctx):
        result = query_today_totals()

    assert result["date"] == today_str

    cats = result["categories"]

    # All 4 categories must be present
    for cat in _ALL_CATEGORIES:
        assert cat in cats, f"Missing category: {cat}"

    # data: 1200 + 87000 = 88200, cap = 100000
    assert cats["data"]["total"] == 88200
    assert cats["data"]["per_day"] == 100_000
    assert cats["data"]["by_process"]["trader"] == 1200
    assert cats["data"]["by_process"]["backfill"] == 87000

    # orders: 42, cap = 7000
    assert cats["orders"]["total"] == 42
    assert cats["orders"]["per_day"] == 7000
    assert cats["orders"]["by_process"]["trader"] == 42

    # quote: absent from table → total=0, per_day=None (uncapped)
    assert cats["quote"]["total"] == 0
    assert cats["quote"]["per_day"] is None
    assert cats["quote"]["by_process"] == {}

    # non_trading: absent from table → total=0, per_day=None (uncapped)
    assert cats["non_trading"]["total"] == 0
    assert cats["non_trading"]["per_day"] is None


# ── (d) QR-C2: shutdown flush must not double-count ────────────────────────────

def test_shutdown_flush_does_not_double_count(tmp_path):
    """Regression for QR-C2: simulates the trader mid-session flush followed by
    a shutdown flush.

    Before the fix, the shutdown path constructed a *fresh* ApiUsageFlusher
    (with _last={}) and flushed the full calls_today as a delta, doubling the
    DB total.  The correct behaviour is that the shutdown flusher reuses the
    same instance (or one whose _last matches the last-flushed value) so only
    the true remaining delta is written.

    Scenario:
      1. Create ONE flusher (simulating the live write_heartbeat instance).
      2. Mid-session flush: 100 data calls → DB should hold 100.
      3. No new calls occur before shutdown.
      4. Shutdown flush: same flusher → delta == 0 → DB still holds 100.
      5. Assert total == 100, not 200.
    """
    db_path = tmp_path / "t.db"
    eng = _make_sqlite_engine(db_path)

    from contextlib import contextmanager
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=eng)

    @contextmanager
    def _session_ctx():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # Single flusher — shared between heartbeat loop and shutdown path
    flusher = ApiUsageFlusher(process="trader")

    async def do_flush(calls_today: int):
        client = _make_fake_client({"data": calls_today, "orders": 0, "quote": 0, "non_trading": 0})
        with patch("db.get_session", _session_ctx):
            await flusher.flush(client)

    # Step 2: mid-session flush at 100 calls
    asyncio.run(do_flush(100))
    rows = _rows(eng)
    data_total = sum(r["calls"] for r in rows if r["category"] == "data")
    assert data_total == 100, f"After mid-session flush expected 100, got {data_total}"

    # Step 3–4: shutdown flush — no new calls, same flusher, delta must be 0
    asyncio.run(do_flush(100))  # calls_today still 100
    rows = _rows(eng)
    data_total = sum(r["calls"] for r in rows if r["category"] == "data")
    assert data_total == 100, (
        f"QR-C2 regression: shutdown flush double-counted — expected 100, got {data_total}"
    )

    # Sanity: if 5 more calls happen before shutdown, only those 5 are written
    asyncio.run(do_flush(105))
    rows = _rows(eng)
    data_total = sum(r["calls"] for r in rows if r["category"] == "data")
    assert data_total == 105, (
        f"Expected 105 after +5 tail calls, got {data_total}"
    )
