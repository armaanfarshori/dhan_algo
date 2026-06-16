"""Market data + Dhan read-only client handlers.

Includes: funds, positions, instrument search/price, market status,
watchlist read + refresh.

The _ReadOnlyDhan class lives in apps.api (single instance, _dhan_ro).
"""
import asyncio
import logging
from datetime import datetime

from aiohttp import web

logger = logging.getLogger("dhan.api")


async def funds_handler(_r: web.Request) -> web.Response:
    from apps.api import _dhan_ro
    try:
        data = await _dhan_ro.call("get_funds")
        return web.json_response({"ok": True, "data": data})
    except Exception:
        logger.exception("funds_handler failed")
        return web.json_response({"ok": False, "error": "internal error"}, status=503)


async def positions_handler(_r: web.Request) -> web.Response:
    from apps.api import _dhan_ro
    try:
        data = await _dhan_ro.call("get_positions")
        return web.json_response({"ok": True, "data": data})
    except Exception:
        logger.exception("positions_handler failed")
        return web.json_response({"ok": False, "error": "internal error"}, status=503)


async def instrument_price_handler(request: web.Request) -> web.Response:
    from apps.api import _dhan_ro
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
    except Exception:
        logger.exception("instrument_price_handler failed")
        return web.json_response({"ok": False, "error": "internal error"}, status=503)


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
    import apps.api as _api
    if (denial := _api._check_auth(request)) is not None:
        return denial
    wl = request.app.get("watchlist")
    if not wl:
        return web.json_response({"ok": False, "error": "Watchlist not initialised"}, status=503)
    try:
        await wl.refresh()
        return web.json_response({"ok": True, "count": len(wl.get()),
                                  "stocks": [s.symbol for s in wl.get()]})
    except Exception:
        logger.exception("watchlist_refresh_handler failed")
        return web.json_response({"ok": False, "error": "internal error"}, status=500)
