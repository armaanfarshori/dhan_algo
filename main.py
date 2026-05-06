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
from strategies.strategy_base import SMACrossoverStrategy, SMAConfig
from strategies.options_scalper import OptionsScalperStrategy, OptionsScalperConfig

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

    payload = {
        "mode":             "PAPER" if PAPER_TRADING else "LIVE",
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
    signals = request.app["strategy"].signals[-50:]
    return web.json_response([
        {
            "action":    s.action,
            "price":     s.price,
            "reason":    s.reason,
            "timestamp": s.timestamp.isoformat(),
        }
        for s in reversed(signals)
    ])


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


async def switch_strategy_handler(request: web.Request) -> web.Response:
    body          = await request.json()
    strategy_name = body.get("strategy", "scalper")
    segment       = body.get("segment",   "NSE_FNO")
    security_id   = body.get("security_id", "13")
    quantity      = int(body.get("quantity",  75))
    num_lots      = int(body.get("num_lots",   1))

    dhan = request.app["client"]
    risk = request.app["risk"]

    # Stop old strategy
    old = request.app["strategy"]
    old.stop()
    old_task = request.app.get("strategy_task")
    if old_task and not old_task.done():
        old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)

    # Build new strategy
    if strategy_name == "scalper":
        cfg = OptionsScalperConfig(
            security_id=security_id, exchange_segment="IDX_I",
            product_type="MARGIN", quantity=quantity, num_lots=num_lots,
            poll_interval=10.0, paper_trading=PAPER_TRADING,
        )
        new_strategy = OptionsScalperStrategy(dhan, risk, cfg)
    elif strategy_name == "sma_crossover":
        cfg = SMAConfig(
            name=f"SMA_9_21_{security_id}", security_id=security_id,
            exchange_segment=segment, product_type="INTRADAY",
            quantity=quantity, paper_trading=PAPER_TRADING,
        )
        new_strategy = SMACrossoverStrategy(dhan, risk, cfg)
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

        # ── Web app ───────────────────────────────────────────────────────────
        app = web.Application(middlewares=[cors_middleware])
        app["risk"]           = risk
        app["strategy"]       = strategy
        app["strategy_task"]  = asyncio.create_task(strategy.run(), name="strategy")
        app["client"]         = dhan
        app["auth_manager"]   = _auth_manager
        app["start_time"]     = time.time()
        app["runtime_config"] = {
            "strategy":    STRATEGY,
            "segment":     "NSE_FNO" if STRATEGY == "scalper" else "NSE_EQ",
            "security_id": cfg.security_id,
            "quantity":    cfg.quantity,
            "num_lots":    getattr(cfg, "num_lots", 1),
        }

        app.router.add_get("/",                       dashboard_handler)
        app.router.add_get("/health",                 health_handler)
        app.router.add_get("/api/status",             status_handler)
        app.router.add_get("/api/risk",               risk_handler)
        app.router.add_get("/api/signals",            signals_handler)
        app.router.add_get("/api/funds",              funds_handler)
        app.router.add_get("/api/positions",          positions_handler)
        app.router.add_get("/api/scalper",            scalper_handler)
        app.router.add_get("/api/instruments",        instruments_handler)
        app.router.add_get("/api/auth",               auth_handler)
        app.router.add_get("/api/config",             config_handler)
        app.router.add_get("/api/payoff",             payoff_handler)
        app.router.add_get("/api/instruments/search", instrument_search_handler)
        app.router.add_get("/api/instruments/price",  instrument_price_handler)
        app.router.add_post("/api/strategy/switch",   switch_strategy_handler)
        app.router.add_post("/api/killswitch",        killswitch_handler)
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
            stop_event.set()

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, _shutdown, s, None)
            except NotImplementedError:
                pass

        logger.info("🚀 Launching tasks…")
        strategy_task = app["strategy_task"]

        tasks = [
            asyncio.create_task(risk.run(),        name="risk_monitor"),
            asyncio.create_task(stop_event.wait(), name="shutdown_watcher"),
        ]
        if _auth_manager:
            tasks.append(asyncio.create_task(_auth_manager.run(), name="auth_manager"))

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # Cancel strategy task separately
        if not strategy_task.done():
            strategy_task.cancel()
        for task in pending:
            task.cancel()
        await asyncio.gather(strategy_task, *pending, return_exceptions=True)
        await server_runner.cleanup()
        logger.info("✅ Platform shut down cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
