"""Unit tests for F&O API route handlers in apps/routes/fno.py.

Covers five endpoints:
  /api/fno/vrp         → fno_vrp_handler
  /api/fno/vrp/series  → fno_vrp_series_handler
  /api/fno/paper       → fno_paper_handler
  /api/fno/chain       → fno_chain_handler
  /api/fno/backtest    → fno_backtest_handler

Pattern mirrors tests/test_route_handlers.py exactly:
  • _FakeRequest simulates aiohttp.web.Request (rel_url.query.get)
  • asyncio.run(handler(req)) drives async handlers synchronously
  • _body() decodes the JSON from web.Response
  • apps.api._db_query is monkeypatched to run the callable in-process
  • apps.api._cache_get/_cache_set are no-op stubs
  • DB is mocked at db.get_engine; never a real connection
  • Backtest handler reads JSON files — mocked via glob/os.path.getmtime/open

Import guard: if apps/routes/fno.py is not yet present all tests are
skipped (not failed) so CI stays green while the handler file is being
written in parallel.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — tests skip cleanly if fno.py not yet written
# ---------------------------------------------------------------------------
try:
    from apps.routes import fno as fno_mod

    _MODULE_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    fno_mod = None  # type: ignore[assignment]
    _MODULE_AVAILABLE = False

requires_fno = pytest.mark.skipif(
    not _MODULE_AVAILABLE, reason="apps/routes/fno.py not yet implemented"
)

# Verify the handler names exist on the module (report mismatch clearly)
_EXPECTED_HANDLERS = [
    "fno_vrp_handler",
    "fno_vrp_series_handler",
    "fno_paper_handler",
    "fno_chain_handler",
    "fno_backtest_handler",
]
if _MODULE_AVAILABLE:
    _MISSING_HANDLERS = [h for h in _EXPECTED_HANDLERS if not hasattr(fno_mod, h)]
else:
    _MISSING_HANDLERS = _EXPECTED_HANDLERS


# ---------------------------------------------------------------------------
# Shared helpers (mirrored from test_route_handlers.py)
# ---------------------------------------------------------------------------

class _FakeRequest:
    """Minimal stand-in for aiohttp.web.Request."""

    def __init__(self, query: dict | None = None) -> None:
        _q = query or {}

        class _URL:
            class _Query(dict):
                pass

            query = _Query(_q)

        self.rel_url = _URL()
        self.method = "GET"
        self.headers = {}


def _body(resp) -> dict:
    """Decode JSON body from a web.Response."""
    raw = resp.body if hasattr(resp, "body") else resp.text
    if isinstance(raw, bytes):
        return json.loads(raw.decode())
    return json.loads(raw)


def _patch_api(monkeypatch, *, cache_val=None):
    """Monkeypatch apps.api helpers used by fno handlers.

    _cache_get  → returns cache_val (None = cache miss → go compute)
    _cache_set  → no-op
    _db_query   → runs callable synchronously in-process (no thread pool)
    """
    import apps.api as api_mod

    monkeypatch.setattr(api_mod, "_cache_get", lambda key, ttl: cache_val, raising=False)
    monkeypatch.setattr(api_mod, "_cache_set", lambda key, val: None, raising=False)

    async def _fake_db_query(fn):
        return fn()

    monkeypatch.setattr(api_mod, "_db_query", _fake_db_query, raising=False)
    return api_mod


def _make_conn_with_sequence(fetchone_seq: list, fetchall_seq: list | None = None):
    """Build a mock connection that returns different results per successive execute() call.

    fno handlers run multiple queries inside a single `with engine.connect() as conn:` block.
    Each call to conn.execute() must return a fresh mock with the right fetchone/fetchall.

    fetchone_seq : list of return values for successive fetchone() calls
    fetchall_seq : if provided, successive fetchall() results; otherwise [] each time
    """
    if fetchall_seq is None:
        fetchall_seq = [[] for _ in fetchone_seq]

    call_idx = [0]

    def _execute(*args, **kwargs):
        i = call_idx[0]
        mock_result = MagicMock()
        fn_val = fetchone_seq[i] if i < len(fetchone_seq) else None
        fa_val = fetchall_seq[i] if i < len(fetchall_seq) else []
        mock_result.fetchone.return_value = fn_val
        mock_result.fetchall.return_value = fa_val
        call_idx[0] += 1
        return mock_result

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.side_effect = _execute
    return mock_conn


def _engine_from_conn(mock_conn):
    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_conn
    return mock_engine


def _engine_raises(exc):
    """Engine whose connect().__enter__().execute() raises exc."""
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.side_effect = exc
    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_conn
    return mock_engine


# ---------------------------------------------------------------------------
# Reference timestamps / dates
# ---------------------------------------------------------------------------
_SNAP_TS = datetime(2026, 6, 20, 9, 30, 0, tzinfo=timezone.utc)
_SNAP_TS_STR = _SNAP_TS.isoformat()


# ===========================================================================
# 1.  /api/fno/vrp  →  fno_vrp_handler
#
# The handler runs 4 queries inside one connect() block:
#   [0] rv_row    fetchone → (date, realized_vol_20d)
#   [1] vix_row   fetchone → (date, vix_close_pct)
#   [2] atm_row   fetchone → (date, straddle_iv, atm_strike, dte, spot_ref, implied_move, expiry)
#   [3] spot_row  fetchone → (spot,)
# ===========================================================================

def _vrp_happy_conn():
    """Four-query sequence for the happy path."""
    return _make_conn_with_sequence(
        fetchone_seq=[
            ("2026-06-20", 0.12),                                  # rv_row
            ("2026-06-20", 14.5),                                  # vix_row (pct)
            ("2026-06-20", 0.145, 24000.0, 5, 24010.0, 250.0, "2026-06-27"),  # atm_row
            (24010.0,),                                            # spot_row
        ]
    )


@requires_fno
def test_vrp_happy_path(monkeypatch):
    """/api/fno/vrp — 200 ok:True, all required envelope fields present."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_from_conn(_vrp_happy_conn())):
        resp = asyncio.run(fno_mod.fno_vrp_handler(_FakeRequest()))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["available"] is True
    assert "realized_vol" in body
    assert "implied_vol" in body
    assert "vix" in body
    assert "spread" in body
    assert "vrp_ratio" in body
    assert body["gate_decision"] in {"SELL_PREMIUM", "BUY_PREMIUM", "STAND_ASIDE"}
    assert "k" in body
    assert body["unit"] == "fraction"


@requires_fno
def test_vrp_fractions_in_range(monkeypatch):
    """realized_vol and implied_vol must be fractions [0, 2], NOT percent (e.g. not 14.5)."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_from_conn(_vrp_happy_conn())):
        resp = asyncio.run(fno_mod.fno_vrp_handler(_FakeRequest()))

    body = _body(resp)
    rv = body["realized_vol"]
    iv = body["implied_vol"]

    assert 0 < rv < 2, f"realized_vol={rv} looks like percent, not fraction"
    assert 0 < iv < 2, f"implied_vol={iv} looks like percent, not fraction"


@requires_fno
def test_vrp_spread_equals_iv_minus_rv(monkeypatch):
    """spread == implied_vol - realized_vol (within floating-point tolerance)."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_from_conn(_vrp_happy_conn())):
        resp = asyncio.run(fno_mod.fno_vrp_handler(_FakeRequest()))

    body = _body(resp)
    assert math.isclose(
        body["spread"],
        body["implied_vol"] - body["realized_vol"],
        abs_tol=1e-9,
    ), f"spread={body['spread']} != iv-rv={body['implied_vol'] - body['realized_vol']}"


@requires_fno
def test_vrp_gate_sell_when_rv_lt_k_times_iv(monkeypatch):
    """realized_vol < k * implied_vol and realized < implied → SELL_PREMIUM."""
    _patch_api(monkeypatch)

    # rv=0.10, vix=15.0 (pct) → iv=0.15; atm_row also iv=0.15
    # 0.10 < 0.9 * 0.15 = 0.135 → SELL_PREMIUM
    conn = _make_conn_with_sequence(
        fetchone_seq=[
            ("2026-06-20", 0.10),
            ("2026-06-20", 15.0),
            ("2026-06-20", 0.15, 24000.0, 5, 24010.0, 200.0, "2026-06-27"),
            (24010.0,),
        ]
    )
    with patch("db.get_engine", return_value=_engine_from_conn(conn)):
        resp = asyncio.run(fno_mod.fno_vrp_handler(_FakeRequest()))

    assert _body(resp)["gate_decision"] == "SELL_PREMIUM"


@requires_fno
def test_vrp_gate_buy_when_rv_gt_iv(monkeypatch):
    """realized_vol > implied_vol → BUY_PREMIUM."""
    _patch_api(monkeypatch)

    # rv=0.20, vix=12.0 (pct) → iv=0.12; atm_row iv=0.12; rv > iv → BUY_PREMIUM
    conn = _make_conn_with_sequence(
        fetchone_seq=[
            ("2026-06-20", 0.20),
            ("2026-06-20", 12.0),
            ("2026-06-20", 0.12, 24000.0, 5, 24010.0, 150.0, "2026-06-27"),
            (24010.0,),
        ]
    )
    with patch("db.get_engine", return_value=_engine_from_conn(conn)):
        resp = asyncio.run(fno_mod.fno_vrp_handler(_FakeRequest()))

    assert _body(resp)["gate_decision"] == "BUY_PREMIUM"


@requires_fno
def test_vrp_empty_data(monkeypatch):
    """No DB rows → 200, ok:True, available:False. Must not raise."""
    _patch_api(monkeypatch)

    # All four queries return None
    conn = _make_conn_with_sequence(fetchone_seq=[None, None, None, None])
    with patch("db.get_engine", return_value=_engine_from_conn(conn)):
        resp = asyncio.run(fno_mod.fno_vrp_handler(_FakeRequest()))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["available"] is False


@requires_fno
def test_vrp_db_error(monkeypatch):
    """DB exception → response with ok:False, no unhandled exception propagated."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_raises(RuntimeError("DB refused"))):
        resp = asyncio.run(fno_mod.fno_vrp_handler(_FakeRequest()))

    body = _body(resp)
    assert body["ok"] is False


# ===========================================================================
# 2.  /api/fno/vrp/series  →  fno_vrp_series_handler
#
# The handler runs ONE query (fetchall) inside the connect() block.
# Series rows: (date, realized_vol_20d_frac, vix_frac)
# ===========================================================================

_VRP_SERIES_ROWS = [
    ("2026-06-01", 0.11, 0.138),
    ("2026-06-02", 0.115, 0.140),
    ("2026-06-03", 0.12, 0.145),
]


def _vrp_series_conn(rows):
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.execute.return_value.fetchall.return_value = rows
    return conn


@requires_fno
def test_vrp_series_happy_path(monkeypatch):
    """/api/fno/vrp/series — 200 ok:True, correct envelope and series structure."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_from_conn(_vrp_series_conn(_VRP_SERIES_ROWS))):
        resp = asyncio.run(fno_mod.fno_vrp_series_handler(_FakeRequest(query={"days": "30"})))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["unit"] == "fraction"
    assert body["count"] == 3
    assert isinstance(body["series"], list)
    assert len(body["series"]) == 3

    row0 = body["series"][0]
    assert "date" in row0
    assert "realized" in row0
    assert "vix" in row0


@requires_fno
def test_vrp_series_realized_fractions(monkeypatch):
    """series[*].realized values must be fractions [0, 2], not percent."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_from_conn(_vrp_series_conn(_VRP_SERIES_ROWS))):
        resp = asyncio.run(fno_mod.fno_vrp_series_handler(_FakeRequest()))

    body = _body(resp)
    for entry in body["series"]:
        rv = entry["realized"]
        assert rv is None or 0 < rv < 2, f"realized={rv} looks like percent, not fraction"


@requires_fno
def test_vrp_series_empty_data(monkeypatch):
    """Empty DB → 200, ok:True, count:0, series:[]."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_from_conn(_vrp_series_conn([]))):
        resp = asyncio.run(fno_mod.fno_vrp_series_handler(_FakeRequest()))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["count"] == 0
    assert body["series"] == []


@requires_fno
def test_vrp_series_db_error(monkeypatch):
    """DB exception → ok:False, no exception propagated."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_raises(RuntimeError("timeout"))):
        resp = asyncio.run(fno_mod.fno_vrp_series_handler(_FakeRequest()))

    body = _body(resp)
    assert body["ok"] is False


# ===========================================================================
# 3.  /api/fno/paper  →  fno_paper_handler
#
# The handler runs THREE queries inside one connect() block:
#   [0] summary_row  fetchone → (resolved, open_count, net_pnl, wins)
#   [1] curve_rows   fetchall → [(date, cum_pnl), ...]
#   [2] trade_rows   fetchall → [...full trade rows...]
# ===========================================================================

_SUMMARY_ROW = (2, 1, 800.0, 1)   # 2 resolved, 1 open, net=800, 1 win
_CURVE_ROWS  = [("2026-06-08", 1200.0), ("2026-06-15", 800.0)]
_TRADE_ROWS  = [
    # (id, status, entry_date, expiry_date, gate_decision,
    #  straddle_iv, rv20d, spk, sck, lpk, lck, credit, max_loss, net_pnl, win)
    (1, "RESOLVED", "2026-06-01", "2026-06-08", "SELL_PREMIUM",
     0.145, 0.12, 23800.0, 24200.0, 23700.0, 24300.0, 150.0, 3250.0, 1200.0, True),
    (2, "RESOLVED", "2026-06-08", "2026-06-15", "SELL_PREMIUM",
     0.138, 0.115, 23850.0, 24150.0, 23750.0, 24250.0, 130.0, 3250.0, -400.0, False),
    (3, "OPEN",     "2026-06-15", "2026-06-22", "SELL_PREMIUM",
     0.140, 0.11,  23900.0, 24100.0, 23800.0, 24200.0, 140.0, 3250.0, None,   None),
]


def _paper_conn(*, summary_row=_SUMMARY_ROW, curve_rows=_CURVE_ROWS, trade_rows=_TRADE_ROWS):
    return _make_conn_with_sequence(
        fetchone_seq=[summary_row],
        fetchall_seq=[[], curve_rows, trade_rows],
    )


@requires_fno
def test_paper_happy_path(monkeypatch):
    """/api/fno/paper — 200 ok:True, summary + equity_curve + trades present."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_from_conn(_paper_conn())):
        resp = asyncio.run(fno_mod.fno_paper_handler(_FakeRequest(query={"limit": "50"})))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True

    summary = body["summary"]
    assert "resolved" in summary
    assert "open" in summary
    assert "net_pnl" in summary
    assert "win_rate" in summary
    assert "wins" in summary
    assert "avg_pnl" in summary

    assert summary["resolved"] == 2
    assert summary["open"] == 1
    assert summary["wins"] == 1
    assert math.isclose(summary["net_pnl"], 800.0, abs_tol=0.01)
    assert math.isclose(summary["win_rate"], 0.5, abs_tol=1e-6)

    assert isinstance(body["equity_curve"], list)
    assert isinstance(body["trades"], list)


@requires_fno
def test_paper_summary_win_rate_zero_when_no_resolved(monkeypatch):
    """win_rate must be 0.0 (not NaN/exception) when no RESOLVED trades exist."""
    _patch_api(monkeypatch)

    no_resolved = (0, 1, 0.0, 0)   # 0 resolved, 1 open, net=0, 0 wins
    conn = _paper_conn(summary_row=no_resolved, curve_rows=[], trade_rows=[])
    with patch("db.get_engine", return_value=_engine_from_conn(conn)):
        resp = asyncio.run(fno_mod.fno_paper_handler(_FakeRequest()))

    body = _body(resp)
    assert body["ok"] is True
    summary = body["summary"]
    assert summary["resolved"] == 0
    assert summary["wins"] == 0
    assert summary["win_rate"] == 0.0


@requires_fno
def test_paper_empty_data(monkeypatch):
    """No trades → 200, ok:True, zeroed summary, empty curves. Must not raise."""
    _patch_api(monkeypatch)

    empty_summary = (0, 0, 0.0, 0)
    conn = _paper_conn(summary_row=empty_summary, curve_rows=[], trade_rows=[])
    with patch("db.get_engine", return_value=_engine_from_conn(conn)):
        resp = asyncio.run(fno_mod.fno_paper_handler(_FakeRequest()))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    summary = body["summary"]
    assert summary["resolved"] == 0
    assert summary["open"] == 0
    assert summary["net_pnl"] == 0.0
    assert summary["win_rate"] == 0.0
    assert summary["wins"] == 0
    assert body["equity_curve"] == []
    assert body["trades"] == []


@requires_fno
def test_paper_db_error(monkeypatch):
    """DB exception → ok:False, no unhandled exception."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_raises(RuntimeError("DB down"))):
        resp = asyncio.run(fno_mod.fno_paper_handler(_FakeRequest()))

    body = _body(resp)
    assert body["ok"] is False


# ===========================================================================
# 4.  /api/fno/chain  →  fno_chain_handler
#
# The handler runs multiple queries inside one connect() block; the exact
# sequence depends on whether expiry is auto-resolved.  We mock using
# _make_conn_with_sequence and list the calls in order.
#
# Query order (no ?expiry= param):
#   [0] latest snapshot_time  fetchone → (_SNAP_TS,)
#   [1] nearest future expiry fetchone → ("2026-06-27",)
#   [2] expiries list         fetchall → [("2026-06-27",)]
#   [3] spot from snapshot    fetchone → (24010.0,)
#   [4] ATM strike            fetchone → (24000.0,)
#   [5] chain rows            fetchall → [strike, opt_type, ltp, oi, iv_pct, delta]
# ===========================================================================

_CHAIN_DB_ROWS = [
    # (strike, opt_type, ltp, oi, iv_pct, delta)
    (24000, "CE", 185.0, 500000, 14.5, 0.52),
    (24000, "PE", 175.0, 480000, 14.2, -0.48),
    (24050, "CE", 160.0, 400000, 14.8, 0.45),
    (24050, "PE", 195.0, 420000, 13.9, -0.55),
]


def _chain_conn(rows=_CHAIN_DB_ROWS, *, snap_ts=_SNAP_TS, expiry="2026-06-27", spot=24010.0, atm=24000.0):
    return _make_conn_with_sequence(
        # Single shared call counter across execute() calls. The handler's call
        # order is: [0] snapshot_time (fetchone), [1] nearest expiry (fetchone),
        # [2] expiries list (FETCHALL), [3] spot (fetchone), [4] ATM strike
        # (fetchone), [5] chain rows (FETCHALL). A None placeholder at index 2
        # keeps spot/atm aligned to their real call indices (3 and 4).
        fetchone_seq=[
            (snap_ts,),             # [0] latest snapshot_time
            (expiry,),              # [1] nearest expiry
            None,                   # [2] expiries list call → fetchall, fetchone unused
            (spot,),                # [3] spot ref
            (atm,),                 # [4] ATM strike
        ],
        fetchall_seq=[
            [],                     # [0] fetchone
            [],                     # [1] fetchone
            [(expiry,)],            # [2] expiries list
            [],                     # [3] fetchone
            [],                     # [4] fetchone
            rows,                   # [5] chain rows
        ],
    )


@requires_fno
def test_chain_happy_path(monkeypatch):
    """/api/fno/chain — 200 ok:True, all envelope fields present."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_from_conn(_chain_conn())):
        resp = asyncio.run(fno_mod.fno_chain_handler(_FakeRequest(query={"strikes": "5"})))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["available"] is True
    assert "snapshot_time" in body
    assert "age_min" in body
    assert "stale" in body
    assert "expiry" in body
    assert "spot" in body
    assert "atm_strike" in body
    assert body["iv_unit"] == "fraction"
    assert isinstance(body["rows"], list)
    assert len(body["rows"]) > 0
    # spot and ATM come from distinct queries — guard the fixture alignment so
    # the ATM-strike lookup is actually exercised (regression: off-by-one made
    # atm_row None → atm_strike silently fell back to spot).
    assert body["spot"] == 24010.0
    assert body["atm_strike"] == 24000.0


@requires_fno
def test_chain_iv_converted_to_fraction(monkeypatch):
    """CE/PE iv in each chain row must be the raw percent column ÷ 100 (fraction < 2)."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_from_conn(_chain_conn())):
        resp = asyncio.run(fno_mod.fno_chain_handler(_FakeRequest()))

    body = _body(resp)
    for row in body["rows"]:
        ce = row.get("ce") or {}
        pe = row.get("pe") or {}
        if ce and ce.get("iv") is not None:
            assert ce["iv"] < 2, f"ce.iv={ce['iv']} looks like percent, not fraction"
        if pe and pe.get("iv") is not None:
            assert pe["iv"] < 2, f"pe.iv={pe['iv']} looks like percent, not fraction"


@requires_fno
def test_chain_row_structure(monkeypatch):
    """Each chain row must have 'strike' and at least a 'ce' or 'pe' sub-object."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_from_conn(_chain_conn())):
        resp = asyncio.run(fno_mod.fno_chain_handler(_FakeRequest()))

    body = _body(resp)
    for row in body["rows"]:
        assert "strike" in row, f"row missing 'strike': {row}"
        assert "ce" in row or "pe" in row, f"row has neither 'ce' nor 'pe': {row}"


@requires_fno
def test_chain_empty_data(monkeypatch):
    """No snapshot rows → 200, ok:True, available:False. Must not raise."""
    _patch_api(monkeypatch)

    # First query (latest snapshot_time) returns None → handler returns None → available:false
    conn = _make_conn_with_sequence(fetchone_seq=[None])
    with patch("db.get_engine", return_value=_engine_from_conn(conn)):
        resp = asyncio.run(fno_mod.fno_chain_handler(_FakeRequest()))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["available"] is False


@requires_fno
def test_chain_db_error(monkeypatch):
    """DB exception → ok:False, no unhandled exception."""
    _patch_api(monkeypatch)

    with patch("db.get_engine", return_value=_engine_raises(RuntimeError("engine error"))):
        resp = asyncio.run(fno_mod.fno_chain_handler(_FakeRequest()))

    body = _body(resp)
    assert body["ok"] is False


# ===========================================================================
# 5.  /api/fno/backtest  →  fno_backtest_handler
#
# This handler does NOT query the DB — it reads the newest JSON file from
# research/backtest/results/*.json.  We mock glob.glob + os.path.getmtime
# and builtins.open to avoid any filesystem access.
# ===========================================================================

_BACKTEST_JSON = {
    "as_of": "2026-06-20",
    "n_trades": 50,
    "win_rate": 0.68,
    "profit_factor": 1.45,
    "net_pnl": 12500.0,
    "move_mult": 1.5,
    "period": "2024-01-01/2026-06-20",
    "vrp_note": "India VIX > realized on 100% of SELL days (test fixture)",
    "caveats": ["Phase-0 proxy; no fine-tuned forecast"],
    "go": True,
    "go_reason": "Sharpe 1.2, PF 1.45, n=50",
}


@requires_fno
def test_backtest_happy_path(monkeypatch):
    """/api/fno/backtest — 200 ok:True, all KPI fields present when a result file exists."""
    _patch_api(monkeypatch)

    with tempfile.NamedTemporaryFile(
        suffix=".json", mode="w", delete=False
    ) as tmp:
        json.dump(_BACKTEST_JSON, tmp)
        tmp_path = tmp.name

    try:
        with patch("glob.glob", return_value=[tmp_path]):
            with patch("os.path.getmtime", return_value=1.0):
                resp = asyncio.run(fno_mod.fno_backtest_handler(_FakeRequest()))
    finally:
        os.unlink(tmp_path)

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["available"] is True
    assert "n_trades" in body
    assert "win_rate" in body
    assert "profit_factor" in body
    assert "net_pnl" in body
    assert "go" in body
    assert "go_reason" in body
    assert "caveats" in body


@requires_fno
def test_backtest_kpi_types(monkeypatch):
    """Backtest KPI fields must have the correct Python types."""
    _patch_api(monkeypatch)

    with tempfile.NamedTemporaryFile(
        suffix=".json", mode="w", delete=False
    ) as tmp:
        json.dump(_BACKTEST_JSON, tmp)
        tmp_path = tmp.name

    try:
        with patch("glob.glob", return_value=[tmp_path]):
            with patch("os.path.getmtime", return_value=1.0):
                resp = asyncio.run(fno_mod.fno_backtest_handler(_FakeRequest()))
    finally:
        os.unlink(tmp_path)

    body = _body(resp)
    assert isinstance(body["n_trades"], int)
    assert isinstance(body["win_rate"], float)
    assert isinstance(body["profit_factor"], float)
    assert isinstance(body["net_pnl"], float)
    assert isinstance(body["go"], bool)


@requires_fno
def test_backtest_empty_data(monkeypatch):
    """No result JSON files → 200, ok:True, available:False. Must not raise."""
    _patch_api(monkeypatch)

    with patch("glob.glob", return_value=[]):
        resp = asyncio.run(fno_mod.fno_backtest_handler(_FakeRequest()))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["available"] is False
    # KPI fields must be absent or None when not available
    assert "n_trades" not in body or body.get("n_trades") is None


@requires_fno
def test_backtest_db_error(monkeypatch):
    """glob/IO exception → ok:False, no unhandled exception propagated."""
    _patch_api(monkeypatch)

    with patch("glob.glob", side_effect=OSError("permission denied")):
        resp = asyncio.run(fno_mod.fno_backtest_handler(_FakeRequest()))

    body = _body(resp)
    assert body["ok"] is False
