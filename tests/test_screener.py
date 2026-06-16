"""TEST-05 — NSE screener filtering and ranking.

get_top_volatile() embeds filtering entirely inside a SQL query (price floor ₹50,
volume floor 50K, ATR% ranking).  We monkeypatch db.get_session so no real DB is
needed and we can supply synthetic rows.

get_top_live() applies Python-side filtering (prev_close > 0, ltp > 0) and
sorts by change_pct or volume — those are tested against a fake Dhan client.
"""
import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_row(security_id, atr_pct, avg_volume, avg_close, days=20):
    """Return a fake SQLAlchemy row (indexable tuple)."""
    return (security_id, atr_pct, avg_volume, avg_close, days)


@contextmanager
def _fake_session(rows):
    """Context manager that yields a mock SQLAlchemy session returning `rows`."""
    session = MagicMock()
    # session.execute(...).fetchall() returns the supplied rows
    session.execute.return_value.fetchall.return_value = rows
    yield session


# ── get_top_volatile — DB-backed screener ────────────────────────────────────

def _run_volatile(rows, **kwargs):
    """Patch db.get_session and run get_top_volatile with synthetic rows.

    The screener does `from db import get_session` inside the function body,
    so the name that gets bound at call time comes from the `db` module itself.
    Patching `db.get_session` is the correct intercept point.
    """
    with patch("db.get_session", lambda: _fake_session(rows)):
        from core.nse_screener import get_top_volatile
        return get_top_volatile(**kwargs)


def test_volatile_empty_when_no_rows():
    result = _run_volatile([])
    assert result == []


def test_volatile_returns_correct_security_ids():
    rows = [
        _make_row("2885", 0.025, 100_000, 500.0),
        _make_row("3045", 0.018, 200_000, 300.0),
    ]
    result = _run_volatile(rows)
    ids = [r["security_id"] for r in result]
    assert "2885" in ids
    assert "3045" in ids


def test_volatile_result_shape():
    """Each item must have the documented keys."""
    rows = [_make_row("2885", 0.025, 100_000, 500.0)]
    result = _run_volatile(rows)
    assert len(result) == 1
    item = result[0]
    for key in ("security_id", "score", "avg_volume", "avg_close", "days", "reason"):
        assert key in item, f"Missing key: {key}"


def test_volatile_score_is_atr_pct():
    rows = [_make_row("2885", 0.0234, 100_000, 500.0)]
    result = _run_volatile(rows)
    assert abs(result[0]["score"] - 0.0234) < 1e-9


def test_volatile_preserves_db_ordering():
    """The function returns rows in the ORDER already given by the DB (SQL
    handles ranking — we just pass through whatever the DB returns)."""
    rows = [
        _make_row("AAA", 0.050, 100_000, 200.0),   # highest ATR
        _make_row("BBB", 0.030, 100_000, 150.0),
        _make_row("CCC", 0.010, 100_000, 100.0),
    ]
    result = _run_volatile(rows)
    assert result[0]["security_id"] == "AAA"
    assert result[1]["security_id"] == "BBB"
    assert result[2]["security_id"] == "CCC"


def test_volatile_respects_n_limit_via_db():
    """When the DB returns only n rows (because of LIMIT :n in SQL), the Python
    function must not truncate further — all returned rows appear in output."""
    rows = [_make_row(str(i), 0.01 * i, 100_000, 100.0) for i in range(5, 0, -1)]
    result = _run_volatile(rows, n=5)
    assert len(result) == 5


def test_volatile_returns_empty_on_db_exception():
    """If the DB call raises, the screener must return [] (fail-safe)."""
    def _bad_session():
        raise RuntimeError("DB connection refused")

    with patch("db.get_session", _bad_session):
        from core import nse_screener
        result = nse_screener.get_top_volatile()
    assert result == []


# ── The SQL filters (price ≥ 50, volume ≥ 50K) live in the WHERE/HAVING
# clause — the DB enforces them before returning rows.  We verify the boundary
# by confirming what the SQL string actually contains so a careless edit can't
# remove the guard silently.

def test_sql_contains_price_floor():
    """SQL must reference min_price so the DB-side filter can't be removed."""
    import inspect
    from core import nse_screener
    src = inspect.getsource(nse_screener.get_top_volatile)
    assert "min_price" in src, "min_price filter not found in get_top_volatile SQL"


def test_sql_contains_volume_floor():
    """SQL must reference min_vol so the DB-side volume filter can't be removed."""
    import inspect
    from core import nse_screener
    src = inspect.getsource(nse_screener.get_top_volatile)
    assert "min_vol" in src, "min_vol filter not found in get_top_volatile SQL"


def test_sql_orders_by_atr_pct():
    """Result must be ranked by ATR% descending."""
    import inspect
    from core import nse_screener
    src = inspect.getsource(nse_screener.get_top_volatile)
    assert "atr_pct DESC" in src or "ORDER BY atr_pct" in src, \
        "ATR% descending ordering not found in get_top_volatile SQL"


# ── get_top_live — Python-side filtering ──────────────────────────────────────

async def _run_live(instruments_data, rank_by="change_pct"):
    """Patch get_session (for universe fetch) and Dhan client for live screener."""
    # Universe: return one security id
    universe_rows = [(sid,) for sid in instruments_data]
    session = MagicMock()
    session.execute.return_value.fetchall.return_value = universe_rows

    @contextmanager
    def _universe_session():
        yield session

    # Build a fake Dhan client whose get_ohlc returns the supplied data
    fake_ohlc_data = {}
    for sid, info in instruments_data.items():
        fake_ohlc_data[sid] = info

    fake_client = MagicMock()

    async def _get_ohlc(_payload):
        return {"data": fake_ohlc_data}

    fake_client.get_ohlc = _get_ohlc

    with patch("db.get_session", _universe_session):
        from core.nse_screener import get_top_live
        return await get_top_live(fake_client, n=10, rank_by=rank_by)


def test_live_excludes_zero_prev_close():
    """An instrument with prev_close=0 must be excluded from results."""
    instruments = {
        "2885": {"previousClose": 0, "lastPrice": 100, "volume": 100_000},
        "3045": {"previousClose": 200, "lastPrice": 210, "volume": 100_000},
    }
    result = asyncio.run(_run_live(instruments))
    ids = [r["security_id"] for r in result]
    assert "2885" not in ids
    assert "3045" in ids


def test_live_excludes_zero_ltp():
    """An instrument with ltp=0 must be excluded."""
    instruments = {
        "2885": {"previousClose": 100, "lastPrice": 0, "volume": 100_000},
        "3045": {"previousClose": 200, "lastPrice": 210, "volume": 100_000},
    }
    result = asyncio.run(_run_live(instruments))
    ids = [r["security_id"] for r in result]
    assert "2885" not in ids


def test_live_ranks_by_change_pct_descending():
    """With rank_by='change_pct', the highest mover must appear first.

    Security IDs must be numeric strings because get_top_live does int(sid)
    when building the Dhan get_ohlc payload.
    """
    instruments = {
        "1001": {"previousClose": 100, "lastPrice": 120, "volume": 1_000},   # +20%
        "1002": {"previousClose": 100, "lastPrice": 105, "volume": 1_000},   # +5%
        "1003": {"previousClose": 100, "lastPrice": 112, "volume": 1_000},   # +12%
    }
    result = asyncio.run(_run_live(instruments, rank_by="change_pct"))
    assert result[0]["security_id"] == "1001"
    assert result[1]["security_id"] == "1003"
    assert result[2]["security_id"] == "1002"


def test_live_ranks_by_volume_descending():
    """With rank_by='volume', the highest volume must appear first."""
    instruments = {
        "1001": {"previousClose": 100, "lastPrice": 102, "volume": 500_000},
        "1002": {"previousClose": 100, "lastPrice": 102, "volume": 2_000_000},
        "1003": {"previousClose": 100, "lastPrice": 102, "volume": 100_000},
    }
    result = asyncio.run(_run_live(instruments, rank_by="volume"))
    assert result[0]["security_id"] == "1002"


def test_live_result_shape():
    """Each live screener result must contain the documented keys."""
    instruments = {
        "2885": {"previousClose": 200, "lastPrice": 210, "volume": 100_000},
    }
    result = asyncio.run(_run_live(instruments))
    assert len(result) == 1
    for key in ("security_id", "score", "ltp", "volume", "reason"):
        assert key in result[0], f"Missing key: {key}"
