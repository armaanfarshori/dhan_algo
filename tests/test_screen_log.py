"""Tests for the daily screened-stock audit log (backend).

Covers:
  - core.screen_log.record_daily_screen      (upsert idempotency + non-fatal)
  - apps.routes.db._safe_date                 (date-param validation)
  - apps.routes.db.screen_handler             (returns rows / 400 on bad date)
  - apps.routes.db.screen_days_handler        (returns distinct days)

All DB I/O is monkeypatched — no real database is touched.
"""
import asyncio
import json
from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock

# ── Minimal fake aiohttp.web.Request (mirrors test_route_handlers.py) ──────────

class _FakeRequest:
    def __init__(self, query=None):
        _q = query or {}

        class _URL:
            class _Query(dict):
                pass
            query = _Query(_q)

        self.rel_url = _URL()
        self.method = "GET"
        self.headers = {}


def _body(resp) -> dict:
    raw = resp.body if hasattr(resp, "body") else resp.text
    if isinstance(raw, bytes):
        return json.loads(raw.decode())
    return json.loads(raw)


def _patch_db_query(monkeypatch):
    """Make apps.api._db_query run the callable in-process (no thread)."""
    import apps.api as api_mod

    async def _fake_db_query(fn):
        return fn()

    monkeypatch.setattr(api_mod, "_db_query", _fake_db_query, raising=False)


# ═══════════════════════════════════════════════════════════════════════════════
# record_daily_screen — upsert + idempotency
# ═══════════════════════════════════════════════════════════════════════════════

@contextmanager
def _fake_session(captured):
    """Yield a mock session that records every execute() params dict."""
    session = MagicMock()

    def _execute(stmt, params=None):
        captured.append((str(stmt), params))
        return MagicMock()

    session.execute.side_effect = _execute
    yield session


def _run_record(monkeypatch, **kwargs):
    captured = []
    monkeypatch.setattr("db.get_session", lambda: _fake_session(captured))
    from core.screen_log import record_daily_screen
    n = record_daily_screen(**kwargs)
    return n, captured


def test_record_writes_one_row_per_candidate(monkeypatch):
    cands = [
        {"security_id": "11", "score": 0.03, "avg_volume": 100000,
         "avg_close": 250.0, "reason": "ATR%=3.00%"},
        {"security_id": "22", "score": 0.02, "avg_volume": 50000,
         "avg_close": 99.0, "reason": "ATR%=2.00%"},
    ]
    n, captured = _run_record(monkeypatch, candidates=cands,
                              trading_day=date(2026, 6, 18))
    assert n == 2
    assert len(captured) == 2
    # rank derived from list order; volume from avg_volume; price from avg_close
    p0 = captured[0][1]
    assert p0["security_id"] == "11"
    assert p0["rank"] == 1
    assert p0["score"] == 0.03
    assert p0["volume"] == 100000
    assert p0["price"] == 250.0
    assert p0["selected"] is True
    assert p0["trading_day"] == date(2026, 6, 18)


def test_record_uses_upsert_on_conflict(monkeypatch):
    """The statement must be an ON CONFLICT upsert keyed on (day, security_id)."""
    n, captured = _run_record(
        monkeypatch,
        candidates=[{"security_id": "11", "score": 0.01}],
        trading_day=date(2026, 6, 18),
    )
    assert n == 1
    stmt = captured[0][0]
    assert "ON CONFLICT (trading_day, security_id) DO UPDATE" in stmt


def test_record_idempotent_same_params_on_rerun(monkeypatch):
    """Re-running a day issues the SAME upsert params — DB collapses to one row."""
    cands = [{"security_id": "11", "score": 0.05, "avg_close": 300.0}]
    n1, cap1 = _run_record(monkeypatch, candidates=cands, trading_day=date(2026, 6, 18))
    n2, cap2 = _run_record(monkeypatch, candidates=cands, trading_day=date(2026, 6, 18))
    assert n1 == n2 == 1
    # identical bound params → ON CONFLICT updates the existing row, not a dup
    assert cap1[0][1] == cap2[0][1]


def test_record_selected_ids_marks_only_chosen(monkeypatch):
    cands = [
        {"security_id": "11", "score": 0.03},
        {"security_id": "22", "score": 0.02},
        {"security_id": "33", "score": 0.01},
    ]
    n, captured = _run_record(monkeypatch, candidates=cands,
                              selected_ids=["11", "33"],
                              trading_day=date(2026, 6, 18))
    assert n == 3
    sel = {c[1]["security_id"]: c[1]["selected"] for c in captured}
    assert sel == {"11": True, "22": False, "33": True}


def test_record_skips_candidates_without_security_id(monkeypatch):
    cands = [{"score": 0.03}, {"security_id": "22", "score": 0.02}]
    n, captured = _run_record(monkeypatch, candidates=cands,
                              trading_day=date(2026, 6, 18))
    assert n == 1
    assert captured[0][1]["security_id"] == "22"


def test_record_empty_is_noop(monkeypatch):
    n, captured = _run_record(monkeypatch, candidates=[], trading_day=date(2026, 6, 18))
    assert n == 0
    assert captured == []


def test_record_non_fatal_on_db_error(monkeypatch):
    """A DB failure must be swallowed (returns 0) — never breaks the screener."""
    def _boom():
        raise RuntimeError("DB down")
    monkeypatch.setattr("db.get_session", _boom)
    from core.screen_log import record_daily_screen
    n = record_daily_screen([{"security_id": "11", "score": 0.01}],
                            trading_day=date(2026, 6, 18))
    assert n == 0


# ═══════════════════════════════════════════════════════════════════════════════
# _safe_date — date-param validation
# ═══════════════════════════════════════════════════════════════════════════════

def test_safe_date_valid():
    from apps.routes.db import _safe_date
    assert _safe_date("2026-06-18") == date(2026, 6, 18)
    assert _safe_date("  2026-06-18  ") == date(2026, 6, 18)


def test_safe_date_rejects_malformed():
    from apps.routes.db import _safe_date
    for bad in ["", "not-a-date", "2026-13-99", "2026/06/18",
                "18-06-2026", "1; DROP TABLE daily_screen", "2026-06-18'"]:
        assert _safe_date(bad) is None


# ═══════════════════════════════════════════════════════════════════════════════
# screen_handler / screen_days_handler
# ═══════════════════════════════════════════════════════════════════════════════

def test_screen_handler_returns_rows(monkeypatch):
    _patch_db_query(monkeypatch)
    sample = [{"security_id": "11", "ticker": "FOO", "rank": 1,
               "score": 0.03, "price": 250.0, "volume": 100000,
               "selected": True, "reason": "ATR%=3.00%",
               "trading_day": "2026-06-18", "created_at": "2026-06-18T10:00:00"}]
    monkeypatch.setattr("core.screen_log.get_screen_for_day", lambda d: sample)

    resp = asyncio.run(screen_call({"date": "2026-06-18"}))
    body = _body(resp)
    assert body["ok"] is True
    assert body["date"] == "2026-06-18"
    assert body["count"] == 1
    assert body["rows"][0]["security_id"] == "11"


def test_screen_handler_defaults_to_today(monkeypatch):
    _patch_db_query(monkeypatch)
    captured = {}

    def _fake(d):
        captured["day"] = d
        return []

    monkeypatch.setattr("core.screen_log.get_screen_for_day", _fake)
    resp = asyncio.run(screen_call({}))   # no date param
    body = _body(resp)
    assert body["ok"] is True
    assert isinstance(captured["day"], date)   # defaulted to today IST


def test_screen_handler_rejects_bad_date(monkeypatch):
    _patch_db_query(monkeypatch)
    resp = asyncio.run(screen_call({"date": "garbage"}))
    assert resp.status == 400
    assert _body(resp)["ok"] is False


def test_screen_days_handler_returns_days(monkeypatch):
    _patch_db_query(monkeypatch)
    monkeypatch.setattr("core.screen_log.list_screen_days",
                        lambda: ["2026-06-18", "2026-06-17"])
    from apps.routes.db import screen_days_handler
    resp = asyncio.run(screen_days_handler(_FakeRequest()))
    body = _body(resp)
    assert body["ok"] is True
    assert body["count"] == 2
    assert body["days"] == ["2026-06-18", "2026-06-17"]


def screen_call(query):
    """Invoke screen_handler with a fake request carrying `query`."""
    from apps.routes.db import screen_handler
    return screen_handler(_FakeRequest(query))
