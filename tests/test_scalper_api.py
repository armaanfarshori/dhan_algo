"""Unit tests for scalper API route handlers in apps/routes/scalper.py.

Covers:
  GET  /api/scalper/live      → scalper_live_handler
  GET  /api/scalper/trades    → scalper_trades_handler
  GET  /api/scalper/governor  → scalper_governor_handler
  POST /api/scalper/control   → scalper_control_handler

Pattern mirrors tests/test_fno_api.py and tests/test_api_handlers.py:
  * _FakeRequest simulates aiohttp.web.Request
  * asyncio.run(handler(req)) drives async handlers synchronously
  * apps.api._db_query / _cache_get / _cache_set are monkeypatched to run in-process
  * DB is mocked at db.get_engine; never a real connection

Every GET must return a safe empty SHELL when the tables / heartbeat are absent.
The control POST must 401 on missing/wrong token when DASHBOARD_TOKEN is set, and
must write the run/ flag on a valid token (or when unset = fail-open).
"""
from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — skip cleanly if scalper.py not yet present
# ---------------------------------------------------------------------------
try:
    from apps.routes import scalper as scalper_mod

    _MODULE_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    scalper_mod = None  # type: ignore[assignment]
    _MODULE_AVAILABLE = False

requires_scalper = pytest.mark.skipif(
    not _MODULE_AVAILABLE, reason="apps/routes/scalper.py not yet implemented"
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, query=None, method="GET", json_body=None, headers=None):
        _q = query or {}

        class _URL:
            class _Query(dict):
                pass

            query = _Query(_q)

        self.rel_url = _URL()
        self.method = method
        self._json = json_body if json_body is not None else {}
        self.headers = headers or {}

    async def json(self):
        return self._json


def _body(resp):
    raw = resp.body if hasattr(resp, "body") else resp.text
    if isinstance(raw, bytes):
        return json.loads(raw.decode())
    return json.loads(raw)


def _patch_api(monkeypatch, *, cache_val=None):
    import apps.api as api_mod

    monkeypatch.setattr(api_mod, "_cache_get", lambda key, ttl: cache_val, raising=False)
    monkeypatch.setattr(api_mod, "_cache_set", lambda key, val: None, raising=False)

    async def _fake_db_query(fn):
        return fn()

    monkeypatch.setattr(api_mod, "_db_query", _fake_db_query, raising=False)
    return api_mod


def _engine_from_conn(mock_conn):
    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_conn
    return mock_engine


def _engine_raises(exc):
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.side_effect = exc
    return _engine_from_conn(mock_conn)


def _conn_with_sequence(fetchone_seq, fetchall_seq=None):
    if fetchall_seq is None:
        fetchall_seq = [[] for _ in fetchone_seq]
    call_idx = [0]

    def _execute(*args, **kwargs):
        i = call_idx[0]
        m = MagicMock()
        m.fetchone.return_value = fetchone_seq[i] if i < len(fetchone_seq) else None
        m.fetchall.return_value = fetchall_seq[i] if i < len(fetchall_seq) else []
        call_idx[0] += 1
        return m

    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.execute.side_effect = _execute
    return conn


def _make_cfg(**overrides):
    import config as cfg_mod
    base = cfg_mod.Config()
    d = dict(base)
    d.update(overrides)
    return types.SimpleNamespace(**d)


# ===========================================================================
# 1. GET /api/scalper/live
# ===========================================================================

@requires_scalper
def test_live_scalper_off(monkeypatch):
    """No hb["scalper"] → 200, ok:True, available:False (safe shell)."""
    import apps.api as api_mod
    monkeypatch.setattr(api_mod, "read_heartbeat", lambda: ({}, False), raising=False)
    resp = asyncio.run(scalper_mod.scalper_live_handler(_FakeRequest()))
    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["available"] is False
    assert body["state"] is None


@requires_scalper
def test_live_scalper_on(monkeypatch):
    """hb["scalper"] present → available:True with the state echoed back."""
    import apps.api as api_mod
    state = {"direction": "LONG", "position_lots": 2, "state": "ACTIVE"}
    monkeypatch.setattr(api_mod, "read_heartbeat",
                        lambda: ({"scalper": state}, True), raising=False)
    resp = asyncio.run(scalper_mod.scalper_live_handler(_FakeRequest()))
    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["available"] is True
    assert body["state"]["direction"] == "LONG"


@requires_scalper
def test_live_error(monkeypatch):
    """read_heartbeat raising → safe shell, ok:False, no exception propagated."""
    import apps.api as api_mod

    def _boom():
        raise RuntimeError("hb read failed")

    monkeypatch.setattr(api_mod, "read_heartbeat", _boom, raising=False)
    resp = asyncio.run(scalper_mod.scalper_live_handler(_FakeRequest()))
    body = _body(resp)
    assert body["ok"] is False
    assert body["available"] is False


# ===========================================================================
# 2. GET /api/scalper/trades
# ===========================================================================

@requires_scalper
def test_trades_empty(monkeypatch):
    """Empty table → 200, zeroed summary, empty list. Must not raise."""
    _patch_api(monkeypatch)
    conn = _conn_with_sequence(fetchone_seq=[(0, 0, 0.0, 0)], fetchall_seq=[[], []])
    with patch("db.get_engine", return_value=_engine_from_conn(conn)):
        resp = asyncio.run(scalper_mod.scalper_trades_handler(_FakeRequest()))
    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["summary"]["closed"] == 0
    assert body["summary"]["open"] == 0
    assert body["summary"]["net_pnl"] == 0.0
    assert body["summary"]["win_rate"] == 0.0
    assert body["trades"] == []


@requires_scalper
def test_trades_happy(monkeypatch):
    """Summary + trade list populated; net-of-charges net_pnl surfaced."""
    _patch_api(monkeypatch)
    summary = (2, 1, 1500.0, 1)  # closed, open, net, wins
    trade_rows = [
        # id, symbol, trade_date, status, direction, option_type, strike, lots,
        # entry_time, entry_premium, entry_underlying, entry_reason,
        # exit_time, exit_premium, exit_reason, net_pnl, win
        (1, "NIFTY", "2026-06-20", "CLOSED", "LONG", "CE", 24000, 1,
         None, 120.0, 24010.0, "Ladder open", None, 145.0, "TP[0]", 1625.0, True),
        (2, "NIFTY", "2026-06-20", "CLOSED", "SHORT", "PE", 23900, 1,
         None, 110.0, 23890.0, "Ladder open", None, 108.0, "Time-stop", -130.0, False),
    ]
    conn = _conn_with_sequence(fetchone_seq=[summary], fetchall_seq=[[], trade_rows])
    with patch("db.get_engine", return_value=_engine_from_conn(conn)):
        resp = asyncio.run(scalper_mod.scalper_trades_handler(_FakeRequest(query={"limit": "50"})))
    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["summary"]["closed"] == 2
    assert body["summary"]["wins"] == 1
    assert body["summary"]["win_rate"] == 0.5
    assert body["summary"]["net_pnl"] == 1500.0
    assert len(body["trades"]) == 2
    assert body["trades"][0]["net_pnl"] == 1625.0
    assert body["trades"][0]["option_type"] == "CE"


@requires_scalper
def test_trades_db_error(monkeypatch):
    """DB exception → ok:False safe shell."""
    _patch_api(monkeypatch)
    with patch("db.get_engine", return_value=_engine_raises(RuntimeError("DB down"))):
        resp = asyncio.run(scalper_mod.scalper_trades_handler(_FakeRequest()))
    body = _body(resp)
    assert body["ok"] is False
    assert body["summary"]["closed"] == 0
    assert body["trades"] == []


# ===========================================================================
# 3. GET /api/scalper/governor
# ===========================================================================

@requires_scalper
def test_governor_empty(monkeypatch):
    """No row for today → 200, available:False safe shell. Must not raise."""
    _patch_api(monkeypatch)
    conn = _conn_with_sequence(fetchone_seq=[None])
    with patch("db.get_engine", return_value=_engine_from_conn(conn)):
        resp = asyncio.run(scalper_mod.scalper_governor_handler(_FakeRequest()))
    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["available"] is False
    assert body["totals"]["realized_pnl"] == 0.0


@requires_scalper
def test_governor_happy(monkeypatch):
    """Row present → limits + totals surfaced, available:True."""
    _patch_api(monkeypatch)
    # trade_date, state, daily_loss_cap, profit_lock_arm, profit_lock_giveback,
    # profit_lock_mode, max_trades, consec_loss_limit,
    # realized_pnl, trades_today, consec_losses,
    # profit_floor, profit_hi_water, lock_armed
    row = ("2026-06-20", "LOCKED", 8000.0, 6000.0, 2500.0,
           "trail_only", 8, 3,
           6500.0, 3, 1,
           4000.0, 6500.0, True)
    conn = _conn_with_sequence(fetchone_seq=[row])
    with patch("db.get_engine", return_value=_engine_from_conn(conn)):
        resp = asyncio.run(scalper_mod.scalper_governor_handler(_FakeRequest()))
    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["available"] is True
    assert body["state"] == "LOCKED"
    assert body["limits"]["daily_loss_cap"] == 8000.0
    assert body["limits"]["max_trades"] == 8
    assert body["totals"]["realized_pnl"] == 6500.0
    assert body["totals"]["lock_armed"] is True


@requires_scalper
def test_governor_uses_ist_date(monkeypatch):
    """The governor query must bind the IST trading day (not naive/UTC date).

    Clock-frozen at a UTC instant that is the PREVIOUS calendar day in UTC but
    the CURRENT day in IST (UTC+5:30). A handler that used date.today()/naive
    now() would bind the UTC date and FAIL this assertion at any hour — this
    makes the [[ci-ist-date-trap]] coverage watertight, not time-of-day-lucky.
    """
    _patch_api(monkeypatch)
    import datetime as _dt

    captured = {}

    def _execute(stmt, params=None):
        captured["params"] = params
        m = MagicMock()
        m.fetchone.return_value = None
        return m

    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.execute.side_effect = _execute

    # 2026-06-20 22:00 UTC  →  2026-06-21 03:30 IST. UTC date and IST date differ.
    frozen_utc = _dt.datetime(2026, 6, 20, 22, 0, 0, tzinfo=_dt.timezone.utc)
    ist_date = _dt.date(2026, 6, 21)
    utc_date = _dt.date(2026, 6, 20)
    assert ist_date != utc_date  # fixture sanity

    class _FrozenDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_utc.astimezone(tz) if tz is not None else frozen_utc.replace(tzinfo=None)

    monkeypatch.setattr(scalper_mod, "datetime", _FrozenDateTime)
    with patch("db.get_engine", return_value=_engine_from_conn(conn)):
        asyncio.run(scalper_mod.scalper_governor_handler(_FakeRequest()))

    # Must be the IST date, never the UTC date.
    assert captured["params"]["td"] == ist_date
    assert captured["params"]["td"] != utc_date


@requires_scalper
def test_governor_db_error(monkeypatch):
    """DB exception → ok:False safe shell."""
    _patch_api(monkeypatch)
    with patch("db.get_engine", return_value=_engine_raises(RuntimeError("boom"))):
        resp = asyncio.run(scalper_mod.scalper_governor_handler(_FakeRequest()))
    body = _body(resp)
    assert body["ok"] is False
    assert body["available"] is False


# ===========================================================================
# 4. POST /api/scalper/control
# ===========================================================================

@requires_scalper
def test_control_token_set_no_header_rejected(monkeypatch):
    """DASHBOARD_TOKEN set + no header → 401, no flag written."""
    import apps.api as api_mod
    monkeypatch.setattr(api_mod, "cfg", _make_cfg(dashboard_token="secret123"))
    monkeypatch.setattr(api_mod, "_unprotected_warned", False)
    resp = asyncio.run(scalper_mod.scalper_control_handler(
        _FakeRequest(method="POST", json_body={"action": "start"}, headers={})))
    assert resp.status == 401


@requires_scalper
def test_control_token_set_wrong_token_rejected(monkeypatch):
    """DASHBOARD_TOKEN set + wrong token → 401."""
    import apps.api as api_mod
    monkeypatch.setattr(api_mod, "cfg", _make_cfg(dashboard_token="secret123"))
    monkeypatch.setattr(api_mod, "_unprotected_warned", False)
    req = _FakeRequest(method="POST", json_body={"action": "stop"},
                       headers={"X-Dashboard-Token": "WRONG"})
    resp = asyncio.run(scalper_mod.scalper_control_handler(req))
    assert resp.status == 401


@requires_scalper
def test_control_valid_token_writes_start_flag(tmp_path, monkeypatch):
    """Valid token → 200 and run/scalper_start written, scalper_stop cleared."""
    import apps.api as api_mod
    monkeypatch.setattr(api_mod, "RUN_DIR", tmp_path)
    monkeypatch.setattr(api_mod, "cfg", _make_cfg(dashboard_token="secret123"))
    monkeypatch.setattr(api_mod, "_unprotected_warned", False)
    (tmp_path / "scalper_stop").write_text("stale")  # should be cleared
    req = _FakeRequest(method="POST", json_body={"action": "start"},
                       headers={"X-Dashboard-Token": "secret123"})
    resp = asyncio.run(scalper_mod.scalper_control_handler(req))
    assert resp.status == 200
    assert (tmp_path / "scalper_start").exists()
    assert not (tmp_path / "scalper_stop").exists()


@requires_scalper
def test_control_writes_stop_flag(tmp_path, monkeypatch):
    """action=stop writes run/scalper_stop, clears scalper_start."""
    import apps.api as api_mod
    monkeypatch.setattr(api_mod, "RUN_DIR", tmp_path)
    monkeypatch.setattr(api_mod, "cfg", _make_cfg(dashboard_token=""))  # unset = fail-open
    monkeypatch.setattr(api_mod, "_unprotected_warned", False)
    (tmp_path / "scalper_start").write_text("stale")
    req = _FakeRequest(method="POST", json_body={"action": "stop"})
    resp = asyncio.run(scalper_mod.scalper_control_handler(req))
    assert resp.status == 200
    assert (tmp_path / "scalper_stop").exists()
    assert not (tmp_path / "scalper_start").exists()


@requires_scalper
def test_control_bad_action(tmp_path, monkeypatch):
    """Unknown action → 400, no flag written (token unset = fail-open path)."""
    import apps.api as api_mod
    monkeypatch.setattr(api_mod, "RUN_DIR", tmp_path)
    monkeypatch.setattr(api_mod, "cfg", _make_cfg(dashboard_token=""))
    monkeypatch.setattr(api_mod, "_unprotected_warned", False)
    req = _FakeRequest(method="POST", json_body={"action": "frobnicate"})
    resp = asyncio.run(scalper_mod.scalper_control_handler(req))
    assert resp.status == 400
    assert not (tmp_path / "scalper_start").exists()
    assert not (tmp_path / "scalper_stop").exists()
