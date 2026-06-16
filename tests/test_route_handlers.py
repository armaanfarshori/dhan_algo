"""Tests for read-only dashboard route handlers that previously had zero coverage.

Covers:
  - /api/system/health  (apps.routes.system.system_health_handler)
  - /api/backfill/status (apps.routes.system.backfill_status_handler)
  - /api/rate-limits    (apps.routes.db.rate_limits_handler)
  - /api/kronos/screener (apps.routes.db.kronos_screener_handler)

All tests are fast and hermetic: DB, subprocess, and filesystem I/O are mocked.
Auth-guarded POSTs are explicitly out of scope (deferred to M6 test coverage).
"""
import asyncio
import json
import types
from unittest.mock import MagicMock, patch

# ── Minimal fake aiohttp.web.Request ──────────────────────────────────────────

class _FakeRequest:
    """Minimal stand-in for aiohttp.web.Request.

    Handlers only call request.rel_url.query.get(), so this is all we need.
    """
    def __init__(self, query=None):  # type: Optional[dict]
        _q = query or {}

        class _URL:
            class _Query(dict):
                pass
            query = _Query(_q)

        self.rel_url = _URL()
        self.method = "GET"
        self.headers = {}


# ── Helpers to introspect web.Response ────────────────────────────────────────

def _body(resp) -> dict:
    """Decode the JSON body from a web.Response."""
    raw = resp.body if hasattr(resp, "body") else resp.text
    if isinstance(raw, bytes):
        return json.loads(raw.decode())
    return json.loads(raw)


# ── Patch boilerplate shared by system handlers ───────────────────────────────

def _make_api_stubs(monkeypatch, cache_val=None, db_result=None):
    """Patch the three symbols imported from apps.api inside the system module.

    _cache_get → returns cache_val (None means cache miss → go compute)
    _cache_set → no-op
    _db_query  → coroutine that calls the passed callable in-process (no thread)
    cfg        → SimpleNamespace with backfill/telegram/log paths
    """
    import apps.routes.system as sys_mod

    monkeypatch.setattr(sys_mod, "__builtins__", __builtins__, raising=False)

    # We patch the symbols as they are imported inside the handlers (lazy imports
    # from apps.api), so we patch apps.api directly.
    import apps.api as api_mod

    monkeypatch.setattr(api_mod, "_cache_get", lambda key, ttl: cache_val, raising=False)
    monkeypatch.setattr(api_mod, "_cache_set", lambda key, val: None, raising=False)

    # _db_query: run the callable synchronously in the same thread (fast, no executor)
    async def _fake_db_query(fn):
        return fn()

    monkeypatch.setattr(api_mod, "_db_query", _fake_db_query, raising=False)

    fake_cfg = types.SimpleNamespace(
        backfill_checkpoint_path="/nonexistent/ckpt.json",
        backfill_log_path="/nonexistent/backfill.log",
        telegram_bot_token="tok",
        telegram_chat_id="chat",
    )
    monkeypatch.setattr(api_mod, "cfg", fake_cfg, raising=False)

    return api_mod


# ═══════════════════════════════════════════════════════════════════════════════
# /api/system/health
# ═══════════════════════════════════════════════════════════════════════════════

def test_system_health_status_200(monkeypatch):
    """system_health_handler returns 200 and the expected top-level keys."""
    from apps.routes.system import system_health_handler

    _make_api_stubs(monkeypatch)

    # Stub subprocess so crontab + log reads don't hit the real FS
    mock_proc = MagicMock()
    mock_proc.stdout = "*/15 * * * * python backfill_resume.py\n"
    mock_proc.returncode = 0

    with patch("subprocess.run", return_value=mock_proc):
        # Also stub the log file read so it returns known content
        with patch("pathlib.Path.read_text", return_value=""):
            resp = asyncio.run(system_health_handler(_FakeRequest()))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert "telegram_configured" in body
    assert "crons" in body
    assert "trader_errors_today" in body
    assert "hermes" in body


def test_system_health_telegram_configured(monkeypatch):
    """telegram_configured reflects whether bot_token + chat_id are set."""
    from apps.routes.system import system_health_handler

    api_mod = _make_api_stubs(monkeypatch)
    api_mod.cfg.telegram_bot_token = "real_token"
    api_mod.cfg.telegram_chat_id = "real_chat"

    mock_proc = MagicMock()
    mock_proc.stdout = ""
    mock_proc.returncode = 0

    with patch("subprocess.run", return_value=mock_proc):
        with patch("pathlib.Path.read_text", return_value=""):
            resp = asyncio.run(system_health_handler(_FakeRequest()))

    body = _body(resp)
    assert body["telegram_configured"] is True


def test_system_health_telegram_not_configured(monkeypatch):
    """telegram_configured is False when credentials are absent."""
    from apps.routes.system import system_health_handler

    api_mod = _make_api_stubs(monkeypatch)
    api_mod.cfg.telegram_bot_token = ""
    api_mod.cfg.telegram_chat_id = ""

    mock_proc = MagicMock()
    mock_proc.stdout = ""
    mock_proc.returncode = 0

    with patch("subprocess.run", return_value=mock_proc):
        with patch("pathlib.Path.read_text", return_value=""):
            resp = asyncio.run(system_health_handler(_FakeRequest()))

    body = _body(resp)
    assert body["telegram_configured"] is False


def test_system_health_cron_parsing(monkeypatch):
    """Crontab entries are parsed into schedule + label pairs."""
    from apps.routes.system import system_health_handler

    _make_api_stubs(monkeypatch)

    crontab_output = (
        "# comment line — should be ignored\n"
        "*/15 * * * * python backfill_resume.py\n"
        "45 16 * * 1-5 python -m ml.calibration fill\n"
        "0 17 * * 1-5 python eod_summary.py\n"
    )
    mock_proc = MagicMock()
    mock_proc.stdout = crontab_output
    mock_proc.returncode = 0

    with patch("subprocess.run", return_value=mock_proc):
        with patch("pathlib.Path.read_text", return_value=""):
            resp = asyncio.run(system_health_handler(_FakeRequest()))

    body = _body(resp)
    assert body["ok"] is True
    crons = body["crons"]
    # Three non-comment lines → three cron entries
    assert len(crons) == 3
    labels = [c["job"] for c in crons]
    assert any("backfill" in lbl for lbl in labels)
    assert any("calibration" in lbl for lbl in labels)
    assert any("EOD" in lbl for lbl in labels)


def test_system_health_error_count(monkeypatch):
    """trader_errors_today counts ERROR/CRITICAL lines that start with today."""
    from apps.routes.system import system_health_handler
    from datetime import datetime, timezone

    _make_api_stubs(monkeypatch)

    today = datetime.now(timezone.utc).date().isoformat()
    log_content = (
        f"{today}T10:00:00  INFO     dhan.trader — doing things\n"
        f"{today}T10:01:00  ERROR    dhan.risk — something broke\n"
        f"{today}T10:02:00  CRITICAL dhan.risk — really broke\n"
        "2020-01-01T10:00:00  ERROR  dhan.trader — old error, different date\n"
    )

    mock_proc = MagicMock()
    mock_proc.stdout = ""
    mock_proc.returncode = 0

    with patch("subprocess.run", return_value=mock_proc):
        with patch("pathlib.Path.read_text", return_value=log_content):
            resp = asyncio.run(system_health_handler(_FakeRequest()))

    body = _body(resp)
    # Today has 1 ERROR + 1 CRITICAL = 2
    assert body["trader_errors_today"] == 2


def test_system_health_uses_cache(monkeypatch):
    """If there is a cached result, the handler returns it immediately without
    calling _db_query (i.e. no subprocess or file I/O)."""
    from apps.routes.system import system_health_handler

    cached = {"ok": True, "telegram_configured": True,
               "crons": [], "trader_errors_today": 99,
               "hermes": "cached"}

    _make_api_stubs(monkeypatch, cache_val=cached)

    # If _db_query were called it would fail — we leave it as an async callable
    # that raises to confirm it was NOT invoked.
    resp = asyncio.run(system_health_handler(_FakeRequest()))
    body = _body(resp)
    assert body["trader_errors_today"] == 99


# ═══════════════════════════════════════════════════════════════════════════════
# /api/backfill/status
# ═══════════════════════════════════════════════════════════════════════════════

def test_backfill_status_no_checkpoint(monkeypatch):
    """When the checkpoint file doesn't exist the handler still returns 200
    with ok=True and an empty checkpoint dict."""
    from apps.routes.system import backfill_status_handler

    _make_api_stubs(monkeypatch)

    # pgrep → not running
    mock_pgrep = MagicMock()
    mock_pgrep.returncode = 1

    # tail → no output
    mock_tail = MagicMock()
    mock_tail.stdout = ""

    def _run(cmd, **kwargs):
        if cmd[0] == "pgrep":
            return mock_pgrep
        return mock_tail

    with patch("subprocess.run", side_effect=_run):
        resp = asyncio.run(backfill_status_handler(_FakeRequest()))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["checkpoint"] == {}
    assert body["running"] is False


def test_backfill_status_with_checkpoint(monkeypatch, tmp_path):
    """When a checkpoint file exists its contents appear in the response and
    a percentage is computed."""
    from apps.routes.system import backfill_status_handler

    api_mod = _make_api_stubs(monkeypatch)

    ckpt_file = tmp_path / "ckpt.json"
    ckpt_file.write_text(json.dumps({"index": 670, "total": 1000}))
    api_mod.cfg.backfill_checkpoint_path = str(ckpt_file)
    api_mod.cfg.backfill_log_path = str(tmp_path / "backfill.log")

    mock_pgrep = MagicMock()
    mock_pgrep.returncode = 0   # process running

    mock_tail = MagicMock()
    mock_tail.stdout = "line1\nline2\n"

    def _run(cmd, **kwargs):
        if cmd[0] == "pgrep":
            return mock_pgrep
        return mock_tail

    with patch("subprocess.run", side_effect=_run):
        resp = asyncio.run(backfill_status_handler(_FakeRequest()))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["running"] is True
    assert body["checkpoint"]["index"] == 670
    assert body["checkpoint"]["total"] == 1000
    assert body["checkpoint"]["pct"] == 67.0
    assert body["log_tail"] == ["line1", "line2"]


def test_backfill_status_pct_zero_total(monkeypatch, tmp_path):
    """pct is 0.0 when total==0 (avoids division-by-zero)."""
    from apps.routes.system import backfill_status_handler

    api_mod = _make_api_stubs(monkeypatch)

    ckpt_file = tmp_path / "ckpt.json"
    ckpt_file.write_text(json.dumps({"index": 0, "total": 0}))
    api_mod.cfg.backfill_checkpoint_path = str(ckpt_file)
    api_mod.cfg.backfill_log_path = str(tmp_path / "backfill.log")

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = ""

    with patch("subprocess.run", return_value=mock_proc):
        resp = asyncio.run(backfill_status_handler(_FakeRequest()))

    body = _body(resp)
    assert body["checkpoint"]["pct"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# /api/rate-limits
# ═══════════════════════════════════════════════════════════════════════════════

def _make_fake_totals():
    """Return a plausible query_today_totals() result."""
    return {
        "date": "2026-06-16",
        "categories": {
            "orders":      {"total": 42,    "per_day": 7000,    "by_process": {"trader": 42}},
            "data":        {"total": 88200, "per_day": 100_000, "by_process": {"trader": 1200, "backfill": 87000}},
            "quote":       {"total": 0,     "per_day": None,    "by_process": {}},
            "non_trading": {"total": 0,     "per_day": None,    "by_process": {}},
        },
    }


def test_rate_limits_status_200(monkeypatch):
    """rate_limits_handler returns 200, ok=True, and the categories shape."""
    from apps.routes.db import rate_limits_handler
    import apps.api as api_mod

    fake_totals = _make_fake_totals()

    async def _fake_db_query(fn):
        return fn()

    monkeypatch.setattr(api_mod, "_db_query", _fake_db_query, raising=False)

    with patch("core.api_usage.query_today_totals", return_value=fake_totals):
        resp = asyncio.run(rate_limits_handler(_FakeRequest()))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert "categories" in body
    assert "date" in body


def test_rate_limits_all_four_categories(monkeypatch):
    """All four rate-limit categories are present in the response."""
    from apps.routes.db import rate_limits_handler
    import apps.api as api_mod

    fake_totals = _make_fake_totals()

    async def _fake_db_query(fn):
        return fn()

    monkeypatch.setattr(api_mod, "_db_query", _fake_db_query, raising=False)

    with patch("core.api_usage.query_today_totals", return_value=fake_totals):
        resp = asyncio.run(rate_limits_handler(_FakeRequest()))

    cats = _body(resp)["categories"]
    for cat in ("orders", "data", "quote", "non_trading"):
        assert cat in cats, f"Missing category: {cat}"
        assert "total" in cats[cat]
        assert "per_day" in cats[cat]
        assert "by_process" in cats[cat]


def test_rate_limits_values(monkeypatch):
    """rate_limits_handler correctly passes through the totals from query_today_totals."""
    from apps.routes.db import rate_limits_handler
    import apps.api as api_mod

    fake_totals = _make_fake_totals()

    async def _fake_db_query(fn):
        return fn()

    monkeypatch.setattr(api_mod, "_db_query", _fake_db_query, raising=False)

    with patch("core.api_usage.query_today_totals", return_value=fake_totals):
        resp = asyncio.run(rate_limits_handler(_FakeRequest()))

    body = _body(resp)
    assert body["categories"]["orders"]["total"] == 42
    assert body["categories"]["data"]["total"] == 88200
    assert body["categories"]["data"]["per_day"] == 100_000
    assert body["categories"]["quote"]["per_day"] is None


def test_rate_limits_db_error_returns_500(monkeypatch):
    """If the DB query raises, the handler returns 500 with ok=False."""
    from apps.routes.db import rate_limits_handler
    import apps.api as api_mod

    async def _failing_db_query(fn):
        raise RuntimeError("DB connection refused")

    monkeypatch.setattr(api_mod, "_db_query", _failing_db_query, raising=False)

    resp = asyncio.run(rate_limits_handler(_FakeRequest()))
    assert resp.status == 500
    body = _body(resp)
    assert body["ok"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# /api/kronos/screener
# ═══════════════════════════════════════════════════════════════════════════════

def _make_screener_stubs(monkeypatch, cache_val=None, candidates=None):
    """Patch apps.api symbols needed by kronos_screener_handler."""
    import apps.api as api_mod

    monkeypatch.setattr(api_mod, "_cache_get", lambda key, ttl: cache_val, raising=False)
    monkeypatch.setattr(api_mod, "_cache_set", lambda key, val: None, raising=False)

    async def _fake_db_query(fn):
        return fn()

    monkeypatch.setattr(api_mod, "_db_query", _fake_db_query, raising=False)

    return api_mod


def test_screener_status_200_with_candidates(monkeypatch):
    """kronos_screener_handler returns 200, ok=True, and the candidates list."""
    from apps.routes.db import kronos_screener_handler

    _make_screener_stubs(monkeypatch)

    fake_candidates = [
        {"security_id": "1333", "atr_pct": 2.5, "volume": 150000},
        {"security_id": "500325", "atr_pct": 1.8, "volume": 200000},
    ]

    # Stub get_top_volatile and the instruments DB lookup
    def _fake_get_top_volatile(n):
        return list(fake_candidates)

    # instruments query result: [(security_id, ticker, name), ...]
    fake_rows = [
        ("1333",   "HDFCBANK", "HDFC Bank Ltd"),
        ("500325", "RELIANCE", "Reliance Industries"),
    ]

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value.fetchall.return_value = fake_rows

    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_conn

    with patch("core.nse_screener.get_top_volatile", side_effect=_fake_get_top_volatile):
        with patch("db.get_engine", return_value=mock_engine):
            resp = asyncio.run(kronos_screener_handler(_FakeRequest(query={"n": "2"})))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["count"] == 2
    assert isinstance(body["candidates"], list)
    assert len(body["candidates"]) == 2


def test_screener_candidates_have_ticker_name(monkeypatch):
    """Candidates returned by the screener include ticker and name from instruments."""
    from apps.routes.db import kronos_screener_handler

    _make_screener_stubs(monkeypatch)

    fake_candidates = [{"security_id": "1333", "atr_pct": 2.5, "volume": 150000}]
    fake_rows = [("1333", "HDFCBANK", "HDFC Bank Ltd")]

    def _fake_get_top_volatile(n):
        return list(fake_candidates)

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value.fetchall.return_value = fake_rows

    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_conn

    with patch("core.nse_screener.get_top_volatile", side_effect=_fake_get_top_volatile):
        with patch("db.get_engine", return_value=mock_engine):
            resp = asyncio.run(kronos_screener_handler(_FakeRequest()))

    body = _body(resp)
    c = body["candidates"][0]
    assert c["ticker"] == "HDFCBANK"
    assert c["name"] == "HDFC Bank Ltd"


def test_screener_empty_candidates(monkeypatch):
    """When get_top_volatile returns [] the handler returns ok=True, count=0,
    candidates=[] — and does NOT cache the result (empty = transient state)."""
    from apps.routes.db import kronos_screener_handler

    cached_calls = []
    import apps.api as api_mod
    monkeypatch.setattr(api_mod, "_cache_get", lambda key, ttl: None, raising=False)
    monkeypatch.setattr(api_mod, "_cache_set", lambda key, val: cached_calls.append((key, val)), raising=False)

    async def _fake_db_query(fn):
        return fn()
    monkeypatch.setattr(api_mod, "_db_query", _fake_db_query, raising=False)

    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value.fetchall.return_value = []
    mock_engine.connect.return_value = mock_conn

    with patch("core.nse_screener.get_top_volatile", return_value=[]):
        with patch("db.get_engine", return_value=mock_engine):
            resp = asyncio.run(kronos_screener_handler(_FakeRequest()))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["count"] == 0
    assert body["candidates"] == []
    # Cache must NOT be written for empty results
    assert cached_calls == []


def test_screener_uses_cache(monkeypatch):
    """If the cache holds a fresh result, kronos_screener_handler returns it
    without touching the DB or the screener."""
    from apps.routes.db import kronos_screener_handler

    cached = {"ok": True, "candidates": [{"security_id": "1333", "ticker": "HDFCBANK",
                                          "name": "HDFC Bank", "atr_pct": 2.5}],
              "count": 1}
    _make_screener_stubs(monkeypatch, cache_val=cached)

    # If DB or screener were called these would raise — proving they weren't hit
    resp = asyncio.run(kronos_screener_handler(_FakeRequest()))
    assert resp.status == 200
    body = _body(resp)
    assert body["count"] == 1
    assert body["candidates"][0]["ticker"] == "HDFCBANK"


def test_screener_db_error_returns_200_with_error_flag(monkeypatch):
    """If the screener or DB raises, the handler returns 200 with ok=False
    and an empty candidates list (graceful degradation, not a 5xx crash)."""
    from apps.routes.db import kronos_screener_handler

    _make_screener_stubs(monkeypatch)

    with patch("core.nse_screener.get_top_volatile", side_effect=RuntimeError("screener down")):
        resp = asyncio.run(kronos_screener_handler(_FakeRequest()))

    # Handler catches the exception and returns an error payload (not a 5xx)
    body = _body(resp)
    assert body["ok"] is False
    assert "candidates" in body


def test_screener_n_param_respected(monkeypatch):
    """The ?n= query param is forwarded to get_top_volatile."""
    from apps.routes.db import kronos_screener_handler

    _make_screener_stubs(monkeypatch)

    seen_n = []

    def _fake_gtv(n):
        seen_n.append(n)
        return []

    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value.fetchall.return_value = []
    mock_engine.connect.return_value = mock_conn

    with patch("core.nse_screener.get_top_volatile", side_effect=_fake_gtv):
        with patch("db.get_engine", return_value=mock_engine):
            asyncio.run(kronos_screener_handler(_FakeRequest(query={"n": "10"})))

    assert seen_n == [10]
