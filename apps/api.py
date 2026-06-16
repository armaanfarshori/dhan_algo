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
logger = logging.getLogger("dhan.api")

from config import get_config

cfg = get_config()

ROOT = Path(__file__).parent.parent
RUN_DIR = ROOT / "run"
HEARTBEAT_FILE = RUN_DIR / "trader_heartbeat.json"
KILLSWITCH_FILE = RUN_DIR / "killswitch"
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


# ── Heartbeat-backed handlers ─────────────────────────────────────────────────

async def health_handler(_r: web.Request) -> web.Response:
    hb, alive = read_heartbeat()
    return web.json_response({
        "status": "ok",
        "trader_alive": alive,
        "paper": hb.get("mode", "PAPER") == "PAPER",
    })


async def snapshot_handler(_r: web.Request) -> web.Response:
    """One payload for the dashboard's fast loop — replaces 5 separate 1s
    pollers. Everything here is a file read; never touches the DB."""
    hb, alive = read_heartbeat()
    return web.json_response({
        "ok": True,
        "alive": alive,
        "ts": datetime.now(timezone.utc).isoformat(),
        "trader": hb,
        "limits": {
            # Approximate (base equity × pct) — the live number in the
            # heartbeat's risk.daily_loss_budget compounds realized P&L.
            "max_daily_loss": round(
                (cfg.paper_balance if cfg.paper_trading else cfg.capital)
                * cfg.max_daily_loss_pct
                * (1.0 if cfg.paper_trading else cfg.live_risk_scale)),
            "paper_balance": cfg.paper_balance,
            "max_orders_per_session": cfg.max_orders_per_session,
            "max_open_positions": cfg.max_open_positions,
        },
    })


async def kronos_gate_handler(_r: web.Request) -> web.Response:
    """Today's persisted gate verdicts + the latest calibration verdict."""
    cached = _cache_get("gate_panel", 20)
    if cached:
        return web.json_response(cached)

    def _decisions():
        from db import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            rows = conn.execute(text("""
                SELECT s.security_id, s.side, s.confidence, s.ts,
                       s.features_snapshot, i.ticker
                FROM signals s
                LEFT JOIN instruments i ON i.security_id = s.security_id
                WHERE s.strategy = 'orb_gate' AND s.ts::date = CURRENT_DATE
                ORDER BY s.ts DESC LIMIT 50
            """)).fetchall()
        out = []
        for sid, side, conf, ts, feat, ticker in rows:
            f = feat if isinstance(feat, dict) else json.loads(feat or "{}")
            out.append({
                "security_id": sid, "ticker": ticker or sid,
                "model_side": side, "confidence": float(conf or 0),
                "ts": str(ts),
                "requested_direction": f.get("requested_direction"),
                "verdict": f.get("verdict"),
                "shadow": f.get("shadow", True),
                "data_age_min": f.get("data_age_min"),
                "stale": f.get("stale"),
            })
        return out

    def _calibration():
        # Heavy-ish; separately cached for 10 minutes
        cached_cal = _cache_get("gate_calibration", 600)
        if cached_cal:
            return cached_cal
        from ml.calibration import build_report
        rep = build_report(days=30)
        cal = {
            "recommendation": rep["recommendation"],
            "fresh_n": rep["model_accuracy"]["fresh"]["n"],
            "fresh_accuracy": rep["model_accuracy"]["fresh"]["accuracy"],
            "recommended_min_confidence": rep["recommended_min_confidence"],
            "gate_value": rep["gate_value"],
        }
        _cache_set("gate_calibration", cal)
        return cal

    loop = asyncio.get_running_loop()
    try:
        decisions = await loop.run_in_executor(None, _decisions)
    except Exception as exc:
        decisions = []
        logger.warning("gate decisions query failed: %s", exc)
    try:
        calibration = await loop.run_in_executor(None, _calibration)
    except Exception as exc:
        calibration = None
        logger.warning("calibration summary failed: %s", exc)

    result = {"ok": True, "decisions": decisions, "calibration": calibration}
    _cache_set("gate_panel", result)
    return web.json_response(result)


async def equity_handler(_r: web.Request) -> web.Response:
    """Intraday P&L curve from the equity_curve hypertable (risk engine
    snapshots every ~10s; served as 1-minute buckets). Cached 15s."""
    cached = _cache_get("equity_curve", 15)
    if cached:
        return web.json_response(cached)

    def _query():
        from db import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            rows = conn.execute(text("""
                SELECT time_bucket('1 minute', time) AS t,
                       last(realized_pnl, time)   AS rpnl,
                       last(unrealized_pnl, time) AS upnl,
                       last(total_equity, time)   AS equity
                FROM equity_curve
                WHERE time >= CURRENT_DATE
                GROUP BY 1 ORDER BY 1
            """)).fetchall()
        return {"ok": True, "intraday": [
            {"t": str(r[0])[11:16],
             "pnl": round(float(r[1] or 0) + float(r[2] or 0), 2),
             "equity": round(float(r[3] or 0), 2)}
            for r in rows]}

    try:
        result = await asyncio.get_running_loop().run_in_executor(None, _query)
        _cache_set("equity_curve", result)
        return web.json_response(result)
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc), "intraday": []})


async def status_handler(request: web.Request) -> web.Response:
    hb, alive = read_heartbeat()
    strategies = hb.get("strategies", [])
    first = strategies[0] if strategies else {}
    return web.json_response({
        "mode": hb.get("mode", "PAPER" if cfg.paper_trading else "LIVE"),
        "client_id": cfg.dhan_client_id,
        "uptime_seconds": hb.get("uptime_seconds", 0),
        "trader_alive": alive,
        "strategy_name": f"ORB_{first.get('security_id')}" if first else "none",
        "strategy_running": alive and bool(first.get("running")),
        "orders_placed": sum(s.get("entries_today", 0) for s in strategies),
        "position": first.get("position", 0),
        "entry_price": first.get("entry_price", 0.0),
        "warmup": {"ready": bool(strategies)},
        "kronos_gate": hb.get("kronos_gate", "shadow"),
        "strategies": strategies,
        "note": None if alive else "trader heartbeat stale — is dhan-trader running?",
    })


async def risk_handler(_r: web.Request) -> web.Response:
    hb, alive = read_heartbeat()
    risk = hb.get("risk")
    if not risk:
        return web.json_response({"ok": False, "trader_alive": alive}, status=503)
    return web.json_response({**risk, "trader_alive": alive})


async def feed_handler(_r: web.Request) -> web.Response:
    hb, alive = read_heartbeat()
    feed = hb.get("feed", {})
    return web.json_response({
        "ok": alive,
        "connected": feed.get("connected", False),
        "subscribed": feed.get("subscribed", 0),
        "bars": hb.get("bars", {}),
    })


async def paper_positions_handler(_r: web.Request) -> web.Response:
    hb, _alive = read_heartbeat()
    pf = hb.get("portfolio", {})
    if hb.get("mode") == "LIVE":
        return web.json_response({"ok": True, "count": 0, "data": [],
                                  "note": "Live mode — see /api/positions"})
    positions = [{
        "engine": "EQ", "strategy": p.get("strategy", "ORB"),
        "symbol": p["security_id"], "segment": cfg.watchlist_exchange_segment,
        "qty": p["qty"], "entry_price": p["avg_price"],
    } for p in pf.get("open_positions", [])]
    return web.json_response({
        "ok": True, "count": len(positions), "data": positions,
        "realized_pnl": pf.get("realized_pnl", 0),
        "unrealized_pnl": pf.get("unrealized_pnl", 0),
    })


async def config_handler(_r: web.Request) -> web.Response:
    hb, _ = read_heartbeat()
    return web.json_response({
        "strategy": "orb+kronos",
        "kronos_gate": hb.get("kronos_gate", "shadow"),
        "segment": cfg.watchlist_exchange_segment,
        "watchlist": hb.get("watchlist", []),
        "mode": hb.get("mode"),
    })


async def trading_mode_handler(request: web.Request) -> web.Response:
    if request.method == "POST":
        return web.json_response({
            "ok": False,
            "error": "Mode is fixed per process: set PAPER_TRADING in .env and "
                     "`sudo systemctl restart dhan-trader` (no auth layer until M6)",
        }, status=409)
    hb, _ = read_heartbeat()
    paper = hb.get("mode", "PAPER") == "PAPER"
    return web.json_response({"ok": True, "paper": paper,
                              "mode": "PAPER" if paper else "LIVE"})


async def killswitch_handler(request: web.Request) -> web.Response:
    if (denial := _check_auth(request)) is not None:
        return denial
    RUN_DIR.mkdir(exist_ok=True)
    KILLSWITCH_FILE.write_text(f"dashboard @ {datetime.now(timezone.utc).isoformat()}")
    logger.critical("⛔ KILL SWITCH requested via dashboard — flag written for trader")
    return web.json_response({"ok": True, "halted": True,
                              "message": "Kill switch flag set — trader halts within ~10s"})


async def kronos_live_handler(_r: web.Request) -> web.Response:
    hb, _ = read_heartbeat()
    state = hb.get("kronos_scanner")
    if not state:
        return web.json_response({"ok": False, "error": "scanner not running",
                                  "results": [], "screened_today": {}})
    return web.json_response(state)


# ── Log tail (trader process log) ─────────────────────────────────────────────

async def logs_handler(request: web.Request) -> web.Response:
    limit = int(request.rel_url.query.get("limit", 50))

    def _tail():
        import re
        if not TRADER_LOG.exists():
            return []
        lines = TRADER_LOG.read_text(errors="replace").splitlines()[-limit:]
        today = datetime.now(timezone.utc).date()
        out = []
        for ln in lines:
            parts = ln.split("  ", 2)
            level = parts[1].strip() if len(parts) > 2 else "INFO"
            ts_raw = parts[0].strip() if len(parts) > 2 else ""
            # Old log lines carry HH:MM:SS only — the dashboard runs
            # new Date(ts), so a bare time renders as "Invalid Date".
            # Lift it to full ISO (server logs in UTC).
            if re.fullmatch(r"\d{2}:\d{2}:\d{2}", ts_raw):
                ts = f"{today}T{ts_raw}+00:00"
            else:
                ts = ts_raw
            body = parts[2].lstrip() if len(parts) > 2 else ln
            name = "trader"
            if " — " in body:
                name, body = body.split(" — ", 1)
                name = name.replace("dhan.", "")
            out.append({
                "ts": ts,
                "level": level,
                "icon": {"INFO": "·", "WARNING": "⚠", "ERROR": "✗", "CRITICAL": "⛔"}.get(level, "·"),
                "name": name,
                "msg": body,
            })
        return out

    logs = await asyncio.get_running_loop().run_in_executor(None, _tail)
    return web.json_response({"ok": True, "logs": logs})


# ── DB-backed handlers ────────────────────────────────────────────────────────

async def signals_handler(_r: web.Request) -> web.Response:
    """Trade entries/exits from the trades table, newest first."""
    def _query():
        from db import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            return conn.execute(text("""
                SELECT t.security_id, t.side, t.qty, t.entry_ts, t.entry_price,
                       t.exit_ts, t.exit_price, t.pnl, t.strategy, t.status,
                       COALESCE(NULLIF(i.ticker, ''), t.security_id) AS ticker
                FROM trades t
                LEFT JOIN instruments i ON i.security_id = t.security_id
                WHERE t.entry_ts::date = CURRENT_DATE
                   OR t.exit_ts::date  = CURRENT_DATE
                ORDER BY t.entry_ts DESC LIMIT 100
            """)).fetchall()
    try:
        rows = await asyncio.get_running_loop().run_in_executor(None, _query)
    except Exception as exc:
        return web.json_response([])
    sigs = []
    for sid, side, qty, ets, ep, xts, xp, pnl, strat, status, ticker in rows:
        sigs.append({"action": side, "price": float(ep or 0),
                     "reason": f"{strat} entry x{qty}",
                     "timestamp": str(ets), "source": f"{strat} {ticker}"})
        if xts:
            sigs.append({"action": "EXIT", "price": float(xp or 0),
                         "reason": f"{strat} exit  PnL ₹{float(pnl or 0):+.2f}",
                         "timestamp": str(xts), "source": f"{strat} {ticker}"})
    sigs.sort(key=lambda x: x["timestamp"], reverse=True)
    return web.json_response(sigs[:100])


async def trades_handler(request: web.Request) -> web.Response:
    limit = int(request.rel_url.query.get("limit", 200))

    def _query():
        from db import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            rows = conn.execute(text("""
                SELECT t.security_id, t.side, t.qty, t.entry_ts, t.entry_price,
                       t.exit_ts, t.exit_price, t.pnl, t.strategy, t.status,
                       COALESCE(NULLIF(i.ticker, ''), t.security_id) AS ticker
                FROM trades t
                LEFT JOIN instruments i ON i.security_id = t.security_id
                ORDER BY t.entry_ts DESC LIMIT :lim
            """), {"lim": limit}).fetchall()
            summary = conn.execute(text("""
                SELECT COUNT(*) FILTER (WHERE status='CLOSED'),
                       COALESCE(SUM(pnl) FILTER (WHERE status='CLOSED'), 0),
                       COUNT(*) FILTER (WHERE status='CLOSED' AND pnl > 0)
                FROM trades WHERE entry_ts::date = CURRENT_DATE
            """)).fetchone()
        return rows, summary

    try:
        rows, summary = await asyncio.get_running_loop().run_in_executor(None, _query)
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc), "trades": []})

    closed, pnl_sum, wins = summary or (0, 0, 0)
    return web.json_response({
        "ok": True, "count": len(rows),
        "summary": {"closed_today": int(closed or 0),
                    "pnl_today": float(pnl_sum or 0),
                    "wins_today": int(wins or 0)},
        "trades": [{
            "symbol": r[10], "security_id": r[0], "action": r[1], "qty": r[2],
            "entry_ts": str(r[3]), "entry_price": float(r[4] or 0),
            "exit_ts": str(r[5]) if r[5] else None,
            "exit_price": float(r[6] or 0) if r[6] is not None else None,
            "pnl": float(r[7] or 0) if r[7] is not None else None,
            "strategy": r[8], "status": r[9],
        } for r in rows],
    })


# Simple TTL cache for slow endpoints
_CACHE: dict = {}

def _cache_get(key: str, ttl: int):
    e = _CACHE.get(key)
    return e["val"] if e and (time.monotonic() - e["ts"]) < ttl else None

def _cache_set(key: str, val):
    _CACHE[key] = {"val": val, "ts": time.monotonic()}


async def db_stats_handler(_r: web.Request) -> web.Response:
    """DB health + sizes using ONLY instant queries. Cached 60s.

    The old version ran COUNT(*) over the bars hypertable (332M+ rows) —
    minutes under backfill write load, so the panel sat on "Connecting…"
    forever. approximate_row_count() reads planner stats, hypertable_size()
    reads the catalog — both answer in milliseconds.
    """
    cached = _cache_get("db_stats", 60)
    if cached:
        return web.json_response(cached)

    def _query():
        import time as _t
        from db import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            t0 = _t.monotonic()
            conn.execute(text("SELECT 1"))
            ping_ms = round((_t.monotonic() - t0) * 1000, 1)

            db_size = conn.execute(text(
                "SELECT pg_size_pretty(pg_database_size(current_database()))")).scalar()
            hts = conn.execute(text("""
                SELECT hypertable_name,
                       pg_size_pretty(hypertable_size(format('%I.%I', hypertable_schema, hypertable_name)::regclass)),
                       approximate_row_count(format('%I.%I', hypertable_schema, hypertable_name)::regclass)
                FROM timescaledb_information.hypertables
            """)).fetchall()
            compression = conn.execute(text("""
                SELECT hypertable_name, count(*) FILTER (WHERE is_compressed), count(*)
                FROM timescaledb_information.chunks GROUP BY 1
            """)).fetchall()
            # Span from the chunk CATALOG — ORDER BY time LIMIT 1 walks into
            # compressed chunks (decompression, minutes); chunk ranges are
            # free and accurate to within one chunk interval.
            earliest, latest = conn.execute(text("""
                SELECT min(range_start)::date, max(range_end)::date
                FROM timescaledb_information.chunks WHERE hypertable_name = 'bars'
            """)).fetchone()
            instruments = conn.execute(text(
                "SELECT exchange_segment, COUNT(*) FROM instruments GROUP BY exchange_segment"
            )).fetchall()
            signals_count = conn.execute(text("SELECT COUNT(*) FROM signals")).scalar()
            trades_count = conn.execute(text("SELECT COUNT(*) FROM trades")).scalar()
            alembic = conn.execute(text(
                "SELECT version_num FROM alembic_version")).scalar()

        comp = {r[0]: {"compressed": r[1], "total": r[2]} for r in compression}
        return {
            "ok": True, "up": True, "ping_ms": ping_ms,
            "db_size": db_size, "alembic": alembic,
            "hypertables": [{
                "name": r[0], "size": r[1], "approx_rows": int(r[2] or 0),
                "chunks_compressed": comp.get(r[0], {}).get("compressed", 0),
                "chunks_total": comp.get(r[0], {}).get("total", 0),
            } for r in hts],
            "bars_span": {"earliest": str(earliest), "latest": str(latest)},
            "instruments": {r[0]: r[1] for r in instruments},
            "signals": signals_count, "trades": trades_count,
        }

    try:
        result = await asyncio.get_running_loop().run_in_executor(None, _query)
        _cache_set("db_stats", result)
        return web.json_response(result)
    except Exception as exc:
        return web.json_response({"ok": False, "up": False, "error": str(exc)})


async def kronos_signals_handler(request: web.Request) -> web.Response:
    limit = int(request.rel_url.query.get("limit", 50))

    def _query():
        from db import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            # TODAY only. Without this the panel showed days-old signals
            # (Jun-5 scanner rows, Jun-12 gate verdicts) as if current —
            # a "strong buy" that was really 3-10 days stale. The dashboard
            # reflects live state; an empty panel when nothing fired today
            # is correct, not a bug.
            return conn.execute(text("""
                SELECT s.security_id, s.side, s.score, s.confidence, s.strategy, s.ts,
                       s.features_snapshot, i.ticker, i.name
                FROM signals s
                LEFT JOIN instruments i ON i.security_id = s.security_id
                WHERE s.ts::date = CURRENT_DATE
                ORDER BY s.ts DESC LIMIT :lim
            """), {"lim": limit}).fetchall()

    try:
        rows = await asyncio.get_running_loop().run_in_executor(None, _query)
        return web.json_response({"ok": True, "signals": [
            {"security_id": r[0], "side": r[1], "score": float(r[2] or 0),
             "confidence": float(r[3] or 0), "strategy": r[4], "ts": str(r[5]),
             "features": r[6], "ticker": r[7] or r[0], "name": r[8] or ""}
            for r in rows]})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc), "signals": []})


async def kronos_screener_handler(request: web.Request) -> web.Response:
    n = int(request.rel_url.query.get("n", 20))
    cached = _cache_get(f"screener_{n}", 300)   # ATR ranking barely moves intraday
    if cached:
        return web.json_response(cached)

    def _query():
        from core.nse_screener import get_top_volatile
        from db import get_engine
        from sqlalchemy import text
        candidates = get_top_volatile(n=n)
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
            c["name"] = meta.get("name", "")
        return candidates

    try:
        candidates = await asyncio.get_running_loop().run_in_executor(None, _query)
        result = {"ok": True, "candidates": candidates, "count": len(candidates)}
        if candidates:
            _cache_set(f"screener_{n}", result)
        return web.json_response(result)
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc), "candidates": []})


# ── Dhan read-only client (funds / positions / LTP) ───────────────────────────

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


async def funds_handler(_r: web.Request) -> web.Response:
    try:
        data = await _dhan_ro.call("get_funds")
        return web.json_response({"ok": True, "data": data})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=503)


async def positions_handler(_r: web.Request) -> web.Response:
    try:
        data = await _dhan_ro.call("get_positions")
        return web.json_response({"ok": True, "data": data})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=503)


async def instrument_price_handler(request: web.Request) -> web.Response:
    sid = request.rel_url.query.get("security_id", "")
    segment = request.rel_url.query.get("segment", "NSE_EQ")
    if not sid:
        return web.json_response({"ok": False, "error": "security_id required"}, status=400)
    seg_map = {"NSE_EQ": "NSE_EQ", "NSE_FNO": "NSE_FNO", "MCX": "MCX_COMM"}
    api_seg = seg_map.get(segment, segment)
    try:
        data = await _dhan_ro.call("get_ltp", {api_seg: [int(sid)]})
        price = data.get("data", {}).get(api_seg, {}).get(sid, {}).get("last_price", 0.0)
        return web.json_response({"ok": True, "security_id": sid, "price": price,
                                  "segment": segment})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=503)


async def instrument_search_handler(request: web.Request) -> web.Response:
    from core.instruments import InstrumentMaster
    q = request.rel_url.query.get("q", "").strip()
    segment = request.rel_url.query.get("segment", "NSE_EQ")
    if len(q) < 2:
        return web.json_response({"ok": False, "error": "Query must be at least 2 characters"},
                                 status=400)
    results = await asyncio.get_running_loop().run_in_executor(
        None, InstrumentMaster.search_instruments, q, segment)
    return web.json_response({"ok": True, "results": results})


# ── Misc ──────────────────────────────────────────────────────────────────────

async def market_status_handler(_r: web.Request) -> web.Response:
    from datetime import time as dtime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    t, wd = now.time(), now.weekday()
    is_weekday, is_saturday = wd < 5, wd == 5
    nse = "OPEN" if is_weekday and dtime(9, 15) <= t <= dtime(15, 30) else "CLOSED"
    pre = "PRE" if is_weekday and dtime(9, 0) <= t < dtime(9, 15) else None
    if is_weekday:
        mcx = "OPEN" if dtime(9, 0) <= t <= dtime(23, 30) else "CLOSED"
    elif is_saturday:
        mcx = "OPEN" if dtime(9, 0) <= t <= dtime(14, 0) else "CLOSED"
    else:
        mcx = "CLOSED"
    return web.json_response({
        "nse_equity": pre or nse, "nse_fno": pre or nse, "mcx": mcx,
        "ist_time": now.strftime("%H:%M:%S"), "weekday": now.strftime("%A"),
        "is_weekend": not is_weekday and not is_saturday,
    })


async def watchlist_handler(request: web.Request) -> web.Response:
    wl = request.app.get("watchlist")
    if not wl:
        return web.json_response({"ok": False, "error": "Watchlist not initialised"}, status=503)
    return web.json_response({"ok": True, **wl.summary()})


async def watchlist_refresh_handler(request: web.Request) -> web.Response:
    if (denial := _check_auth(request)) is not None:
        return denial
    wl = request.app.get("watchlist")
    if not wl:
        return web.json_response({"ok": False, "error": "Watchlist not initialised"}, status=503)
    try:
        await wl.refresh()
        return web.json_response({"ok": True, "count": len(wl.get()),
                                  "stocks": [s.symbol for s in wl.get()]})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def backfill_status_handler(_r: web.Request) -> web.Response:
    """Backfill progress: checkpoint (authoritative) + process + log tail."""
    import subprocess
    ckpt = {}
    try:
        ckpt = json.loads(Path("/opt/dhan-trading/backfill_ckpt_NSE_EQ.json").read_text())
        ckpt["pct"] = round(ckpt["index"] / max(ckpt["total"], 1) * 100, 1)
    except Exception:
        pass
    lines: list[str] = []
    try:
        result = subprocess.run(["tail", "-30", "/tmp/backfill.log"],
                                capture_output=True, text=True, timeout=3)
        lines = [l for l in result.stdout.strip().split("\n") if l]
    except Exception:
        pass
    running = subprocess.run(["pgrep", "-f", "backfill.py"],
                             capture_output=True).returncode == 0
    return web.json_response({"ok": True, "running": running,
                              "checkpoint": ckpt, "log_tail": lines})


async def system_health_handler(_r: web.Request) -> web.Response:
    """Automation health: cron schedule, alert channel, recent error count.
    Replaces the Hermes panel — the gateway was retired 2026-06-11."""
    cached = _cache_get("system_health", 30)
    if cached:
        return web.json_response(cached)

    def _collect():
        import subprocess
        crons = []
        try:
            out = subprocess.run(["crontab", "-l"], capture_output=True,
                                 text=True, timeout=3).stdout
            for ln in out.splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    sched = " ".join(ln.split()[:5])
                    label = ("backfill watchdog" if "backfill_resume" in ln else
                             "EOD summary" if "eod_summary" in ln else
                             "calibration" if "ml.calibration" in ln else
                             ln.split("&&")[-1][:40])
                    crons.append({"schedule": sched, "job": label})
        except Exception:
            pass
        errors_today = 0
        try:
            log = Path("/var/log/dhan/trader.log").read_text(errors="replace")
            today = datetime.now(timezone.utc).date().isoformat()
            errors_today = sum(1 for l in log.splitlines()
                               if l.startswith(today) and ("ERROR" in l or "CRITICAL" in l))
        except Exception:
            pass
        return {
            "ok": True,
            "telegram_configured": bool(cfg.telegram_bot_token and cfg.telegram_chat_id),
            "crons": crons,
            "trader_errors_today": errors_today,
            "hermes": "retired 2026-06-11 — plain Telegram alerts via core/notify.py",
        }

    result = await asyncio.get_running_loop().run_in_executor(None, _collect)
    _cache_set("system_health", result)
    return web.json_response(result)


async def dashboard_handler(_r: web.Request) -> web.Response:
    react_index = DIST_DIR / "index.html"
    path = react_index if react_index.exists() else STATIC_DIR / "index.html"
    # index.html must always revalidate — the JS bundles it points at are
    # content-hashed, so a cached index can pin users to a stale build
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


async def postback_handler(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        logger.info("📬 Postback: %s order %s → %s",
                    payload.get("tradingSymbol", "?"), payload.get("orderId"),
                    payload.get("orderStatus"))
        return web.json_response({"ack": "ok"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


# ── App ───────────────────────────────────────────────────────────────────────

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
    app.router.add_get("/api/backfill/status", backfill_status_handler)
    app.router.add_get("/api/system/health", system_health_handler)
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
    site = web.TCPSite(runner, "0.0.0.0", cfg.webhook_port)
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
