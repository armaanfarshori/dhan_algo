"""
dhan-api — dashboard + analytics process.

Serves the React dashboard and every read endpoint from:
  • run/trader_heartbeat.json  — live engine state exported by dhan-trader
  • TimescaleDB                — signals, trades, bars, instruments
  • a READ-ONLY DhanClient     — funds/positions/LTP (data APIs only;
                                 order placement never happens here)

Control surface is deliberately tiny: POST /api/killswitch writes a flag
file the trader's risk loop picks up within seconds. Trading mode changes
require editing .env and restarting dhan-trader — there is no auth layer
until M6, so nothing dangerous is one HTTP request away.

This process can crash, hang, or redeploy without touching order flow.
"""
import asyncio
import hmac
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S+00:00",
)
# T4: always emit UTC timestamps regardless of host OS timezone.
logging.Formatter.converter = time.gmtime
logger = logging.getLogger("dhan.api")

from config import get_config

cfg = get_config()

ROOT = Path(__file__).parent.parent
RUN_DIR = ROOT / "run"
HEARTBEAT_FILE = RUN_DIR / "trader_heartbeat.json"
KILLSWITCH_FILE = RUN_DIR / "killswitch"
RESUME_FILE = RUN_DIR / "resume"
TRADER_LOG = Path("/var/log/dhan/trader.log")
DIST_DIR = ROOT / "dashboard" / "dist"
STATIC_DIR = ROOT / "static"

HEARTBEAT_STALE_S = 30


# ── Heartbeat access ──────────────────────────────────────────────────────────

def read_heartbeat() -> tuple[dict, bool]:
    """Returns (heartbeat, alive). alive=False if missing or stale."""
    try:
        hb = json.loads(HEARTBEAT_FILE.read_text())
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(hb["ts"])).total_seconds()
        return hb, age < HEARTBEAT_STALE_S
    except Exception:
        return {}, False


# ── DB query helper ───────────────────────────────────────────────────────────

async def _db_query(fn):
    """Run a synchronous callable in the default thread executor.

    Replaces the repeated asyncio.get_running_loop().run_in_executor(None, fn)
    pattern that appeared in every DB-backed handler.  Callers catch exceptions
    themselves — this helper does NOT swallow them.
    """
    return await asyncio.get_running_loop().run_in_executor(None, fn)


# ── Simple TTL cache for slow endpoints ──────────────────────────────────────

_CACHE: dict = {}


def _cache_get(key: str, ttl: int):
    e = _CACHE.get(key)
    return e["val"] if e and (time.monotonic() - e["ts"]) < ttl else None


def _cache_set(key: str, val):
    _CACHE[key] = {"val": val, "ts": time.monotonic()}


# ── Middleware ────────────────────────────────────────────────────────────────

@web.middleware
async def cors_middleware(request, handler):
    # OPTIONS preflight must return 200 before the real handler runs.
    if request.method == "OPTIONS":
        return web.Response(status=200, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Dashboard-Token, Authorization",
        })
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Dashboard-Token, Authorization"
    return resp


# ── Auth guard for mutating POST endpoints ────────────────────────────────────

_unprotected_warned = False  # log the warning once per process lifetime


def _check_auth(request: web.Request):
    """Return a 401 Response if the request fails the shared-secret check,
    or None if the request is allowed.

    Behaviour:
    - Token unset (empty string): ALLOW but log a one-time WARNING so the
      operator knows the control surface is open.  This preserves current
      behaviour so a misconfigured secret never locks anyone out of the
      kill-switch.
    - Token set: require either
        X-Dashboard-Token: <token>
      or
        Authorization: Bearer <token>
      Any mismatch → 401.
    """
    global _unprotected_warned
    token = cfg.dashboard_token
    if not token:
        if not _unprotected_warned:
            logger.warning(
                "Control endpoints are UNPROTECTED — set DASHBOARD_TOKEN in .env"
            )
            _unprotected_warned = True
        return None  # fail-open

    # Check X-Dashboard-Token header first, then Authorization: Bearer …
    provided = request.headers.get("X-Dashboard-Token", "")
    if not provided:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            provided = auth_hdr[len("Bearer "):]

    if not hmac.compare_digest(provided, token):
        return web.json_response(
            {"ok": False, "error": "unauthorized"}, status=401
        )
    return None


# ── Read-only Dhan client (funds / positions / LTP) ───────────────────────────

class _ReadOnlyDhan:
    """Lazy DhanClient that re-reads the shared token on auth errors.
    Data APIs only — this process never places orders."""

    def __init__(self):
        self._client = None

    async def _get(self):
        from core.client import DhanClient
        from core.token_manager import read_current_token
        if self._client is None:
            token = read_current_token() or cfg.dhan_access_token
            self._client = DhanClient(cfg.dhan_client_id, token)
            await self._client.__aenter__()
        return self._client

    async def call(self, method: str, *args, **kwargs):
        client = await self._get()
        try:
            return await getattr(client, method)(*args, **kwargs)
        except Exception as exc:
            if "DH-901" in str(exc):
                from core.token_manager import read_current_token
                fresh = read_current_token()
                if fresh:
                    client._on_token_refreshed(fresh)
                    return await getattr(client, method)(*args, **kwargs)
            raise


_dhan_ro = _ReadOnlyDhan()


# ── Dashboard static file handler ─────────────────────────────────────────────

async def dashboard_handler(_r: web.Request) -> web.Response:
    react_index = DIST_DIR / "index.html"
    path = react_index if react_index.exists() else STATIC_DIR / "index.html"
    # index.html must always revalidate — the JS bundles it points at are
    # content-hashed, so a cached index can pin users to a stale build
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


# ── Route handler re-exports ──────────────────────────────────────────────────
# Imported at module level so tests can patch them via `apps.api.<name>` and
# so that monkeypatching api.cfg / api.RUN_DIR is picked up at call time
# (the handlers resolve those names through `import apps.api` lazily).

from apps.routes.heartbeat import (  # noqa: E402
    health_handler,
    snapshot_handler,
    status_handler,
    risk_handler,
    feed_handler,
    paper_positions_handler,
    config_handler,
    trading_mode_handler,
    killswitch_handler,
    resume_handler,
    kronos_live_handler,
)
from apps.routes.db import (  # noqa: E402
    equity_handler,
    signals_handler,
    trades_handler,
    db_stats_handler,
    kronos_gate_handler,
    kronos_signals_handler,
    kronos_screener_handler,
    rate_limits_handler,
    screen_handler,
    screen_days_handler,
)
from apps.routes.market import (  # noqa: E402
    funds_handler,
    positions_handler,
    instrument_price_handler,
    instrument_search_handler,
    market_status_handler,
    watchlist_handler,
    watchlist_refresh_handler,
)
from apps.routes.system import (  # noqa: E402
    logs_handler,
    backfill_status_handler,
    system_health_handler,
)
from apps.routes.backtest import (  # noqa: E402
    backtest_runs_handler,
    backtest_run_handler,
)


# ── Postback webhook (Dhan order-update notifications) ────────────────────────
# Kept here (not in routes/system.py) so that monkeypatching
# apps.api._reconcile_postback in tests resolves at call time from this
# module's namespace rather than from the routes sub-module.

def _reconcile_postback(payload: dict) -> None:
    """Persist the authoritative broker fill to the journal table.

    The api process owns no trading state — positions live in the trader
    process.  Full cross-process reconciliation (trader adopting the fill)
    is the larger M6/M8 follow-up work.  This call captures the raw event
    for audit and later reconcile without mutating any in-memory state.
    """
    try:
        import json as _json
        from db import get_session
        from sqlalchemy import text
        with get_session() as session:
            session.execute(text(
                "INSERT INTO journal (level, category, security_id, message, detail) "
                "VALUES (:lvl, :cat, :sid, :msg, CAST(:detail AS JSONB))"),
                {"lvl": "ORDER", "cat": "postback",
                 "sid": str(payload.get("securityId") or "")[:20],
                 "msg": f"postback {payload.get('orderStatus')} {payload.get('orderId')}",
                 "detail": _json.dumps(payload)})
    except Exception:
        # A webhook must always ack — swallow persistence errors (non-fatal).
        logger.exception("_reconcile_postback: failed to persist fill (non-fatal)")


async def postback_handler(request: web.Request) -> web.Response:
    """Dhan order-update webhook.

    SEC-09: HMAC verification via DHAN_WEBHOOK_SECRET / X-Dhan-Signature.
    M6: persists the fill to the journal table via _reconcile_postback so
    the authoritative broker event is no longer silently discarded.
    """
    import hashlib
    secret = cfg.dhan_webhook_secret
    if secret:
        raw_body = await request.read()
        expected = hmac.new(
            secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        provided = request.headers.get("X-Dhan-Signature", "")
        if not provided or not hmac.compare_digest(expected, provided):
            logger.warning("Postback rejected — invalid or missing X-Dhan-Signature")
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            payload = json.loads(raw_body)
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
    else:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
    try:
        logger.info("📬 Postback: %s order %s → %s",
                    payload.get("tradingSymbol", "?"), payload.get("orderId"),
                    payload.get("orderStatus"))
        _reconcile_postback(payload)
        return web.json_response({"ack": "ok"})
    except Exception:
        logger.exception("postback_handler failed")
        return web.json_response({"ok": False, "error": "internal error"}, status=400)


def build_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])

    app.router.add_get("/", dashboard_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/api/snapshot", snapshot_handler)
    app.router.add_get("/api/equity", equity_handler)
    app.router.add_get("/api/kronos/gate", kronos_gate_handler)
    app.router.add_get("/api/status", status_handler)
    app.router.add_get("/api/risk", risk_handler)
    app.router.add_get("/api/feed", feed_handler)
    app.router.add_get("/api/signals", signals_handler)
    app.router.add_get("/api/trades", trades_handler)
    app.router.add_get("/api/logs", logs_handler)
    app.router.add_get("/api/mode", trading_mode_handler)
    app.router.add_post("/api/mode", trading_mode_handler)
    app.router.add_post("/api/killswitch", killswitch_handler)
    app.router.add_post("/api/resume", resume_handler)
    app.router.add_get("/api/paper/positions", paper_positions_handler)
    app.router.add_get("/api/config", config_handler)
    app.router.add_get("/api/funds", funds_handler)
    app.router.add_get("/api/positions", positions_handler)
    app.router.add_get("/api/instruments/search", instrument_search_handler)
    app.router.add_get("/api/instruments/price", instrument_price_handler)
    app.router.add_get("/api/market", market_status_handler)
    app.router.add_get("/api/watchlist", watchlist_handler)
    app.router.add_post("/api/watchlist/refresh", watchlist_refresh_handler)
    app.router.add_get("/api/db/stats", db_stats_handler)
    app.router.add_get("/api/kronos/signals", kronos_signals_handler)
    app.router.add_get("/api/kronos/screener", kronos_screener_handler)
    app.router.add_get("/api/kronos/live", kronos_live_handler)
    app.router.add_get("/api/rate-limits", rate_limits_handler)
    app.router.add_get("/api/screen", screen_handler)
    app.router.add_get("/api/screen/days", screen_days_handler)
    app.router.add_get("/api/backfill/status", backfill_status_handler)
    app.router.add_get("/api/system/health", system_health_handler)
    app.router.add_get("/api/backtest/runs", backtest_runs_handler)
    app.router.add_get("/api/backtest/runs/{name}", backtest_run_handler)
    app.router.add_post("/postback", postback_handler)

    if (DIST_DIR / "assets").exists():
        app.router.add_static("/assets", DIST_DIR / "assets")
    return app


async def main():
    logger.info("dhan-api starting on port %d", cfg.webhook_port)

    # aiohttp's FileResponse/static serving stats+opens files in the DEFAULT
    # executor. The DB queries this app runs there too once saturated all
    # ~6 default threads during DB contention and froze the dashboard while
    # JSON endpoints kept answering. Bigger dedicated pool = file serving
    # can never queue behind slow queries.
    from concurrent.futures import ThreadPoolExecutor
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=16, thread_name_prefix="api"))

    from db import init_db
    init_db(cfg.db_url)

    app = build_app()
    # access_log=None: the dashboard polls several endpoints every second —
    # access lines would bury real log content (~500KB per 5 min observed)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg.api_bind_host, cfg.webhook_port)
    await site.start()
    logger.info("🌐 Dashboard: http://localhost:%d", cfg.webhook_port)

    # Watchlist for /api/watchlist (cache-file based, refreshed on demand)
    from core.watchlist import WatchlistManager
    try:
        app["watchlist"] = await WatchlistManager.build()
    except Exception as exc:
        logger.warning("Watchlist init failed: %s", exc)

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
