"""DB-backed API handlers — all heavy queries run in the thread executor via
the shared _db_query helper from apps.api.

Never COUNT(*) or ORDER BY time LIMIT 1 on the bars hypertable (300M+ rows).
Use approximate_row_count() / hypertable_size() / chunk-catalog ranges.
"""
import json
import logging

from aiohttp import web

logger = logging.getLogger("dhan.api")


async def equity_handler(_r: web.Request) -> web.Response:
    """Intraday P&L curve from the equity_curve hypertable (risk engine
    snapshots every ~10s; served as 1-minute buckets). Cached 15s."""
    from apps.api import _cache_get, _cache_set, _db_query
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
                WHERE time >= timezone('Asia/Kolkata', date_trunc('day', timezone('Asia/Kolkata', now())))  -- IST trading day (not UTC CURRENT_DATE)
                GROUP BY 1 ORDER BY 1
            """)).fetchall()
        return {"ok": True, "intraday": [
            {"t": str(r[0])[11:16],
             "pnl": round(float(r[1] or 0) + float(r[2] or 0), 2),
             "equity": round(float(r[3] or 0), 2)}
            for r in rows]}

    try:
        result = await _db_query(_query)
        _cache_set("equity_curve", result)
        return web.json_response(result)
    except Exception:
        logger.exception("equity_handler failed")
        return web.json_response({"ok": False, "error": "internal error", "intraday": []})


async def signals_handler(_r: web.Request) -> web.Response:
    """Trade entries/exits from the trades table, newest first."""
    from apps.api import _db_query

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
                WHERE (t.entry_ts AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date  -- IST trading day (not UTC CURRENT_DATE)
                   OR (t.exit_ts  AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date  -- IST trading day (not UTC CURRENT_DATE)
                ORDER BY t.entry_ts DESC LIMIT 100
            """)).fetchall()

    try:
        rows = await _db_query(_query)
    except Exception:
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
    from apps.api import _db_query
    limit = int(request.rel_url.query.get("limit", 200))
    limit = max(1, min(limit, 500))

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
                FROM trades WHERE (entry_ts AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date  -- IST trading day (not UTC CURRENT_DATE)
            """)).fetchone()
        return rows, summary

    try:
        rows, summary = await _db_query(_query)
    except Exception:
        logger.exception("trades_handler failed")
        return web.json_response({"ok": False, "error": "internal error", "trades": []})

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


async def db_stats_handler(_r: web.Request) -> web.Response:
    """DB health + sizes using ONLY instant queries. Cached 60s.

    The old version ran COUNT(*) over the bars hypertable (332M+ rows) —
    minutes under backfill write load, so the panel sat on "Connecting…"
    forever. approximate_row_count() reads planner stats, hypertable_size()
    reads the catalog — both answer in milliseconds.
    """
    from apps.api import _cache_get, _cache_set, _db_query
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
        result = await _db_query(_query)
        _cache_set("db_stats", result)
        return web.json_response(result)
    except Exception:
        logger.exception("db_stats_handler failed")
        return web.json_response({"ok": False, "up": False, "error": "internal error"})


async def kronos_gate_handler(_r: web.Request) -> web.Response:
    """Today's persisted gate verdicts + the latest calibration verdict."""
    from apps.api import _cache_get, _cache_set, _db_query
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
                WHERE s.strategy = 'orb_gate' AND (s.ts AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date  -- IST trading day (not UTC CURRENT_DATE)
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

    try:
        decisions = await _db_query(_decisions)
    except Exception as exc:
        decisions = []
        logger.warning("gate decisions query failed: %s", exc)
    try:
        calibration = await _db_query(_calibration)
    except Exception as exc:
        calibration = None
        logger.warning("calibration summary failed: %s", exc)

    result = {"ok": True, "decisions": decisions, "calibration": calibration}
    _cache_set("gate_panel", result)
    return web.json_response(result)


async def kronos_signals_handler(request: web.Request) -> web.Response:
    from apps.api import _db_query
    limit = int(request.rel_url.query.get("limit", 50))
    limit = max(1, min(limit, 500))

    def _query():
        from db import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            # TODAY only — dashboard reflects live state; an empty panel when
            # nothing fired today is correct, not a bug.
            return conn.execute(text("""
                SELECT s.security_id, s.side, s.score, s.confidence, s.strategy, s.ts,
                       s.features_snapshot, i.ticker, i.name
                FROM signals s
                LEFT JOIN instruments i ON i.security_id = s.security_id
                WHERE (s.ts AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date  -- IST trading day (not UTC CURRENT_DATE)
                ORDER BY s.ts DESC LIMIT :lim
            """), {"lim": limit}).fetchall()

    try:
        rows = await _db_query(_query)
        return web.json_response({"ok": True, "signals": [
            {"security_id": r[0], "side": r[1], "score": float(r[2] or 0),
             "confidence": float(r[3] or 0), "strategy": r[4], "ts": str(r[5]),
             "features": r[6], "ticker": r[7] or r[0], "name": r[8] or ""}
            for r in rows]})
    except Exception:
        logger.exception("kronos_signals_handler failed")
        return web.json_response({"ok": False, "error": "internal error", "signals": []})


async def kronos_screener_handler(request: web.Request) -> web.Response:
    from apps.api import _cache_get, _cache_set, _db_query
    n = int(request.rel_url.query.get("n", 20))
    n = max(1, min(n, 100))
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
        candidates = await _db_query(_query)
        result = {"ok": True, "candidates": candidates, "count": len(candidates)}
        if candidates:
            _cache_set(f"screener_{n}", result)
        return web.json_response(result)
    except Exception:
        logger.exception("kronos_screener_handler failed")
        return web.json_response({"ok": False, "error": "internal error", "candidates": []})


async def rate_limits_handler(_r: web.Request) -> web.Response:
    """Today's API call counts aggregated across all processes, with per-day caps."""
    from apps.api import _db_query

    def _query():
        from core.api_usage import query_today_totals
        return query_today_totals()

    try:
        result = await _db_query(_query)
        return web.json_response({"ok": True, **result})
    except Exception:
        logger.exception("rate_limits_handler failed")
        return web.json_response({"ok": False, "error": "internal error"}, status=500)
