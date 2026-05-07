"""
DhanHQ Algo Platform — Main Orchestrator
=========================================
Wires together:  DhanClient → RiskManager → Strategies → Web Dashboard

Run:
    python main.py

Set env vars (or .env file):
    DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, PAPER_TRADING, MAX_DAILY_LOSS
    STRATEGY=scalper|sma   (default: scalper)
"""

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from aiohttp import web
from aiohttp.web_middlewares import normalize_path_middleware
from dotenv import load_dotenv

load_dotenv()

from core.auth import DhanAuthManager
from core.client import DhanClient
from core.risk import RiskManager, RiskConfig
from core.backtest import Backtester
from strategies.strategy_base import (
    SMACrossoverStrategy, SMAConfig,
    StraddleSellerStrategy, StraddleSellerConfig,
)
from strategies.options_scalper import OptionsScalperStrategy, OptionsScalperConfig
from core.watchlist import WatchlistManager
from strategies.scanner import MultiStockScanner
from strategies.index_options import IndexOptionsScanner
from strategies.backtest_strategies import (
    RSIScalperStrategy, RSIConfig,
    MomentumBreakoutStrategy, MomentumConfig,
    MeanReversionStrategy, MeanReversionConfig,
    BollingerReversionStrategy, BollingerConfig,
    VWAPReversionStrategy, VWAPConfig,
    STRATEGY_REGISTRY,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dhan.main")

CLIENT_ID      = os.getenv("DHAN_CLIENT_ID",    "DEMO_CLIENT")
ACCESS_TOKEN   = os.getenv("DHAN_ACCESS_TOKEN", "DEMO_TOKEN")
DHAN_PIN       = os.getenv("DHAN_PIN",          "")
TOTP_SECRET    = os.getenv("DHAN_TOTP_SECRET",  "")
PAPER_TRADING  = os.getenv("PAPER_TRADING",     "true").lower() == "true"
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "5000"))
WEBHOOK_PORT   = int(os.getenv("WEBHOOK_PORT",  "8765"))
STRATEGY       = os.getenv("STRATEGY", "scalper").lower()
SCANNER_MODE   = os.getenv("SCANNER_MODE", "false").lower() == "true"
SEGMENTS       = os.getenv("SEGMENTS", "NSE_EQ").split(",")
PAPER_BALANCE  = float(os.getenv("PAPER_BALANCE", "500000"))  # ₹5L simulated capital for paper mode

# Auth manager available when PIN + TOTP are configured
_auth_manager: Optional[DhanAuthManager] = None

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
    return web.json_response({"status": "ok", "paper": PAPER_TRADING})


async def trading_mode_handler(request: web.Request) -> web.Response:
    """GET → current mode. POST {paper: true/false} → toggle all engines + child strategies."""
    fno = request.app.get("fno_scanner")
    eq  = request.app.get("equity_scanner")

    if request.method == "POST":
        body  = await request.json()
        paper = bool(body.get("paper", True))

        # ── F&O scanner ──────────────────────────────────────────────────────
        if fno:
            fno.paper_trading = paper
            # Safe mid-session switch: clear paper positions if switching to live
            if not paper:
                for state in fno._indices.values():
                    if state.in_position:
                        logger.warning(f"Mode→LIVE: clearing paper position {state.name}")
                        fno._go_flat(state)

        # ── Equity scanner + ALL child strategy instances ────────────────────
        if eq:
            eq.paper_trading = paper
            for strategy in eq._strategies.values():
                strategy.config.paper_trading = paper   # propagate to children
            if not paper:
                for key in list(eq._positions.keys()):
                    logger.warning(f"Mode→LIVE: clearing paper equity position {key}")
                eq._positions.clear()
                eq._current_prices.clear()

        # ── Primary strategy (app["strategy"]) ───────────────────────────────
        primary = request.app.get("strategy")
        if primary and hasattr(primary, "paper_trading"):
            primary.paper_trading = paper
        if primary and hasattr(primary, "config"):
            primary.config.paper_trading = paper

        # ── Update app-level mode flag (fixes status_handler) ────────────────
        request.app["paper_trading"] = paper

        mode = "PAPER" if paper else "LIVE"
        logger.warning(f"⚠️  Trading mode switched to {mode}")
        return web.json_response({"ok": True, "paper": paper, "mode": mode})

    paper = request.app.get("paper_trading", fno.paper_trading if fno else PAPER_TRADING)
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

    current_paper = request.app.get("paper_trading", PAPER_TRADING)
    payload = {
        "mode":             "PAPER" if current_paper else "LIVE",
        "client_id":        CLIENT_ID,
        "uptime_seconds":   uptime,
        "strategy_name":    strategy.config.name,
        "strategy_running": strategy._running,
        "orders_placed":    strategy.orders_placed,
        "position":         strategy.position,
        "entry_price":      strategy.entry_price,
    }

    # SMA warmup info
    if hasattr(strategy, '_fast_prices'):
        sc = strategy.sma_config
        payload["warmup"] = {
            "fast_current":  len(strategy._fast_prices),
            "fast_required": sc.fast_period,
            "slow_current":  len(strategy._slow_prices),
            "slow_required": sc.slow_period,
            "ready":         len(strategy._slow_prices) >= sc.slow_period,
        }
    else:
        payload["warmup"] = {"ready": True}

    return web.json_response(payload)


async def risk_handler(request: web.Request) -> web.Response:
    return web.json_response(request.app["risk"].get_summary())


async def signals_handler(request: web.Request) -> web.Response:
    """Aggregates signals from BOTH scanners, newest first."""
    fno = request.app.get("fno_scanner")
    eq  = request.app.get("equity_scanner")
    all_sigs = []
    for src, tag in [(fno, "F&O"), (eq, "EQ")]:
        if src:
            for s in getattr(src, "signals", []):
                all_sigs.append({
                    "action":    s.action,
                    "price":     s.price,
                    "reason":    s.reason,
                    "timestamp": s.timestamp.isoformat(),
                    "source":    tag,
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
    """Aggregates simulated paper positions — only when in paper mode."""
    current_paper = request.app.get("paper_trading", PAPER_TRADING)
    if not current_paper:
        return web.json_response({"ok": True, "count": 0, "data": [],
                                   "note": "Live mode — see /api/positions for real positions"})
    positions = []

    fno: "IndexOptionsScanner" = request.app.get("fno_scanner")
    if fno:
        for name, state in fno._indices.items():
            if state.in_position:
                positions.append({
                    "engine":        "F&O",
                    "symbol":        f"{name} {int(state.strike)} {state.option_type}",
                    "index":         name,
                    "option_type":   state.option_type,
                    "strike":        state.strike,
                    "entry_premium": state.entry_premium,
                    "lot_size":      state.lot_size,
                    "expiry":        state.active_expiry,
                    "bep":           state.breakeven,
                })

    eq = request.app.get("equity_scanner")
    if eq:
        wl = request.app.get("watchlist")
        stocks = {s.security_id: s for s in (wl.get() if wl else [])}
        for key, entry_price in eq._positions.items():
            seg, sid = key.split(":")
            sym = stocks.get(sid)
            cur_price = eq._current_prices.get(sid, 0.0)
            strat = eq._strategies.get(key)
            qty = strat.config.quantity if strat else 1
            upnl = round((cur_price - entry_price) * qty, 2) if cur_price else 0.0
            positions.append({
                "engine":        "EQ",
                "symbol":        sym.symbol if sym else sid,
                "name":          sym.name if sym else sid,
                "segment":       seg,
                "entry_price":   entry_price,
                "current_price": cur_price,
                "qty":           qty,
                "unrealized_pnl": upnl,
                "change_pct":    round((cur_price - entry_price) / entry_price * 100, 2) if entry_price else 0,
            })

    return web.json_response({
        "ok":    True,
        "count": len(positions),
        "data":  positions,
    })


async def scalper_handler(request: web.Request) -> web.Response:
    strategy = request.app["strategy"]
    if hasattr(strategy, "get_scalper_summary"):
        return web.json_response(strategy.get_scalper_summary())
    return web.json_response({"ok": False, "error": "Not a scalper strategy"}, status=404)


async def instruments_handler(request: web.Request) -> web.Response:
    strategy = request.app["strategy"]
    if hasattr(strategy, "get_expiries"):
        return web.json_response(strategy.get_expiries())
    return web.json_response({"ok": False, "error": "Not a scalper strategy"}, status=404)


async def auth_handler(request: web.Request) -> web.Response:
    mgr = request.app.get("auth_manager")
    if not mgr:
        return web.json_response({"mode": "manual", "note": "Set DHAN_PIN + DHAN_TOTP_SECRET to enable auto-refresh"})
    return web.json_response({"mode": "auto", **mgr.summary()})


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
    fno_open  = dtime(9, 15)
    fno_close = dtime(15, 30)
    pre_open  = dtime(9, 0)

    # MCX: Mon–Fri 09:00–23:30, Sat 09:00–14:00
    mcx_open      = dtime(9, 0)
    mcx_close_wkd = dtime(23, 30)
    mcx_close_sat = dtime(14, 0)
    is_saturday   = wd == 5

    nse_status  = "OPEN"  if is_weekday and nse_open  <= t <= nse_close else "CLOSED"
    fno_status  = "OPEN"  if is_weekday and fno_open  <= t <= fno_close else "CLOSED"
    pre_status  = "PRE"   if is_weekday and pre_open  <= t < nse_open   else None

    if is_weekday:
        mcx_status = "OPEN" if mcx_open <= t <= mcx_close_wkd else "CLOSED"
    elif is_saturday:
        mcx_status = "OPEN" if mcx_open <= t <= mcx_close_sat else "CLOSED"
    else:
        mcx_status = "CLOSED"

    return web.json_response({
        "nse_equity":  pre_status or nse_status,
        "nse_fno":     pre_status or fno_status,
        "mcx":         mcx_status,
        "ist_time":    now.strftime("%H:%M:%S"),
        "weekday":     now.strftime("%A"),
        "is_weekend":  not is_weekday and not is_saturday,
    })


async def switch_strategy_handler(request: web.Request) -> web.Response:
    body          = await request.json()
    strategy_name = body.get("strategy", "scalper")
    segment       = body.get("segment",   "NSE_FNO")
    security_id   = body.get("security_id", "13")
    quantity      = int(body.get("quantity",  75))
    num_lots      = int(body.get("num_lots",   1))

    dhan  = request.app["client"]
    risk  = request.app["risk"]
    # Always use the current live mode — not the startup global
    paper = request.app.get("paper_trading", PAPER_TRADING)

    # Stop old strategy
    old = request.app["strategy"]
    old.stop()
    old_task = request.app.get("strategy_task")
    if old_task and not old_task.done():
        old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)

    # Common equity config kwargs
    equity_kwargs = dict(
        security_id=security_id, exchange_segment=segment,
        product_type="INTRADAY", quantity=quantity, paper_trading=paper,
    )

    if strategy_name == "scalper":
        cfg = OptionsScalperConfig(
            security_id=security_id, exchange_segment="IDX_I",
            product_type="MARGIN", quantity=quantity, num_lots=num_lots,
            poll_interval=10.0, paper_trading=paper,
        )
        new_strategy = OptionsScalperStrategy(dhan, risk, cfg)

    elif strategy_name == "sma_crossover":
        cfg = SMAConfig(name=f"SMA_9_21_{security_id}", **equity_kwargs)
        new_strategy = SMACrossoverStrategy(dhan, risk, cfg)

    elif strategy_name == "rsi_scalper":
        cfg = RSIConfig(name=f"RSI_Scalper_{security_id}", **equity_kwargs)
        new_strategy = RSIScalperStrategy(dhan, risk, cfg)

    elif strategy_name == "momentum_breakout":
        cfg = MomentumConfig(name=f"Momentum_{security_id}", **equity_kwargs)
        new_strategy = MomentumBreakoutStrategy(dhan, risk, cfg)

    elif strategy_name == "mean_reversion":
        cfg = MeanReversionConfig(name=f"MeanRev_{security_id}", **equity_kwargs)
        new_strategy = MeanReversionStrategy(dhan, risk, cfg)

    elif strategy_name == "bollinger":
        cfg = BollingerConfig(name=f"Bollinger_{security_id}", **equity_kwargs)
        new_strategy = BollingerReversionStrategy(dhan, risk, cfg)

    elif strategy_name == "vwap_reversion":
        cfg = VWAPConfig(name=f"VWAP_{security_id}", **equity_kwargs)
        new_strategy = VWAPReversionStrategy(dhan, risk, cfg)

    elif strategy_name == "short_straddle":
        cfg = StraddleSellerConfig(
            name="Short_Straddle", security_id=security_id,
            exchange_segment="NSE_FNO", product_type="MARGIN",
            quantity=quantity, lot_size=quantity, paper_trading=paper,
        )
        new_strategy = StraddleSellerStrategy(dhan, risk, cfg)

    else:
        return web.json_response({"ok": False, "error": f"Unknown strategy: {strategy_name}"}, status=400)

    request.app["strategy"]      = new_strategy
    request.app["strategy_task"] = asyncio.create_task(new_strategy.run(), name="strategy")
    request.app["runtime_config"] = {
        "strategy": strategy_name, "segment": segment,
        "security_id": security_id, "quantity": quantity, "num_lots": num_lots,
    }
    logger.info(f"Strategy switched to {new_strategy.config.name}")
    return web.json_response({"ok": True, "strategy": strategy_name, "message": f"Switched to {new_strategy.config.name}"})


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
    risk     = request.app["risk"]
    strategy = request.app["strategy"]
    risk.activate_kill_switch()
    strategy.stop()
    task = request.app.get("strategy_task")
    if task and not task.done():
        task.cancel()

    oco_cancelled = 0
    if not PAPER_TRADING and hasattr(strategy, "_oco_order_id") and strategy._oco_order_id:
        try:
            await request.app["client"].cancel_forever_order(strategy._oco_order_id)
            oco_cancelled = 1
        except Exception as e:
            logger.warning(f"OCO cancel failed: {e}")

    logger.critical("⛔ KILL SWITCH ACTIVATED via dashboard")
    return web.json_response({"ok": True, "halted": True, "oco_cancelled": oco_cancelled, "message": "Kill switch activated"})


async def payoff_handler(request: web.Request) -> web.Response:
    strategy = request.app["strategy"]
    if not hasattr(strategy, "get_scalper_summary"):
        return web.json_response({"ok": False, "error": "Payoff only for scalper"}, status=404)

    sc  = strategy.get_scalper_summary()
    cfg = strategy.scalper_cfg

    if sc["state"] == "IN_POSITION":
        entry  = sc["entry_premium"]
        bep    = sc["breakeven_premium"]
        target = round(bep + cfg.target_buffer, 2)
        stop   = round(entry - cfg.stop_buffer, 2)
        qty    = cfg.quantity * cfg.num_lots
        mode   = "live"
    else:
        entry  = cfg.max_premium / 2
        bep    = round(entry + 2.0, 2)
        target = round(bep + cfg.target_buffer, 2)
        stop   = round(entry - cfg.stop_buffer, 2)
        qty    = cfg.quantity * cfg.num_lots
        mode   = "whatif"

    lo   = min(stop * 0.8, stop - 10)
    hi   = max(target * 1.2, target + 10)
    step = (hi - lo) / 19
    points = [{"premium": round(lo + i * step, 2), "pnl": round((lo + i * step - entry) * qty, 2)} for i in range(20)]

    return web.json_response({"ok": True, "mode": mode, "entry": entry, "breakeven": bep, "target": target, "stop": stop, "points": points})


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


async def scanner_handler(request: web.Request) -> web.Response:
    scanner = request.app.get("scanner")
    if not scanner:
        return web.json_response({"ok": False, "mode": "single_strategy"})
    if hasattr(scanner, "get_summary"):
        return web.json_response({"ok": True, **scanner.get_summary()})
    return web.json_response({"ok": True, **scanner.get_scan_summary()})


async def scanner_config_handler(request: web.Request) -> web.Response:
    """Update scanner config (strategy, segments, capital_pct) without full restart."""
    body         = await request.json()
    scanner      = request.app.get("scanner")
    if not scanner:
        return web.json_response({"ok": False, "error": "Scanner not running"}, status=404)

    if "strategy_key" in body:
        new_key = body["strategy_key"]
        if new_key in STRATEGY_MAP:
            scanner.strategy_key = new_key
            scanner._cfg_cls, scanner._cfg_type = STRATEGY_MAP[new_key]
            scanner._strategies.clear()   # reset per-stock instances
    if "segments" in body:
        segs = body["segments"]
        if hasattr(scanner, "active_segments"):
            # IndexOptionsScanner: map UI segment names to option segments
            active = []
            if "NSE_FNO" in segs: active.append("NSE_FNO")
            if "BSE_FNO" in segs: active.append("BSE_FNO")
            scanner.active_segments = active or ["NSE_FNO"]
            active_indices = [n for n, s in scanner._indices.items() if s.option_segment in scanner.active_segments]
            logger.info(f"Scanner segments updated: {scanner.active_segments} → indices: {active_indices}")
        elif hasattr(scanner, "segments"):
            scanner.segments = segs
    if "capital_pct" in body:
        scanner.capital_pct = float(body["capital_pct"])
    if "max_positions" in body:
        scanner.max_positions = int(body["max_positions"])
    if "hedge_fno" in body:
        scanner.hedge_fno = bool(body["hedge_fno"])

    resp: dict = {"ok": True}
    if hasattr(scanner, "strategy_key"):
        resp["strategy_key"] = scanner.strategy_key
    if hasattr(scanner, "active_segments"):
        resp["segments"] = scanner.active_segments
    elif hasattr(scanner, "segments"):
        resp["segments"] = scanner.segments
    if hasattr(scanner, "capital_pct"):
        resp["capital_pct"] = scanner.capital_pct
    return web.json_response(resp)


async def backtest_run_handler(request: web.Request) -> web.Response:
    body        = await request.json()
    strategy_key = body.get("strategy", "sma_crossover")
    security_id  = body.get("security_id", "2885")
    segment      = body.get("segment", "NSE_EQ")
    from_date    = body.get("from_date", "2026-01-01")
    to_date      = body.get("to_date",   "2026-05-01")
    quantity     = int(body.get("quantity",    1))
    fast_period  = int(body.get("fast_period", 9))
    slow_period  = int(body.get("slow_period", 21))
    interval     = body.get("interval", "D")

    try:
        client = request.app["client"]

        # ── Fetch historical bars ─────────────────────────────────────────────
        instrument = "EQUITY" if segment == "NSE_EQ" else "INDEX"
        if interval == "D":
            raw = await client.get_daily_historical(
                security_id=security_id, exchange_segment=segment,
                instrument=instrument, from_date=from_date, to_date=to_date,
            )
        else:
            raw = await client.get_intraday_historical(
                security_id=security_id, exchange_segment=segment,
                instrument=instrument, interval=interval,
                from_date=from_date, to_date=to_date,
            )

        closes     = raw.get("close",     [])
        opens      = raw.get("open",      closes)
        highs      = raw.get("high",      closes)
        lows       = raw.get("low",       closes)
        volumes    = raw.get("volume",    [0] * len(closes))
        timestamps = raw.get("timestamp", raw.get("start_Time", list(range(len(closes)))))

        if not closes:
            return web.json_response({
                "ok": False,
                "error": "No historical data returned — check security_id, segment and date range."
            }, status=400)

        bars = [
            {
                "date":   str(timestamps[i])[:10] if i < len(timestamps) else str(i),
                "open":   opens[i],
                "high":   highs[i],
                "low":    lows[i],
                "close":  closes[i],
                "volume": volumes[i] if i < len(volumes) else 0,
            }
            for i in range(len(closes))
        ]

        # ── Build strategy + backtester ───────────────────────────────────────
        base_kwargs = dict(
            security_id=security_id, exchange_segment=segment,
            product_type="INTRADAY", quantity=quantity, paper_trading=True,
        )

        if strategy_key == "sma_crossover":
            cfg = SMAConfig(name=f"SMA_{fast_period}_{slow_period}",
                            fast_period=fast_period, slow_period=slow_period,
                            **base_kwargs)
            bt = Backtester(SMACrossoverStrategy, cfg)

        elif strategy_key == "rsi_scalper":
            cfg = RSIConfig(name="RSI_Scalper", **base_kwargs)
            bt  = Backtester(RSIScalperStrategy, cfg)

        elif strategy_key == "momentum_breakout":
            cfg = MomentumConfig(name="Momentum_Breakout", **base_kwargs)
            bt  = Backtester(MomentumBreakoutStrategy, cfg)

        elif strategy_key == "mean_reversion":
            cfg = MeanReversionConfig(name="Mean_Reversion", **base_kwargs)
            bt  = Backtester(MeanReversionStrategy, cfg)

        elif strategy_key == "bollinger":
            cfg = BollingerConfig(name="Bollinger_Reversion", **base_kwargs)
            bt  = Backtester(BollingerReversionStrategy, cfg)

        elif strategy_key == "vwap_reversion":
            cfg = VWAPConfig(name="VWAP_Reversion", **base_kwargs)
            bt  = Backtester(VWAPReversionStrategy, cfg)

        else:
            return web.json_response({"ok": False, "error": f"Unknown strategy: {strategy_key}"}, status=400)

        result = await bt.run(bars)
        return web.json_response({
            "ok":           True,
            "bars":         len(bars),
            "strategy":     strategy_key,
            "symbol":       body.get("symbol", security_id),
            "summary":      result.summary(),
            "equity_curve": result.equity_curve,
            "trades":       result.trades,
        })

    except Exception as e:
        logger.error(f"Backtest error: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


# ── Server startup ────────────────────────────────────────────────────────────
async def start_server(app: web.Application):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    logger.info(f"🌐 Dashboard: http://localhost:{WEBHOOK_PORT}")
    return runner


# ── Bootstrap ─────────────────────────────────────────────────────────────────
async def main():
    logger.info("=" * 60)
    logger.info("  DhanHQ Algo Trading Platform  v1.0")
    logger.info(f"  Mode:     {'📝 PAPER TRADING' if PAPER_TRADING else '🔴 LIVE TRADING'}")
    logger.info(f"  Strategy: {STRATEGY.upper()}")
    logger.info(f"  Client:   {CLIENT_ID}")
    logger.info("=" * 60)

    if not PAPER_TRADING:
        logger.warning("⚠️  LIVE TRADING MODE — real money at risk!")

    # ── Auth Manager (auto token refresh) ─────────────────────────────────────
    global _auth_manager
    access_token = ACCESS_TOKEN

    if DHAN_PIN and TOTP_SECRET:
        _auth_manager = DhanAuthManager(
            client_id=CLIENT_ID,
            pin=DHAN_PIN,
            totp_secret=TOTP_SECRET,
        )
        access_token = await _auth_manager.load_or_generate()
        logger.info("🔑 Auto token management enabled (PIN + TOTP)")
    else:
        logger.info("🔑 Manual token mode — set DHAN_PIN + DHAN_TOTP_SECRET for auto-refresh")

    async with DhanClient(
        client_id=CLIENT_ID,
        access_token=access_token,
        sandbox=PAPER_TRADING,
        auth_manager=_auth_manager,
    ) as dhan:

        risk_cfg = RiskConfig(
            max_daily_loss=MAX_DAILY_LOSS,
            max_open_positions=5,
            max_loss_per_trade=15_000,   # 1 NIFTY lot × max premium ₹200
            check_interval_seconds=30,
        )
        risk = RiskManager(dhan, risk_cfg)

        @risk.on_halt
        async def on_risk_halt(reason: str):
            logger.critical(f"⛔ HALT: {reason}")

        # ── Watchlist (top movers from NSE) ──────────────────────────────────
        watchlist = await WatchlistManager.build()

        # ── Strategy selection ────────────────────────────────────────────────
        if STRATEGY == "scalper":
            cfg = OptionsScalperConfig(
                security_id="13",
                exchange_segment="IDX_I",
                product_type="MARGIN",
                quantity=75,
                expiry_date=os.getenv("EXPIRY_DATE", ""),   # empty = auto nearest expiry
                num_lots=int(os.getenv("NUM_LOTS", "1")),
                poll_interval=10.0,
                paper_trading=PAPER_TRADING,
            )
            strategy = OptionsScalperStrategy(dhan, risk, cfg)
        else:
            cfg = SMAConfig(
                name="SMA_9_21_Reliance",
                security_id="2885",
                exchange_segment="NSE_EQ",
                product_type="INTRADAY",
                quantity=1,
                fast_period=9,
                slow_period=21,
                poll_interval=10.0,
                paper_trading=PAPER_TRADING,
                max_orders=10,
            )
            strategy = SMACrossoverStrategy(dhan, risk, cfg)

        # ── Both scanners run in parallel ────────────────────────────────────
        # 1. Index Options Scanner (NSE_FNO + BSE_FNO) — always runs
        fno_scanner = IndexOptionsScanner(
            client        = dhan,
            risk_manager  = risk,
            paper_trading = PAPER_TRADING,
            capital_pct   = 0.35,
            poll_interval = 10.0,
            paper_balance = PAPER_BALANCE,
        )
        logger.info("🔭 F&O Scanner: NIFTY · BANKNIFTY · SENSEX · FINNIFTY · NIFTYNXT50 · MIDCPNIFTY")

        # 2. Equity Scanner (NSE_EQ top movers) — always runs
        equity_scanner = MultiStockScanner(
            client        = dhan,
            risk_manager  = risk,
            watchlist     = watchlist,
            strategy_key  = STRATEGY if STRATEGY not in ("scalper","index_options") else "sma_crossover",
            segments      = ["NSE_EQ"],
            paper_trading = PAPER_TRADING,
            capital_pct   = 0.35,
            hedge_fno     = False,
            max_positions = 999,
            poll_interval = 30.0,
            paper_balance = PAPER_BALANCE,
        )
        logger.info("📊 Equity Scanner: top 15 NSE movers · SMA crossover")

        # ── Web app ───────────────────────────────────────────────────────────
        app = web.Application(middlewares=[cors_middleware])
        app["risk"]           = risk
        app["strategy"]       = fno_scanner          # primary for /api/status duck-typing
        app["strategy_task"]  = asyncio.create_task(fno_scanner.run(), name="fno_scanner")
        app["equity_task"]    = asyncio.create_task(equity_scanner.run(), name="equity_scanner")
        app["client"]         = dhan
        app["auth_manager"]   = _auth_manager
        app["start_time"]     = time.time()
        app["paper_trading"]  = PAPER_TRADING   # mutable; updated by /api/mode
        app["watchlist"]      = watchlist
        app["fno_scanner"]    = fno_scanner
        app["equity_scanner"] = equity_scanner
        app["scanner"]        = fno_scanner          # kept for backwards compat
        app["runtime_config"] = {
            "strategy":    STRATEGY,
            "segment":     "NSE_FNO" if STRATEGY == "scalper" else "NSE_EQ",
            "security_id": cfg.security_id,
            "quantity":    cfg.quantity,
            "num_lots":    getattr(cfg, "num_lots", 1),
        }

        app.router.add_get("/",                       dashboard_handler)
        app.router.add_get("/health",                 health_handler)
        app.router.add_get("/api/mode",               trading_mode_handler)
        app.router.add_post("/api/mode",              trading_mode_handler)
        app.router.add_get("/api/status",             status_handler)
        app.router.add_get("/api/risk",               risk_handler)
        app.router.add_get("/api/signals",            signals_handler)
        app.router.add_get("/api/funds",              funds_handler)
        app.router.add_get("/api/positions",          positions_handler)
        app.router.add_get("/api/paper/positions",   paper_positions_handler)
        app.router.add_get("/api/scalper",            scalper_handler)
        app.router.add_get("/api/instruments",        instruments_handler)
        app.router.add_get("/api/auth",               auth_handler)
        app.router.add_get("/api/config",             config_handler)
        app.router.add_get("/api/payoff",             payoff_handler)
        app.router.add_get("/api/instruments/search", instrument_search_handler)
        app.router.add_get("/api/instruments/price",  instrument_price_handler)
        app.router.add_post("/api/strategy/switch",   switch_strategy_handler)
        app.router.add_post("/api/killswitch",        killswitch_handler)
        app.router.add_post("/api/backtest/run",      backtest_run_handler)
        app.router.add_get("/api/market",             market_status_handler)
        app.router.add_get("/api/watchlist",          watchlist_handler)
        app.router.add_post("/api/watchlist/refresh", watchlist_refresh_handler)
        app.router.add_get("/api/scanner",            scanner_handler)
        app.router.add_get("/api/scanner/fno",        lambda r: web.json_response({"ok":True, **r.app["fno_scanner"].get_summary()}))
        app.router.add_get("/api/scanner/equity",     lambda r: web.json_response({"ok":True, **r.app["equity_scanner"].get_scan_summary()}))
        app.router.add_post("/api/scanner/config",    scanner_config_handler)
        app.router.add_post("/postback",              postback_handler)

        # Serve React build assets if available
        if (DIST_DIR / "assets").exists():
            app.router.add_static("/assets", DIST_DIR / "assets")

        server_runner = await start_server(app)

        # ── Graceful shutdown ─────────────────────────────────────────────────
        loop       = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        def _shutdown(sig, frame):
            logger.info(f"Signal {sig.name} received — shutting down…")
            strategy.stop()
            fno_scanner.stop()
            equity_scanner.stop()
            stop_event.set()

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, _shutdown, s, None)
            except NotImplementedError:
                pass

        logger.info("🚀 Launching tasks…")
        strategy_task  = app["strategy_task"]
        equity_task    = app["equity_task"]

        tasks = [
            asyncio.create_task(risk.run(),        name="risk_monitor"),
            asyncio.create_task(stop_event.wait(), name="shutdown_watcher"),
        ]
        if _auth_manager:
            tasks.append(asyncio.create_task(_auth_manager.run(), name="auth_manager"))

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # Cancel both scanner tasks
        for t in [strategy_task, equity_task]:
            if not t.done():
                t.cancel()
        for task in pending:
            task.cancel()
        await asyncio.gather(strategy_task, equity_task, *pending, return_exceptions=True)
        await server_runner.cleanup()
        logger.info("✅ Platform shut down cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
