"""Contract tests for the dashboard API — shape regression guard.

These tests assert the exact response shapes that the React dashboard depends
on so that field-name renames or structural changes are caught before they
silently break the frontend.

Endpoints covered:
  /api/snapshot  — trader.risk.total_pnl, trader.risk.halted,
                   trader.kronos_gate, trader.ts, limits.max_open_positions
  /api/trades    — summary.closed_today, summary.wins_today
  /api/rate-limits — categories mapping with total/per_day/by_process
  /api/equity    — intraday list of {t, pnl}

The test server is a real aiohttp TestServer driven by the same build_app()
factory used in production. DB calls and heartbeat reads are patched to avoid
any real I/O.
"""
import asyncio
import json
import pytest
from unittest.mock import MagicMock, patch

import aiohttp
from aiohttp.test_utils import TestServer, TestClient

import apps.api as api_mod
from apps.api import build_app


# ── Shared fixtures ────────────────────────────────────────────────────────────

_FAKE_RISK = {
    "realised_pnl": 120.50,
    "unrealised_pnl": -30.25,
    "total_pnl": 90.25,
    "week_pnl": 90.25,
    "equity": 100090.25,
    "daily_loss_budget": 2000.0,
    "weekly_loss_budget": 5000.0,
    "committed_risk": 500.0,
    "open_positions": 1,
    "halted": False,
    "halt_reason": None,
    "halt_scope": None,
    "violations": [],
    "last_checked": "2026-06-16T09:30:00+00:00",
}

_FAKE_HB = {
    "ts": "2026-06-16T09:30:00+00:00",
    "pid": 1234,
    "mode": "PAPER",
    "uptime_seconds": 3600,
    "kronos_gate": "shadow",
    "watchlist": ["1333"],
    "names": {"1333": "RELIANCE"},
    "strategies": [
        {
            "security_id": "1333",
            "ticker": "RELIANCE",
            "running": True,
            "position": 1,
            "entry_price": 2800.0,
            "entries_today": 1,
        }
    ],
    "portfolio": {
        "open_positions": [{"security_id": "1333", "qty": 5, "avg_price": 2800.0, "strategy": "ORB"}],
        "realized_pnl": 120.50,
        "unrealized_pnl": -30.25,
    },
    "risk": _FAKE_RISK,
    "feed": {"connected": True, "subscribed": 1},
    "bars": {},
    "kronos_scanner": None,
    "rate_limits": {},
}


def _fake_heartbeat_alive():
    return _FAKE_HB, True


def _fake_heartbeat_stale():
    return {}, False


# ── aiohttp client helpers ─────────────────────────────────────────────────────

def _run(coro):
    """Run an async coroutine synchronously (one-shot)."""
    return asyncio.get_event_loop().run_until_complete(coro)


async def _make_client(monkeypatch):
    """Build a TestClient for the full aiohttp app with all external I/O patched."""
    # Patch heartbeat reader at the api module level
    monkeypatch.setattr(api_mod, "read_heartbeat", _fake_heartbeat_alive)

    app = build_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


# ── /api/snapshot contract ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_shape(monkeypatch):
    """/api/snapshot must expose the fields the dashboard fast-poll loop reads."""
    monkeypatch.setattr(api_mod, "read_heartbeat", _fake_heartbeat_alive)

    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/snapshot")
        assert resp.status == 200
        data = await resp.json()

    # Top-level presence
    assert data["ok"] is True
    assert "ts" in data
    assert "trader" in data
    assert "limits" in data

    trader = data["trader"]

    # trader.ts — dashboard renders the heartbeat timestamp
    assert "ts" in trader

    # trader.kronos_gate must be lowercase 'shadow' or 'enforcing'
    assert trader["kronos_gate"] in ("shadow", "enforcing"), (
        f"kronos_gate must be 'shadow' or 'enforcing', got {trader['kronos_gate']!r}"
    )

    # trader.risk.total_pnl and trader.risk.halted
    risk = trader.get("risk")
    assert risk is not None, "trader.risk is missing from /api/snapshot"
    assert "total_pnl" in risk, f"trader.risk.total_pnl missing; got keys: {list(risk)}"
    assert "halted" in risk, f"trader.risk.halted missing; got keys: {list(risk)}"

    # limits.max_open_positions — used in the System tab
    limits = data["limits"]
    assert "max_open_positions" in limits, (
        f"limits.max_open_positions missing; got keys: {list(limits)}"
    )


@pytest.mark.asyncio
async def test_snapshot_kronos_gate_is_lowercase(monkeypatch):
    """Regression: kronos_gate must never be 'SHADOW' or 'ENFORCING' (uppercase)."""
    monkeypatch.setattr(api_mod, "read_heartbeat", _fake_heartbeat_alive)

    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/snapshot")
        data = await resp.json()

    gate = data["trader"]["kronos_gate"]
    assert gate == gate.lower(), (
        f"kronos_gate must be lowercase; got {gate!r}"
    )


# ── /api/trades contract ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trades_summary_shape(monkeypatch):
    """/api/trades summary must have closed_today and wins_today.

    profit_factor is computed client-side in the dashboard so we do NOT require
    it here.
    """
    monkeypatch.setattr(api_mod, "read_heartbeat", _fake_heartbeat_alive)

    # Patch the DB query that trades_handler runs in an executor
    fake_rows = []
    fake_summary = (3, 450.75, 2)  # closed, pnl_sum, wins

    def _fake_db_query(fn):
        # trades_handler calls _db_query once and unpacks (rows, summary)
        async def _inner():
            return fake_rows, fake_summary
        return _inner()

    monkeypatch.setattr(api_mod, "_db_query", _fake_db_query)

    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/trades")
        assert resp.status == 200
        data = await resp.json()

    assert data["ok"] is True
    summary = data.get("summary")
    assert summary is not None, "trades response missing 'summary' key"

    assert "closed_today" in summary, (
        f"summary.closed_today missing; got keys: {list(summary)}"
    )
    assert "wins_today" in summary, (
        f"summary.wins_today missing; got keys: {list(summary)}"
    )

    # Values must match what the DB returned
    assert summary["closed_today"] == 3
    assert summary["wins_today"] == 2


# ── /api/rate-limits contract ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limits_shape(monkeypatch):
    """/api/rate-limits categories must have total, per_day, and by_process."""
    monkeypatch.setattr(api_mod, "read_heartbeat", _fake_heartbeat_alive)

    fake_totals = {
        "date": "2026-06-16",
        "categories": {
            "orders":      {"total": 42,    "per_day": 7000,    "by_process": {"trader": 42}},
            "data":        {"total": 88200, "per_day": 100_000, "by_process": {"trader": 1200, "backfill": 87000}},
            "quote":       {"total": 0,     "per_day": None,    "by_process": {}},
            "non_trading": {"total": 0,     "per_day": None,    "by_process": {}},
        },
    }

    def _fake_db_query(fn):
        async def _inner():
            return fake_totals
        return _inner()

    monkeypatch.setattr(api_mod, "_db_query", _fake_db_query)

    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/rate-limits")
        assert resp.status == 200
        data = await resp.json()

    assert data["ok"] is True
    categories = data.get("categories")
    assert categories is not None, "rate-limits response missing 'categories'"
    assert len(categories) > 0, "categories must be non-empty"

    for cat_name, cat in categories.items():
        assert "total" in cat, (
            f"categories.{cat_name} missing 'total'; got keys: {list(cat)}"
        )
        assert "per_day" in cat, (
            f"categories.{cat_name} missing 'per_day'; got keys: {list(cat)}"
        )
        assert "by_process" in cat, (
            f"categories.{cat_name} missing 'by_process'; got keys: {list(cat)}"
        )


# ── /api/equity contract ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_equity_intraday_shape(monkeypatch):
    """/api/equity must return an 'intraday' list of {t, pnl} objects."""
    monkeypatch.setattr(api_mod, "read_heartbeat", _fake_heartbeat_alive)

    fake_equity = {
        "ok": True,
        "intraday": [
            {"t": "09:15", "pnl": 0.0,   "equity": 100000.0},
            {"t": "09:30", "pnl": 120.5,  "equity": 100120.5},
            {"t": "09:45", "pnl": 90.25,  "equity": 100090.25},
        ],
    }

    # equity_handler checks cache then calls _db_query; patch both
    import apps.routes.db as db_routes

    def _fake_db_query(fn):
        async def _inner():
            return fake_equity
        return _inner()

    # Clear any cached value and patch _db_query at the api level
    api_mod._CACHE.clear()
    monkeypatch.setattr(api_mod, "_db_query", _fake_db_query)

    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/equity")
        assert resp.status == 200
        data = await resp.json()

    assert data["ok"] is True
    intraday = data.get("intraday")
    assert intraday is not None, "equity response missing 'intraday' key"
    assert isinstance(intraday, list), f"'intraday' must be a list, got {type(intraday)}"
    assert len(intraday) > 0, "intraday must be non-empty when data exists"

    # Each point must have 't' (time label) and 'pnl' (the field the chart reads)
    for i, point in enumerate(intraday):
        assert "t" in point, f"intraday[{i}] missing 't'; got keys: {list(point)}"
        assert "pnl" in point, f"intraday[{i}] missing 'pnl'; got keys: {list(point)}"


@pytest.mark.asyncio
async def test_equity_empty_on_error(monkeypatch):
    """On DB failure /api/equity must return {ok: False, intraday: []} so the
    dashboard chart can gracefully render an empty state."""
    monkeypatch.setattr(api_mod, "read_heartbeat", _fake_heartbeat_alive)

    def _failing_db_query(fn):
        async def _inner():
            raise RuntimeError("db is down")
        return _inner()

    api_mod._CACHE.clear()
    monkeypatch.setattr(api_mod, "_db_query", _failing_db_query)

    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/equity")
        data = await resp.json()

    assert data["ok"] is False
    assert "intraday" in data, "error response must still carry 'intraday' key"
    assert data["intraday"] == []
