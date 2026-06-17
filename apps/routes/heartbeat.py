"""Handlers that read exclusively from the trader heartbeat file.

No DB access; these are fast (<1 ms) and form the dashboard's high-frequency
polling surface.
"""
from datetime import datetime, timezone

from aiohttp import web


def _ctx(request: web.Request):
    """Pull shared references from the module-level api namespace."""
    import apps.api as _api
    return _api.read_heartbeat, _api.cfg, _api._check_auth, _api.RUN_DIR, _api.KILLSWITCH_FILE


async def health_handler(_r: web.Request) -> web.Response:
    from apps.api import read_heartbeat
    hb, alive = read_heartbeat()
    return web.json_response({
        "status": "ok",
        "trader_alive": alive,
        "paper": hb.get("mode", "PAPER") == "PAPER",
    })


async def snapshot_handler(_r: web.Request) -> web.Response:
    """One payload for the dashboard's fast loop — replaces 5 separate 1s
    pollers. Everything here is a file read; never touches the DB."""
    from apps.api import read_heartbeat, cfg
    hb, alive = read_heartbeat()
    return web.json_response({
        "ok": True,
        "alive": alive,
        "ts": datetime.now(timezone.utc).isoformat(),
        "trader": hb,
        "limits": {
            "max_daily_loss": round(
                (cfg.paper_balance if cfg.paper_trading else cfg.capital)
                * cfg.max_daily_loss_pct
                * (1.0 if cfg.paper_trading else cfg.live_risk_scale)),
            "paper_balance": cfg.paper_balance,
            "max_orders_per_session": cfg.max_orders_per_session,
            "max_open_positions": cfg.max_open_positions,
        },
    })


async def status_handler(request: web.Request) -> web.Response:
    from apps.api import read_heartbeat, cfg
    hb, alive = read_heartbeat()
    strategies = hb.get("strategies", [])
    first = strategies[0] if strategies else {}
    return web.json_response({
        "mode": hb.get("mode", "PAPER" if cfg.paper_trading else "LIVE"),
        "client_id": "****" + str(cfg.dhan_client_id)[-4:],
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
    from apps.api import read_heartbeat
    hb, alive = read_heartbeat()
    risk = hb.get("risk")
    if not risk:
        return web.json_response({"ok": False, "trader_alive": alive}, status=503)
    return web.json_response({**risk, "trader_alive": alive})


async def feed_handler(_r: web.Request) -> web.Response:
    from apps.api import read_heartbeat
    hb, alive = read_heartbeat()
    feed = hb.get("feed", {})
    return web.json_response({
        "ok": alive,
        "connected": feed.get("connected", False),
        "subscribed": feed.get("subscribed", 0),
        "bars": hb.get("bars", {}),
    })


async def paper_positions_handler(_r: web.Request) -> web.Response:
    from apps.api import read_heartbeat, cfg
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
    from apps.api import read_heartbeat, cfg
    hb, _ = read_heartbeat()
    return web.json_response({
        "strategy": "orb+kronos",
        "kronos_gate": hb.get("kronos_gate", "shadow"),
        "segment": cfg.watchlist_exchange_segment,
        "watchlist": hb.get("watchlist", []),
        "mode": hb.get("mode"),
    })


async def trading_mode_handler(request: web.Request) -> web.Response:
    from apps.api import read_heartbeat
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
    import apps.api as _api
    if (denial := _api._check_auth(request)) is not None:
        return denial
    _api.RUN_DIR.mkdir(exist_ok=True)
    _api.KILLSWITCH_FILE.write_text(
        f"dashboard @ {datetime.now(timezone.utc).isoformat()}")
    _api.logger.critical(
        "⛔ KILL SWITCH requested via dashboard — flag written for trader")
    return web.json_response({"ok": True, "halted": True,
                              "message": "Kill switch flag set — trader halts within ~10s"})


async def resume_handler(request: web.Request) -> web.Response:
    """Clear a halt (kill-switch or loss) from the dashboard. Auth-gated like the
    kill switch. Drops a `run/resume` flag the trader's risk loop consumes within
    ~10s; also removes a stale `run/killswitch` so it can't immediately re-trip.
    A still-breached daily/weekly loss budget will re-halt on the next tick — by
    design (you can't click past a real loss limit)."""
    import apps.api as _api
    if (denial := _api._check_auth(request)) is not None:
        return denial
    _api.RUN_DIR.mkdir(exist_ok=True)
    if _api.KILLSWITCH_FILE.exists():
        _api.KILLSWITCH_FILE.unlink()
    _api.RESUME_FILE.write_text(
        f"dashboard @ {datetime.now(timezone.utc).isoformat()}")
    _api.logger.warning("▶ RESUME requested via dashboard — flag written for trader")
    return web.json_response({"ok": True, "halted": False,
                              "message": "Resume flag set — trader re-arms within ~10s "
                                         "(re-halts if a loss budget is still breached)"})


async def kronos_live_handler(_r: web.Request) -> web.Response:
    from apps.api import read_heartbeat
    hb, _ = read_heartbeat()
    state = hb.get("kronos_scanner")
    if not state:
        return web.json_response({"ok": False, "error": "scanner not running",
                                  "results": [], "screened_today": {}})
    return web.json_response(state)
