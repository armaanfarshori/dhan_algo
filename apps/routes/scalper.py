"""Scalper dashboard API handlers — three read-only GETs + one auth-gated control POST.

Surfaces the intraday options-scalper PAPER track.  The sleeve was backtested on
real minute data in June 2026 and came out decisively negative-EV, so it ships
dark behind cfg.scalper_enabled (default False) and never trades live:

  GET  /api/scalper/live      — live scalper state from the trader heartbeat (hb["scalper"])
  GET  /api/scalper/trades    — scalper_trades summary + list (net-of-charges)
  GET  /api/scalper/governor  — governor limits vs today's running totals
  POST /api/scalper/control   — {action: start|stop}; auth-gated, writes a run/ flag

Conventions mirror apps/routes/fno.py:
  * heavy work runs in the thread executor via the shared _db_query helper
  * DB access uses get_engine() + SQLAlchemy text() with BOUND parameters (never f-string SQL)
  * every handler returns a safe empty shell on error / empty tables — never raises to client
  * the control POST reuses apps.api._check_auth (same shared-secret gate as the kill switch)
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import web

logger = logging.getLogger("dhan.api")
_IST = ZoneInfo("Asia/Kolkata")


# ── 1. GET /api/scalper/live ──────────────────────────────────────────────────

def _scalper_enabled() -> bool:
    """cfg.scalper_enabled, read at call time (never cached).

    The dashboard needs this to know whether Start can do anything at all:
    with the flag off the trader creates no orchestrator, so the run/ flag
    would be written and then silently ignored.  Resolved through apps.api.cfg
    so tests can monkeypatch it like every other config read here.
    """
    try:
        from apps.api import cfg
        return bool(cfg.scalper_enabled)
    except Exception:
        return False


async def scalper_live_handler(_r: web.Request) -> web.Response:
    """Live scalper state from the trader heartbeat (hb["scalper"]).

    The scalper may be OFF (key absent or None) — then we return a calm
    available:false shell, never an error. Pure heartbeat read; no DB.

    `enabled` mirrors cfg.scalper_enabled (the ships-dark feature flag) so the
    control bar can disable Start instead of pretending it will do something.
    """
    from apps.api import read_heartbeat

    enabled = _scalper_enabled()
    try:
        hb, alive = read_heartbeat()
        state = hb.get("scalper")
        if not state:
            return web.json_response({
                "ok": True, "available": False, "enabled": enabled,
                "trader_alive": alive, "state": None,
            })
        return web.json_response({
            "ok": True, "available": True, "enabled": enabled,
            "trader_alive": alive, "state": state,
        })
    except Exception:
        logger.exception("scalper_live_handler failed")
        return web.json_response({
            "ok": False, "error": "internal error",
            "available": False, "enabled": enabled,
            "trader_alive": False, "state": None,
        })


# ── 2. GET /api/scalper/trades ────────────────────────────────────────────────

def _empty_trades_shell(ok: bool = True) -> dict:
    return {
        "ok": ok,
        **({} if ok else {"error": "internal error"}),
        "summary": {
            "closed": 0, "open": 0, "net_pnl": 0.0,
            "win_rate": 0.0, "wins": 0, "avg_pnl": 0.0,
        },
        "trades": [],
    }


async def scalper_trades_handler(request: web.Request) -> web.Response:
    """Scalper trade log: summary stats + trade list (net-of-charges).

    GET /api/scalper/trades?limit=200  (clamp 1–500, cache 30 s).
    Summary computed over CLOSED trades only. Empty/absent table → zeros + [].
    """
    from apps.api import _cache_get, _cache_set, _db_query

    try:
        limit = int(request.rel_url.query.get("limit", 200))
    except (ValueError, TypeError):
        limit = 200
    limit = max(1, min(limit, 500))

    cache_key = f"scalper_trades_{limit}"
    cached = _cache_get(cache_key, 30)
    if cached:
        return web.json_response(cached)

    def _query():
        from db import get_engine
        from sqlalchemy import text

        with get_engine().connect() as conn:
            summary_row = conn.execute(text("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'CLOSED')                       AS closed,
                    COUNT(*) FILTER (WHERE status = 'OPEN')                         AS open_count,
                    COALESCE(SUM(net_pnl) FILTER (WHERE status = 'CLOSED'), 0)      AS net_pnl,
                    COUNT(*) FILTER (WHERE status = 'CLOSED' AND win = TRUE)        AS wins
                FROM scalper_trades
            """)).fetchone()

            trade_rows = conn.execute(text("""
                SELECT
                    id, symbol, trade_date, status,
                    direction, option_type, strike, lots,
                    entry_time, entry_premium, entry_underlying, entry_reason,
                    exit_time, exit_premium, exit_reason,
                    net_pnl, win
                FROM scalper_trades
                ORDER BY COALESCE(entry_time, created_at) DESC, id DESC
                LIMIT :lim
            """), {"lim": limit}).fetchall()

        closed = int(summary_row[0] or 0)
        open_count = int(summary_row[1] or 0)
        net_pnl = float(summary_row[2] or 0)
        wins = int(summary_row[3] or 0)
        win_rate = (wins / closed) if closed > 0 else 0.0
        avg_pnl = (net_pnl / closed) if closed > 0 else 0.0

        trades = []
        for row in trade_rows:
            (tid, symbol, trade_date, status, direction, option_type, strike, lots,
             entry_time, entry_premium, entry_underlying, entry_reason,
             exit_time, exit_premium, exit_reason, net_pnl_t, win) = row
            trades.append({
                "id": tid,
                "symbol": symbol,
                "trade_date": str(trade_date) if trade_date else None,
                "status": status,
                "direction": direction,
                "option_type": option_type,
                "strike": int(strike) if strike is not None else None,
                "lots": int(lots) if lots is not None else None,
                "entry_time": entry_time.isoformat() if entry_time else None,
                "entry_premium": float(entry_premium) if entry_premium is not None else None,
                "entry_underlying": float(entry_underlying) if entry_underlying is not None else None,
                "entry_reason": entry_reason,
                "exit_time": exit_time.isoformat() if exit_time else None,
                "exit_premium": float(exit_premium) if exit_premium is not None else None,
                "exit_reason": exit_reason,
                "net_pnl": float(net_pnl_t) if net_pnl_t is not None else None,
                "win": win,
            })

        return {
            "ok": True,
            "summary": {
                "closed": closed,
                "open": open_count,
                "net_pnl": net_pnl,
                "win_rate": round(win_rate, 4),
                "wins": wins,
                "avg_pnl": round(avg_pnl, 2),
            },
            "trades": trades,
        }

    try:
        result = await _db_query(_query)
        if result["summary"]["closed"] or result["summary"]["open"]:
            _cache_set(cache_key, result)  # don't cache an empty bootstrap shell
        return web.json_response(result)
    except Exception:
        logger.exception("scalper_trades_handler failed")
        return web.json_response(_empty_trades_shell(ok=False))


# ── 3. GET /api/scalper/governor ──────────────────────────────────────────────

def _empty_governor_shell(ok: bool = True) -> dict:
    return {
        "ok": ok,
        **({} if ok else {"error": "internal error"}),
        "available": False,
        "trade_date": None,
        "state": None,
        "limits": {
            "daily_loss_cap": None, "profit_lock_arm": None,
            "profit_lock_giveback": None, "profit_lock_mode": None,
            "max_trades": None, "consec_loss_limit": None,
        },
        "totals": {
            "realized_pnl": 0.0, "trades_today": 0, "consec_losses": 0,
            "profit_floor": None, "profit_hi_water": None, "lock_armed": False,
        },
    }


async def scalper_governor_handler(_r: web.Request) -> web.Response:
    """Daily Risk Governor: configured limits vs today's running totals.

    Cache 30 s. Uses datetime.now(Asia/Kolkata).date() for "today" so the
    session date matches the IST trading day on a UTC host (CI date trap).
    Empty/absent table or no row for today → available:false shell.
    """
    from apps.api import _cache_get, _cache_set, _db_query

    cached = _cache_get("scalper_governor", 30)
    if cached:
        return web.json_response(cached)

    # IST trading day — never date.today()/naive now() (UTC-CI date mismatch).
    today = datetime.now(_IST).date()

    def _query():
        from db import get_engine
        from sqlalchemy import text

        with get_engine().connect() as conn:
            row = conn.execute(text("""
                SELECT
                    trade_date, state,
                    daily_loss_cap, profit_lock_arm, profit_lock_giveback,
                    profit_lock_mode, max_trades, consec_loss_limit,
                    realized_pnl, trades_today, consec_losses,
                    profit_floor, profit_hi_water, lock_armed
                FROM scalper_governor
                WHERE symbol = :sym AND trade_date = :td
                ORDER BY updated_at DESC
                LIMIT 1
            """), {"sym": "NIFTY", "td": today}).fetchone()

        if row is None:
            return None  # signal empty → available:false shell

        (trade_date, state,
         daily_loss_cap, profit_lock_arm, profit_lock_giveback,
         profit_lock_mode, max_trades, consec_loss_limit,
         realized_pnl, trades_today, consec_losses,
         profit_floor, profit_hi_water, lock_armed) = row

        return {
            "ok": True,
            "available": True,
            "trade_date": str(trade_date) if trade_date else None,
            "state": state,
            "limits": {
                "daily_loss_cap": float(daily_loss_cap) if daily_loss_cap is not None else None,
                "profit_lock_arm": float(profit_lock_arm) if profit_lock_arm is not None else None,
                "profit_lock_giveback": float(profit_lock_giveback) if profit_lock_giveback is not None else None,
                "profit_lock_mode": profit_lock_mode,
                "max_trades": int(max_trades) if max_trades is not None else None,
                "consec_loss_limit": int(consec_loss_limit) if consec_loss_limit is not None else None,
            },
            "totals": {
                "realized_pnl": float(realized_pnl) if realized_pnl is not None else 0.0,
                "trades_today": int(trades_today) if trades_today is not None else 0,
                "consec_losses": int(consec_losses) if consec_losses is not None else 0,
                "profit_floor": float(profit_floor) if profit_floor is not None else None,
                "profit_hi_water": float(profit_hi_water) if profit_hi_water is not None else None,
                "lock_armed": bool(lock_armed),
            },
        }

    try:
        result = await _db_query(_query)
        if result is None:
            return web.json_response(_empty_governor_shell(ok=True))
        _cache_set("scalper_governor", result)
        return web.json_response(result)
    except Exception:
        logger.exception("scalper_governor_handler failed")
        return web.json_response(_empty_governor_shell(ok=False))


# ── 4. POST /api/scalper/control ──────────────────────────────────────────────

async def scalper_control_handler(request: web.Request) -> web.Response:
    """Start/stop the scalper via a run/ flag file the trader consumes.

    Body: {"action": "start" | "stop"}.
    Auth-gated by the EXISTING shared-secret check (apps.api._check_auth), same as
    the kill switch: token unset → fail-open; token set → X-Dashboard-Token /
    Authorization: Bearer required, else 401.

    Writes run/scalper_start (action=start) or run/scalper_stop (action=stop) and,
    so the latest intent wins unambiguously, removes the opposite flag.
    """
    import apps.api as _api

    if (denial := _api._check_auth(request)) is not None:
        return denial

    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body or {}).get("action")
    if action not in ("start", "stop"):
        return web.json_response(
            {"ok": False, "error": "action must be 'start' or 'stop'"}, status=400)

    try:
        _api.RUN_DIR.mkdir(exist_ok=True)
        stamp = f"dashboard @ {datetime.now(_IST).isoformat()}"
        start_flag = _api.RUN_DIR / "scalper_start"
        stop_flag = _api.RUN_DIR / "scalper_stop"
        if action == "start":
            start_flag.write_text(stamp)
            stop_flag.unlink(missing_ok=True)
        else:
            stop_flag.write_text(stamp)
            start_flag.unlink(missing_ok=True)
    except Exception as exc:
        # Never raise to the client (convention) and never leak the OS path in
        # the body — keep the full error in the server log only.
        logger.error("scalper control flag write failed: %s", exc)
        return web.json_response(
            {"ok": False, "error": "internal error"}, status=500)

    _api.logger.warning("Scalper %s requested via dashboard — flag written for trader", action)
    return web.json_response({
        "ok": True, "action": action,
        "message": f"Scalper {action} flag set — trader applies within ~10s",
    })
