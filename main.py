"""
DhanHQ Algo Platform — Main Orchestrator
=========================================
Wires together:  DhanClient → RiskManager → ORB+Kronos strategies → Web Dashboard

ORB is the sole production strategy; the Kronos gate runs in shadow mode until
calibration proves it adds value (see config.kronos_shadow_mode). Legacy
strategies/scanners were removed in the Phase-0 cleanup — git history has them.

Run:
    python main.py        (systemd unit: dhan-platform)

Configuration: .env via config.Config — no os.getenv anywhere else.
"""

import asyncio
import logging
import signal
import sys
import time
import warnings
from pathlib import Path
from typing import Optional

# The server intentionally binds before bootstrap finishes (watchdog-safe
# startup) and fills in app state afterwards — aiohttp warns on every such
# assignment. Deliberate pattern; placeholders exist from build_app().
warnings.filterwarnings(
    "ignore", message="Changing state of started or joined application is deprecated",
)

from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

# basicConfig MUST run before install_log_buffer() — it is a no-op once the
# root logger has any handler, and the root level would stay at WARNING. With
# the old order the platform never wrote a single INFO line to stderr (which
# is why /tmp/platform.log was always empty).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

from core.journal import get_trade_logger, get_log_buffer, install_log_buffer
from core.live_feed import LiveFeed
install_log_buffer()   # capture all log messages into rolling buffer
from core.client import DhanClient
from core.risk import RiskManager, RiskConfig
from core.watchlist import WatchlistManager

logger = logging.getLogger("dhan.main")

from config import get_config
cfg = get_config()

DIST_DIR   = Path(__file__).parent / "dashboard" / "dist"
STATIC_DIR = Path(__file__).parent / "static"


# ── CORS middleware ───────────────────────────────────────────────────────────
@web.middleware
async def cors_middleware(request, handler):
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# ── Postback & health ─────────────────────────────────────────────────────────
async def postback_handler(request: web.Request) -> web.Response:
    try:
        payload  = await request.json()
        order_id = payload.get("orderId")
        status   = payload.get("orderStatus")
        symbol   = payload.get("tradingSymbol", "?")
        logger.info(f"📬 Postback: {symbol} order {order_id} → {status}")
        return web.json_response({"ack": "ok"})
    except Exception as e:
        logger.error(f"Postback error: {e}")
        return web.json_response({"error": str(e)}, status=400)


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "paper":  request.app.get("paper_trading", cfg.paper_trading),
    })


async def trading_mode_handler(request: web.Request) -> web.Response:
    """GET → current mode. POST {paper: true/false} → toggle all ORB strategies."""
    if request.method == "POST":
        body  = await request.json()
        paper = bool(body.get("paper", True))

        # No auth layer exists yet (M6) — flipping to LIVE via an open HTTP
        # endpoint is a real-money hazard. Until M6 lands, going live requires
        # ALLOW_LIVE_TOGGLE=true in .env plus a restart.
        if not paper and not cfg.allow_live_toggle:
            logger.warning("Blocked attempt to switch to LIVE via /api/mode (ALLOW_LIVE_TOGGLE not set)")
            return web.json_response({
                "ok": False,
                "error": "Live toggle disabled — set ALLOW_LIVE_TOGGLE=true in .env and restart (no auth layer until M6)",
            }, status=403)

        for strategy in request.app.get("orb_strategies", []):
            strategy.config.paper_trading = paper
            if not paper and strategy.position != 0:
                logger.warning(f"Mode→LIVE: paper position on {strategy.config.name} "
                               f"({strategy.position} @ {strategy.entry_price}) — clearing")
                strategy.position = 0
                strategy.entry_price = 0.0

        request.app["paper_trading"] = paper
        mode = "PAPER" if paper else "LIVE"
        logger.warning(f"⚠️  Trading mode switched to {mode}")
        return web.json_response({"ok": True, "paper": paper, "mode": mode})

    paper = request.app.get("paper_trading", cfg.paper_trading)
    return web.json_response({"ok": True, "paper": paper, "mode": "PAPER" if paper else "LIVE"})


# ── Dashboard (serves React build or fallback to static) ─────────────────────
async def dashboard_handler(request: web.Request) -> web.Response:
    react_index = DIST_DIR / "index.html"
    if react_index.exists():
        return web.FileResponse(react_index)
    return web.FileResponse(STATIC_DIR / "index.html")


# ── API handlers ──────────────────────────────────────────────────────────────
async def status_handler(request: web.Request) -> web.Response:
    strategy = request.app["strategy"]
    uptime   = int(time.time() - request.app["start_time"])

    current_paper = request.app.get("paper_trading", cfg.paper_trading)
    if strategy is None:
        return web.json_response({
            "mode":             "PAPER" if current_paper else "LIVE",
            "client_id":        cfg.dhan_client_id,
            "uptime_seconds":   uptime,
            "strategy_name":    "none",
            "strategy_running": False,
            "orders_placed":    0,
            "position":         0,
            "entry_price":      0.0,
            "warmup":           {"ready": False},
            "note":             "No strategies running — screener returned 0 securities",
        })
    return web.json_response({
        "mode":             "PAPER" if current_paper else "LIVE",
        "client_id":        cfg.dhan_client_id,
        "uptime_seconds":   uptime,
        "strategy_name":    strategy.config.name,
        "strategy_running": strategy._running,
        "orders_placed":    strategy.orders_placed,
        "position":         strategy.position,
        "entry_price":      strategy.entry_price,
        "warmup":           {"ready": True},
    })


async def risk_handler(request: web.Request) -> web.Response:
    risk = request.app.get("risk")
    if risk is None:
        return web.json_response({"ok": False, "initializing": True}, status=503)
    return web.json_response(risk.get_summary())


async def signals_handler(request: web.Request) -> web.Response:
    """All signals from the running ORB strategies, newest first."""
    all_sigs = []
    for strategy in request.app.get("orb_strategies", []):
        for s in strategy.signals:
            all_sigs.append({
                "action":    s.action,
                "price":     s.price,
                "reason":    s.reason,
                "timestamp": s.timestamp.isoformat(),
                "source":    strategy.config.name,
            })
    all_sigs.sort(key=lambda x: x["timestamp"], reverse=True)
    return web.json_response(all_sigs[:100])


async def funds_handler(request: web.Request) -> web.Response:
    try:
        data = await request.app["client"].get_funds()
        return web.json_response({"ok": True, "data": data})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=503)


async def positions_handler(request: web.Request) -> web.Response:
    try:
        data = await request.app["client"].get_positions()
        return web.json_response({"ok": True, "data": data})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=503)


async def paper_positions_handler(request: web.Request) -> web.Response:
    """Open simulated positions across the ORB strategies — paper mode only."""
    current_paper = request.app.get("paper_trading", cfg.paper_trading)
    if not current_paper:
        return web.json_response({"ok": True, "count": 0, "data": [],
                                   "note": "Live mode — see /api/positions for real positions"})

    wl = request.app.get("watchlist")
    stocks = {s.security_id: s for s in (wl.get() if wl else [])}

    positions = []
    for strategy in request.app.get("orb_strategies", []):
        if strategy.position == 0:
            continue
        sid = strategy.config.security_id
        sym = stocks.get(sid)
        positions.append({
            "engine":      "EQ",
            "strategy":    strategy.config.name,
            "symbol":      sym.symbol if sym else sid,
            "name":        sym.name if sym else sid,
            "segment":     strategy.config.exchange_segment,
            "qty":         strategy.position,
            "entry_price": strategy.entry_price,
        })

    return web.json_response({"ok": True, "count": len(positions), "data": positions})


async def auth_handler(request: web.Request) -> web.Response:
    mgr = request.app.get("auth_manager")
    if not mgr:
        return web.json_response({"mode": "manual", "note": "Set DHAN_PIN + DHAN_TOTP_SECRET to enable auto-refresh"})
    return web.json_response({"mode": "auto", "valid": mgr.is_valid()})


async def config_handler(request: web.Request) -> web.Response:
    return web.json_response(request.app.get("runtime_config", {}))


async def market_status_handler(_request: web.Request) -> web.Response:
    """Returns open/close status for NSE equity, NSE F&O and MCX commodity."""
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST)
    t   = now.time()
    wd  = now.weekday()  # 0=Mon … 6=Sun

    is_weekday = wd < 5

    nse_open  = dtime(9, 15)
    nse_close = dtime(15, 30)
    pre_open  = dtime(9, 0)

    # MCX: Mon–Fri 09:00–23:30, Sat 09:00–14:00
    mcx_open      = dtime(9, 0)
    mcx_close_wkd = dtime(23, 30)
    mcx_close_sat = dtime(14, 0)
    is_saturday   = wd == 5

    nse_status = "OPEN" if is_weekday and nse_open <= t <= nse_close else "CLOSED"
    pre_status = "PRE"  if is_weekday and pre_open <= t < nse_open   else None

    if is_weekday:
        mcx_status = "OPEN" if mcx_open <= t <= mcx_close_wkd else "CLOSED"
    elif is_saturday:
        mcx_status = "OPEN" if mcx_open <= t <= mcx_close_sat else "CLOSED"
    else:
        mcx_status = "CLOSED"

    return web.json_response({
        "nse_equity":  pre_status or nse_status,
        "nse_fno":     pre_status or nse_status,
        "mcx":         mcx_status,
        "ist_time":    now.strftime("%H:%M:%S"),
        "weekday":     now.strftime("%A"),
        "is_weekend":  not is_weekday and not is_saturday,
    })


async def instrument_search_handler(request: web.Request) -> web.Response:
    from core.instruments import InstrumentMaster
    q       = request.rel_url.query.get("q", "").strip()
    segment = request.rel_url.query.get("segment", "NSE_EQ")
    if len(q) < 2:
        return web.json_response({"ok": False, "error": "Query must be at least 2 characters"}, status=400)
    loop    = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, InstrumentMaster.search_instruments, q, segment)
    return web.json_response({"ok": True, "results": results})


async def instrument_price_handler(request: web.Request) -> web.Response:
    sid     = request.rel_url.query.get("security_id", "")
    segment = request.rel_url.query.get("segment", "NSE_EQ")
    if not sid:
        return web.json_response({"ok": False, "error": "security_id required"}, status=400)
    seg_map = {"NSE_EQ": "NSE_EQ", "NSE_FNO": "NSE_FNO", "MCX": "MCX_COMM"}
    api_seg = seg_map.get(segment, segment)
    try:
        data  = await request.app["client"].get_ltp({api_seg: [int(sid)]})
        seg_d = data.get("data", {}).get(api_seg, {})
        price = seg_d.get(sid, {}).get("last_price", 0.0)
        return web.json_response({"ok": True, "security_id": sid, "price": price, "segment": segment})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=503)


async def killswitch_handler(request: web.Request) -> web.Response:
    risk = request.app.get("risk")
    if risk is None:
        return web.json_response({"ok": False, "initializing": True}, status=503)
    risk.activate_kill_switch()

    for strategy in request.app.get("orb_strategies", []):
        strategy.stop()
    for task in request.app.get("orb_tasks", []):
        if not task.done():
            task.cancel()

    logger.critical("⛔ KILL SWITCH ACTIVATED via dashboard")
    return web.json_response({"ok": True, "halted": True, "message": "Kill switch activated"})


async def watchlist_handler(request: web.Request) -> web.Response:
    wl = request.app.get("watchlist")
    if not wl:
        return web.json_response({"ok": False, "error": "Watchlist not initialised"}, status=503)
    return web.json_response({"ok": True, **wl.summary()})


async def watchlist_refresh_handler(request: web.Request) -> web.Response:
    wl = request.app.get("watchlist")
    if not wl:
        return web.json_response({"ok": False, "error": "Watchlist not initialised"}, status=503)
    try:
        await wl.refresh()
        return web.json_response({"ok": True, "count": len(wl.get()),
                                  "stocks": [s.symbol for s in wl.get()]})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def logs_handler(_request: web.Request) -> web.Response:
    limit = int(_request.rel_url.query.get("limit", 50))
    return web.json_response({"ok": True, "logs": get_log_buffer().get_logs(limit)})


async def feed_handler(_request: web.Request) -> web.Response:
    feed: LiveFeed = _request.app.get("live_feed")
    if not feed:
        return web.json_response({"ok": False, "error": "Live feed not running"})
    sids   = feed.all_subscribed_sids()
    sample = {sid: feed.get_ltp(sid) for sid in sids[:6]}
    return web.json_response({
        "ok":          True,
        "connected":   feed.is_connected(),
        "subscribed":  len(sids),
        "sample_ltps": sample,
    })


async def trades_handler(request: web.Request) -> web.Response:
    tl      = get_trade_logger()
    limit   = int(request.rel_url.query.get("limit", 200))
    engine  = request.rel_url.query.get("engine", "")
    trades  = tl.get_trades(limit)
    if engine:
        trades = [t for t in trades if t.get("engine","").upper() == engine.upper()]
    return web.json_response({
        "ok":      True,
        "count":   len(trades),
        "summary": tl.get_session_summary(),
        "trades":  trades,
    })


# ── Data pipeline + AI handlers ───────────────────────────────────────────────

# Simple TTL caches so slow queries/subprocesses don't block every poll
_CACHE: dict = {}

def _cache_get(key: str, ttl_seconds: int):
    entry = _CACHE.get(key)
    if entry and (time.monotonic() - entry["ts"]) < ttl_seconds:
        return entry["val"]
    return None

def _cache_set(key: str, val):
    _CACHE[key] = {"val": val, "ts": time.monotonic()}


async def db_stats_handler(_request: web.Request) -> web.Response:
    """Row counts, date ranges, and per-segment breakdown from TimescaleDB. Cached 60s.

    Query runs in a thread executor — these scans take seconds while the
    backfill writes to the same DB, and a sync call here would freeze the
    whole event loop (including /api/status, which the watchdog probes).
    """
    cached = _cache_get("db_stats", 60)
    if cached:
        return web.json_response(cached)

    def _query():
        from db import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            bars = conn.execute(text("""
                SELECT timeframe, COUNT(*) AS rows,
                       MIN(time)::date AS earliest, MAX(time)::date AS latest
                FROM bars GROUP BY timeframe ORDER BY timeframe
            """)).fetchall()
            # Per-segment bar counts (joined with instruments)
            seg_bars = conn.execute(text("""
                SELECT i.exchange_segment,
                       COUNT(DISTINCT b.security_id) AS securities,
                       COUNT(*) AS bars,
                       MIN(b.time)::date AS earliest,
                       MAX(b.time)::date AS latest
                FROM bars b
                JOIN instruments i ON i.security_id = b.security_id
                WHERE b.timeframe = '1m'
                GROUP BY i.exchange_segment
                ORDER BY bars DESC
            """)).fetchall()
            instruments = conn.execute(text(
                "SELECT exchange_segment, COUNT(*) FROM instruments GROUP BY exchange_segment ORDER BY COUNT(*) DESC"
            )).fetchall()
            signals_count = conn.execute(text("SELECT COUNT(*) FROM signals")).scalar()
            trades_count  = conn.execute(text("SELECT COUNT(*) FROM trades")).scalar()
        return {
            "ok": True,
            "bars": [{"timeframe": r[0], "rows": r[1], "earliest": str(r[2]), "latest": str(r[3])} for r in bars],
            "segments": [{"segment": r[0], "securities": r[1], "bars": r[2],
                          "earliest": str(r[3]), "latest": str(r[4])} for r in seg_bars],
            "instruments": {r[0]: r[1] for r in instruments},
            "signals": signals_count,
            "trades":  trades_count,
        }

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _query)
        _cache_set("db_stats", result)
        return web.json_response(result)
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc), "bars": [], "segments": [], "instruments": {}})


async def kronos_signals_handler(request: web.Request) -> web.Response:
    """Latest Kronos signals from the signals table."""
    try:
        limit = int(request.rel_url.query.get("limit", 50))

        def _query():
            from db import get_engine
            from sqlalchemy import text
            with get_engine().connect() as conn:
                return conn.execute(text("""
                    SELECT s.security_id, s.side, s.score, s.confidence, s.strategy, s.ts,
                           s.features_snapshot, i.ticker, i.name
                    FROM signals s
                    LEFT JOIN instruments i ON i.security_id = s.security_id
                    ORDER BY s.ts DESC LIMIT :lim
                """), {"lim": limit}).fetchall()

        rows = await asyncio.get_event_loop().run_in_executor(None, _query)
        return web.json_response({"ok": True, "signals": [
            {"security_id": r[0], "side": r[1], "score": float(r[2] or 0),
             "confidence": float(r[3] or 0), "strategy": r[4],
             "ts": str(r[5]), "features": r[6],
             "ticker": r[7] or r[0], "name": r[8] or ""}
            for r in rows
        ]})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc), "signals": []})


async def kronos_live_handler(request: web.Request) -> web.Response:
    """Live Kronos scanner state — current screener + scored forecasts with names."""
    scanner = request.app.get("kronos_scanner")
    if scanner is None:
        return web.json_response({"ok": False, "error": "scanner not running",
                                  "results": [], "screened_today": {}})
    return web.json_response(scanner.get_state())


async def kronos_screener_handler(request: web.Request) -> web.Response:
    """Top N volatile NSE equities from the ATR screener — with security names."""
    try:
        n = int(request.rel_url.query.get("n", 20))

        def _query():
            from core.nse_screener import get_top_volatile
            from db import get_engine
            from sqlalchemy import text
            candidates = get_top_volatile(n=n)
            # Resolve names
            sids = [c["security_id"] for c in candidates]
            names = {}
            if sids:
                with get_engine().connect() as conn:
                    rows = conn.execute(text(
                        "SELECT security_id, ticker, name FROM instruments WHERE security_id = ANY(:ids)"
                    ), {"ids": sids}).fetchall()
                    names = {r[0]: {"ticker": r[1] or r[0], "name": r[2] or ""} for r in rows}
            for c in candidates:
                meta = names.get(c["security_id"], {})
                c["ticker"] = meta.get("ticker", c["security_id"])
                c["name"]   = meta.get("name", "")
            return candidates

        candidates = await asyncio.get_event_loop().run_in_executor(None, _query)
        return web.json_response({"ok": True, "candidates": candidates, "count": len(candidates)})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc), "candidates": []})


async def backfill_status_handler(_request: web.Request) -> web.Response:
    """Live backfill progress from the EC2 log file."""
    import subprocess
    log_path = "/tmp/backfill.log"
    lines: list[str] = []
    try:
        result = subprocess.run(["tail", "-30", log_path], capture_output=True, text=True, timeout=3)
        lines = [l for l in result.stdout.strip().split("\n") if l]
    except Exception:
        pass
    running = bool(subprocess.run(["pgrep", "-f", "backfill.py"], capture_output=True).returncode == 0)
    return web.json_response({"ok": True, "running": running, "log_tail": lines})


async def hermes_status_handler(_request: web.Request) -> web.Response:
    """Hermes gateway status. Cached 30s — subprocess is slow."""
    cached = _cache_get("hermes_status", 30)
    if cached:
        return web.json_response(cached)
    import subprocess
    try:
        result = subprocess.run(
            ["bash", "-c", "export PATH=$HOME/.local/bin:$PATH; hermes gateway status 2>&1 | head -5"],
            capture_output=True, text=True, timeout=5,
        )
        running = "active (running)" in result.stdout
        val = {
            "ok": True,
            "running": running,
            "raw": result.stdout.strip()[:300],
            "model": "meta-llama/llama-3.3-70b-instruct",
            "provider": "openrouter",
        }
        _cache_set("hermes_status", val)
        return web.json_response(val)
    except Exception as exc:
        return web.json_response({"ok": False, "running": False, "error": str(exc)})


# ── Server startup ────────────────────────────────────────────────────────────
async def start_server(app: web.Application):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.webhook_port)
    await site.start()
    logger.info(f"🌐 Dashboard: http://localhost:{cfg.webhook_port}")
    return runner


def build_app() -> web.Application:
    """Create the web app with all routes and pre-init placeholder state.

    Called BEFORE the heavy startup work (token, watchlist, screener) so the
    port binds within seconds. Handlers must tolerate the placeholder Nones
    until main() fills in the real objects.
    """
    app = web.Application(middlewares=[cors_middleware])

    # Placeholder state — replaced during bootstrap
    app["strategy"]       = None
    app["risk"]           = None
    app["client"]         = None
    app["auth_manager"]   = None
    app["orb_strategies"] = []
    app["orb_tasks"]      = []
    app["start_time"]     = time.time()
    app["paper_trading"]  = cfg.paper_trading
    app["runtime_config"] = {}

    # ── Data pipeline + AI endpoints ──────────────────────────────────────
    app.router.add_get("/api/db/stats",           db_stats_handler)
    app.router.add_get("/api/kronos/signals",     kronos_signals_handler)
    app.router.add_get("/api/kronos/screener",    kronos_screener_handler)
    app.router.add_get("/api/kronos/live",        kronos_live_handler)
    app.router.add_get("/api/backfill/status",    backfill_status_handler)
    app.router.add_get("/api/hermes/status",      hermes_status_handler)

    app.router.add_get("/",                       dashboard_handler)
    app.router.add_get("/health",                 health_handler)
    app.router.add_get("/api/mode",               trading_mode_handler)
    app.router.add_post("/api/mode",              trading_mode_handler)
    app.router.add_get("/api/status",             status_handler)
    app.router.add_get("/api/risk",               risk_handler)
    app.router.add_get("/api/signals",            signals_handler)
    app.router.add_get("/api/funds",              funds_handler)
    app.router.add_get("/api/positions",          positions_handler)
    app.router.add_get("/api/paper/positions",    paper_positions_handler)
    app.router.add_get("/api/auth",               auth_handler)
    app.router.add_get("/api/config",             config_handler)
    app.router.add_get("/api/instruments/search", instrument_search_handler)
    app.router.add_get("/api/instruments/price",  instrument_price_handler)
    app.router.add_post("/api/killswitch",        killswitch_handler)
    app.router.add_get("/api/logs",               logs_handler)
    app.router.add_get("/api/feed",               feed_handler)
    app.router.add_get("/api/trades",             trades_handler)
    app.router.add_get("/api/market",             market_status_handler)
    app.router.add_get("/api/watchlist",          watchlist_handler)
    app.router.add_post("/api/watchlist/refresh", watchlist_refresh_handler)
    app.router.add_post("/postback",              postback_handler)

    # Serve React build assets if available
    if (DIST_DIR / "assets").exists():
        app.router.add_static("/assets", DIST_DIR / "assets")

    return app


# ── Bootstrap ─────────────────────────────────────────────────────────────────
async def main():
    logger.info("=" * 60)
    logger.info("  DhanHQ Algo Trading Platform  v2.0")
    logger.info(f"  Mode:     {'📝 PAPER TRADING' if cfg.paper_trading else '🔴 LIVE TRADING'}")
    logger.info(f"  Strategy: ORB + Kronos ({'SHADOW' if cfg.kronos_shadow_mode else 'ENFORCING'} gate)")
    logger.info(f"  Client:   {cfg.dhan_client_id}")
    logger.info("=" * 60)

    if not cfg.paper_trading:
        logger.warning("⚠️  LIVE TRADING MODE — real money at risk!")

    # ── Web server FIRST — bind the port before any slow startup work ─────────
    app = build_app()
    server_runner = await start_server(app)

    # ── Auth: MasterTokenManager is the single owner of token refresh ─────────
    # backfill.py reads the token from dhan_token.json — never generates itself
    from core.token_manager import MasterTokenManager
    master_tm = MasterTokenManager()
    access_token = await master_tm.load_or_generate()
    logger.info("🔑 MasterTokenManager active — sole token owner")

    async with DhanClient(
        client_id=cfg.dhan_client_id,
        access_token=access_token,
        auth_manager=master_tm,
    ) as dhan:

        # max_loss_per_trade: notional cap per position.
        # Paper: 20% of paper balance. Live: tighten before going live.
        _trade_risk = int(cfg.paper_balance * 0.20) if cfg.paper_trading else 25_000

        # ── DB backend (non-blocking audit trail) ─────────────────────────────
        from core.journal import get_db_backend
        db = get_db_backend()
        await db.connect()
        run_id = await db.log_run_start(
            mode="PAPER" if cfg.paper_trading else "LIVE",
            strategy="orb",
        )

        risk_cfg = RiskConfig(
            max_daily_loss=cfg.max_daily_loss,
            max_open_positions=10,
            max_loss_per_trade=_trade_risk,
            check_interval_seconds=30,
        )
        risk = RiskManager(dhan, risk_cfg, db_backend=db)

        @risk.on_halt
        async def on_risk_halt(reason: str):
            logger.critical(f"⛔ HALT: {reason}")

        # ── Watchlist (top movers from NSE) ──────────────────────────────────
        watchlist = await WatchlistManager.build()

        # ── Strategy: ORB + Kronos (the only strategy) ───────────────────────
        from strategies.strategy_orb import ORBStrategy, ORBConfig
        from core.kronos_signal import get_kronos_engine

        # Kronos engine is a lazy singleton — model loads from HuggingFace on
        # first score call. Don't pre-load: PyTorch adds ~300MB and the box
        # also runs the backfill + Hermes.
        kronos = get_kronos_engine()

        # ── Watchlist: dynamic screener only — no static fallback ────────────
        from core.nse_screener import get_top_volatile
        from db import init_db as _init_db_screener
        _init_db_screener(cfg.db_url)
        # Executor: this scan takes up to its 20s statement timeout while the
        # backfill writes to the same DB — must not block the event loop.
        screener_results = await asyncio.get_event_loop().run_in_executor(
            None, lambda: get_top_volatile(n=cfg.watchlist_n, min_avg_volume=10_000)
        )
        watchlist_ids = [r["security_id"] for r in screener_results]
        if watchlist_ids:
            logger.info("Screener watchlist (%d): %s", len(watchlist_ids), watchlist_ids)
        else:
            # Screener timed out (backfill contention) or bars table empty
            watchlist_ids = [s.security_id for s in watchlist.get()[:cfg.watchlist_n]]
            if watchlist_ids:
                logger.warning("Screener returned 0 — using cached watchlist fallback (%d): %s",
                               len(watchlist_ids), watchlist_ids)
            else:
                logger.warning("Screener and watchlist cache both empty — no ORB strategies will run")

        orb_cfg = ORBConfig(
            orb_minutes=cfg.orb_range_minutes,
            use_kronos=True,
            kronos_min_confidence=cfg.kronos_min_confidence,
            kronos_shadow=cfg.kronos_shadow_mode,
        )
        from strategies.strategy_base import StrategyConfig
        strategies_list = []
        # Stagger poll starts so N concurrent strategies don't burst-hit the
        # quote endpoint simultaneously (Dhan quote limit ~1 req/s).
        stagger_sec = cfg.poll_interval / max(len(watchlist_ids), 1)
        for idx, sid in enumerate(watchlist_ids):
            scfg = StrategyConfig(
                name=f"ORB_{sid}",
                security_id=sid,
                exchange_segment=cfg.watchlist_exchange_segment,
                product_type="INTRADAY",
                quantity=cfg.trade_quantity,
                poll_interval=cfg.poll_interval,
                paper_trading=cfg.paper_trading,
                max_orders=cfg.max_orders_per_session,
            )
            strategies_list.append(
                ORBStrategy(dhan, risk, scfg, orb_config=orb_cfg,
                            trade_logger=get_trade_logger(),
                            kronos_engine=kronos,
                            db_backend=db, run_id=run_id,
                            poll_offset=idx * stagger_sec)
            )

        logger.info("ORB+Kronos active on %d securities: %s", len(strategies_list), watchlist_ids)
        strategy = strategies_list[0] if strategies_list else None

        # ── Live WebSocket feed ───────────────────────────────────────────────
        # Phase 1 wires this into a BarBuilder → bars table so Kronos scores
        # fresh data. For now it backs /api/feed.
        live_feed = LiveFeed(cfg.dhan_client_id, access_token)
        eq_sids = sorted({int(sid) for sid in watchlist_ids if sid.isdigit()} |
                         {int(s.security_id) for s in watchlist.get() if s.security_id.isdigit()})
        if eq_sids:
            live_feed.subscribe({"NSE_EQ": eq_sids})
        logger.info(f"🔌 Live feed subscribed: {len(live_feed.all_subscribed_sids())} instruments via WebSocket")

        # ── Launch ORB strategies (market-hours gated inside run()) ──────────
        orb_tasks = [asyncio.create_task(s.run(), name=f"orb_{s.config.security_id}")
                     for s in strategies_list]

        # ── Kronos live scanner — continuous screener + scoring ──────────────
        # Disable via KRONOS_SCANNER_ENABLED=false to free CPU for the backfill.
        if cfg.kronos_scanner_enabled:
            from core.kronos_scanner import KronosScanner
            kronos_scanner = KronosScanner(kronos, db_backend=db, n=cfg.watchlist_n)
            asyncio.create_task(kronos_scanner.run(), name="kronos_scanner")
            app["kronos_scanner"] = kronos_scanner
        else:
            logger.info("Kronos live scanner DISABLED (KRONOS_SCANNER_ENABLED=false)")

        app["risk"]           = risk
        app["strategy"]       = strategy
        app["orb_strategies"] = strategies_list
        app["orb_tasks"]      = orb_tasks
        app["client"]         = dhan
        app["auth_manager"]   = master_tm
        app["start_time"]     = time.time()
        app["paper_trading"]  = cfg.paper_trading   # mutable; updated by /api/mode
        get_trade_logger()                          # initialise + log session start
        app["watchlist"]      = watchlist
        app["live_feed"]      = live_feed
        app["runtime_config"] = {
            "strategy":      "orb+kronos",
            "kronos_gate":   "shadow" if cfg.kronos_shadow_mode else "enforcing",
            "segment":       cfg.watchlist_exchange_segment,
            "watchlist":     watchlist_ids,
            "quantity":      cfg.trade_quantity,
        }

        # ── Graceful shutdown ─────────────────────────────────────────────────
        loop       = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        def _shutdown(sig, frame):
            logger.info(f"Signal {sig.name} received — shutting down…")
            for s in strategies_list:
                s.stop()
            stop_event.set()

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, _shutdown, s, None)
            except NotImplementedError:
                pass

        logger.info("🚀 Launching tasks…")
        feed_task = asyncio.create_task(live_feed.run(), name="live_feed")

        tasks = [
            asyncio.create_task(risk.run(),        name="risk_monitor"),
            asyncio.create_task(master_tm.run(),   name="token_manager"),
            asyncio.create_task(stop_event.wait(), name="shutdown_watcher"),
        ]

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        live_feed.stop()
        for t in [*orb_tasks, feed_task, *pending]:
            if not t.done():
                t.cancel()
        await asyncio.gather(*orb_tasks, feed_task, *pending, return_exceptions=True)
        await server_runner.cleanup()
        await db.log_run_stop(run_id, outcome="stopped")
        logger.info("✅ Platform shut down cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
