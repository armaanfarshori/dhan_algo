"""
dhan-trader — the trading process. No web server, no analytics queries.

Wires:  LiveFeed ─→ BarBuilder ─→ bars table        (M2: live data lands in DB)
        LiveFeed ─→ StrategyRunner ─→ ORB (pure)
                        └─ KronosGate (shadow) ─→ signals table
                        └─ RiskEngine ─→ Paper/Live executor ─→ Portfolio (DB)

State the dashboard needs is exported via run/trader_heartbeat.json every
few seconds; the dhan-api process serves it. The two processes share nothing
but the DB and that file — an analytics query can no longer delay an order.

Run: systemd unit dhan-trader (apps/api.py is the dashboard process).
"""
import asyncio
import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    # Full ISO + offset (box runs UTC) — the api tails this file for the
    # dashboard, and the browser needs a parseable timestamp
    datefmt="%Y-%m-%dT%H:%M:%S+00:00",
)
logger = logging.getLogger("dhan.trader")

from config import get_config

cfg = get_config()

RUN_DIR = Path(__file__).parent.parent / "run"
HEARTBEAT_FILE = RUN_DIR / "trader_heartbeat.json"
KILLSWITCH_FILE = RUN_DIR / "killswitch"
HEARTBEAT_INTERVAL = 5.0


async def seed_opening_ranges(runners, dhan, segment: str, orb_minutes: int):
    """
    Mid-session restart: the ORB instances never saw 9:15–9:30, so without
    this they can neither manage reconciled positions (no stop/target) nor
    trade today. Rebuild each security's true OR from REST intraday bars;
    breakouts that already happened while we were down are marked tried so
    they aren't taken hours late. Failures are non-fatal — the EOD
    square-off no longer depends on a locked OR.
    """
    from engine.runner import IST
    from strategies.orb import MARKET_OPEN
    now = datetime.now(IST)
    or_end = (datetime.combine(now.date(), MARKET_OPEN, tzinfo=IST)
              + timedelta(minutes=orb_minutes))
    if now.weekday() >= 5 or now <= or_end:
        return
    day = now.strftime("%Y-%m-%d")
    seeded = 0
    for r in runners:
        orb = r.strategy
        if orb.or_locked:
            continue
        try:
            data = None
            for attempt in range(3):
                # charts/intraday tolerates ~1 req/s — back-to-back calls
                # get DH-904'd, so space them out and retry
                await asyncio.sleep(1.2 if attempt == 0 else 3.0)
                try:
                    data = await dhan.get_intraday_historical(
                        security_id=orb.security_id, exchange_segment=segment,
                        instrument="EQUITY", interval="1",
                        from_date=day, to_date=day)
                    break
                except Exception:
                    if attempt == 2:
                        raise
            or_h, or_l = 0.0, float("inf")
            post_h, post_l = 0.0, float("inf")
            for ts, h, l in zip(data.get("timestamp") or [],
                                data.get("high") or [],
                                data.get("low") or []):
                bar = datetime.fromtimestamp(ts, IST)
                if bar.date() != now.date():
                    continue
                if MARKET_OPEN <= bar.time() and bar < or_end:
                    or_h, or_l = max(or_h, h), min(or_l, l)
                elif bar >= or_end:
                    post_h, post_l = max(post_h, h), min(post_l, l)
            orb.seed_opening_range(now.date(), or_h, or_l, post_h, post_l)
            seeded += orb.or_locked
        except Exception as exc:
            logger.warning("OR seed failed for %s (%s) — position still "
                           "guarded by EOD square-off", orb.security_id, exc)
    if seeded:
        logger.info("Opening ranges seeded for %d/%d securities (mid-session boot)",
                    seeded, len(runners))


async def write_heartbeat(*, runners, portfolio, risk, feed, bar_builder,
                          kronos_scanner, start_time, names=None):
    """Atomically export trader state for the api process."""
    names = names or {}
    while True:
        try:
            pf = portfolio.summary(feed.get_ltp)
            for pos in pf.get("open_positions", []):
                pos["ticker"] = names.get(pos["security_id"], pos["security_id"])
            payload = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "pid": __import__("os").getpid(),
                "mode": portfolio.mode,
                "uptime_seconds": int(time.time() - start_time),
                "kronos_gate": "shadow" if cfg.kronos_shadow_mode else "enforcing",
                "watchlist": [r.sid for r in runners],
                "names": names,
                "strategies": [{**r.status(),
                                "ticker": names.get(r.sid, r.sid)} for r in runners],
                "portfolio": pf,
                "risk": risk.get_summary(),
                "feed": {
                    "connected": feed.is_connected(),
                    "subscribed": len(feed.all_subscribed_sids()),
                },
                "bars": bar_builder.status(),
                "kronos_scanner": kronos_scanner.get_state() if kronos_scanner else None,
            }
            tmp = HEARTBEAT_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(HEARTBEAT_FILE)
        except Exception as exc:
            logger.warning("Heartbeat write failed: %s", exc)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def main():
    logger.info("=" * 60)
    logger.info("  dhan-trader  —  ORB + Kronos engine")
    logger.info(f"  Mode:  {'📝 PAPER' if cfg.paper_trading else '🔴 LIVE'}"
                f"   Gate: {'SHADOW' if cfg.kronos_shadow_mode else 'ENFORCING'}")
    logger.info("=" * 60)
    if not cfg.paper_trading:
        logger.warning("⚠️  LIVE TRADING MODE — real money at risk!")
        # SEC-12: refuse to start in LIVE mode unless the operator has
        # explicitly set ALLOW_LIVE_TOGGLE=true.  This prevents an accidental
        # PAPER_TRADING=false flip from silently opening live positions.
        if not cfg.allow_live_toggle:
            logger.critical(
                "LIVE mode requested but ALLOW_LIVE_TOGGLE is not set. "
                "Set ALLOW_LIVE_TOGGLE=true in .env to confirm live intent, "
                "then restart.  Refusing to start."
            )
            sys.exit(1)

    RUN_DIR.mkdir(exist_ok=True)
    start_time = time.time()

    from db import init_db
    init_db(cfg.db_url)

    # ── Token (single owner) + client ─────────────────────────────────────────
    from core.token_manager import MasterTokenManager
    from core.client import DhanClient
    master_tm = MasterTokenManager()
    access_token = await master_tm.load_or_generate()

    async with DhanClient(
        client_id=cfg.dhan_client_id,
        access_token=access_token,
        auth_manager=master_tm,
    ) as dhan:

        # ── Journal + run record ───────────────────────────────────────────────
        from core.journal import get_db_backend
        db = get_db_backend()
        await db.connect()
        run_id = await db.log_run_start(
            mode="PAPER" if cfg.paper_trading else "LIVE", strategy="orb")

        # ── Portfolio (DB-persisted) — reconcile before anything trades ───────
        from engine.portfolio import Portfolio
        portfolio = Portfolio(mode="PAPER" if cfg.paper_trading else "LIVE", db_backend=db)
        await portfolio.reconcile_on_boot()
        # LIVE: cross-check against the broker — it is the source of truth.
        # No-op in paper mode.
        await portfolio.reconcile_with_broker(dhan)

        # ── Market data: WebSocket feed → BarBuilder → bars table ─────────────
        from core.live_feed import LiveFeed
        from engine.bar_builder import BarBuilder
        bar_builder = BarBuilder(exchange_segment=cfg.watchlist_exchange_segment)
        feed = LiveFeed(cfg.dhan_client_id, access_token, on_tick=bar_builder.on_tick)

        # ── Watchlist from screener (cached watchlist as fallback) ─────────────
        from core.nse_screener import get_top_volatile
        from core.watchlist import WatchlistManager
        watchlist = await WatchlistManager.build()
        screener_results = await asyncio.get_running_loop().run_in_executor(
            None, lambda: get_top_volatile(
                n=cfg.watchlist_n,
                min_avg_volume=cfg.screener_min_avg_volume,
                min_price=cfg.screener_min_price))
        watchlist_ids = [r["security_id"] for r in screener_results]
        if not watchlist_ids:
            watchlist_ids = [s.security_id for s in watchlist.get()[:cfg.watchlist_n]]
            logger.warning("Screener empty — cached watchlist fallback: %s", watchlist_ids)

        # Validate the candidate list against the scrip master — a cached
        # watchlist once smuggled in a non-tradeable INDEX (sid 40, NIFTY
        # Consumption) that cost ₹6.5K of paper P&L on day 1. Only tradeable
        # equities in our segment pass; open positions are exempt below
        # (whatever we hold must keep being managed so it can exit).
        def _validate_watchlist(ids):
            from sqlalchemy import text as _text
            from db import get_session
            with get_session() as s:
                rows = s.execute(_text(
                    "SELECT security_id FROM instruments "
                    "WHERE security_id = ANY(:ids) "
                    "AND exchange_segment = :seg AND instrument_type = 'EQUITY'"),
                    {"ids": ids, "seg": cfg.watchlist_exchange_segment}).fetchall()
            return {r[0] for r in rows}

        valid = await asyncio.get_running_loop().run_in_executor(
            None, _validate_watchlist, list(watchlist_ids))
        for sid in [s for s in watchlist_ids if s not in valid]:
            watchlist_ids.remove(sid)
            logger.warning("Watchlist -= %s — not a tradeable %s equity, dropped",
                           sid, cfg.watchlist_exchange_segment)

        # Reconciled positions must keep streaming + trading even if today's
        # screener dropped them (orphan protection).
        for p in portfolio.open_positions():
            if p.security_id not in watchlist_ids:
                watchlist_ids.append(p.security_id)
                logger.warning("Watchlist += %s (open position, not in screener)", p.security_id)
        logger.info("Watchlist (%d): %s", len(watchlist_ids), watchlist_ids)

        eq_sids = [int(s) for s in watchlist_ids if s.isdigit()]
        if eq_sids:
            feed.subscribe({cfg.watchlist_exchange_segment: eq_sids})

        # Resolve display names once — the heartbeat carries them so every
        # dashboard panel shows tickers instead of raw security ids.
        def _resolve_names():
            from sqlalchemy import text as _text
            from db import get_session
            with get_session() as s:
                rows = s.execute(_text(
                    "SELECT security_id, COALESCE(NULLIF(ticker, ''), security_id) "
                    "FROM instruments WHERE security_id = ANY(:ids)"
                ), {"ids": watchlist_ids}).fetchall()
            return {r[0]: r[1] for r in rows}
        try:
            names = await asyncio.get_running_loop().run_in_executor(None, _resolve_names)
        except Exception as exc:
            logger.warning("Name resolution failed (%s) — showing ids", exc)
            names = {}

        # ── Kronos gate (shadow until calibrated) ──────────────────────────────
        from core.kronos_signal import get_kronos_engine
        from ml.kronos_gate import KronosGate
        kronos = get_kronos_engine()
        gate = KronosGate(kronos, db_backend=db,
                          min_confidence=cfg.kronos_min_confidence,
                          shadow=cfg.kronos_shadow_mode)

        # ── Execution + risk ───────────────────────────────────────────────────
        from engine.execution import PaperExecutor, LiveExecutor
        from engine.risk import RiskEngine, RiskParams
        if cfg.paper_trading:
            executor = PaperExecutor(db_backend=db, run_id=run_id,
                                     slippage_bps=cfg.paper_slippage_bps)
        else:
            executor = LiveExecutor(dhan, db_backend=db, run_id=run_id)

        runners: list = []

        def ltp_lookup(sid: str) -> float:
            ltp = feed.get_ltp(sid)
            if ltp > 0:
                return ltp
            for r in runners:
                if r.sid == sid:
                    return r.last_price
            return 0.0

        # Live mode runs the same fractional geometry at reduced scale
        # (training wheels for M8) — paper validates exactly what live does.
        scale = 1.0 if cfg.paper_trading else cfg.live_risk_scale
        risk = RiskEngine(
            RiskParams(
                equity_base=cfg.paper_balance if cfg.paper_trading else cfg.capital,
                risk_per_trade=cfg.risk_per_trade * scale,
                max_daily_loss_pct=cfg.max_daily_loss_pct * scale,
                weekly_loss_pct=cfg.weekly_loss_pct * scale,
                max_notional_pct=cfg.max_notional_per_trade_pct * scale,
                max_gross_exposure_pct=cfg.max_gross_exposure_pct * scale,
                adv_participation_pct=cfg.adv_participation_pct,
                min_stop_distance_pct=cfg.min_stop_distance_pct,
                max_open_positions=cfg.max_open_positions,
                killswitch_file=KILLSWITCH_FILE,
                halt_file=RUN_DIR / "halt_state.json",
            ),
            portfolio, ltp_lookup, db_backend=db)
        # Restart-proofing: restore an in-scope loss halt, seed the DB-backed
        # P&L cache, and conservatively book one full risk budget against
        # each reconciled position (their stops are recomputed by ORB).
        risk.load_persisted_halt()
        await risk.refresh_pnl()
        for p in portfolio.open_positions():
            risk.register_risk(p.security_id, risk.risk_budget_per_trade)

        @risk.on_halt
        async def on_halt(reason: str):
            logger.critical("⛔ HALT: %s — flattening open positions", reason)
            from core.notify import send_async
            await send_async(f"⛔ TRADING HALTED ({portfolio.mode})\n{reason}\n"
                             f"Open positions are being flattened.")
            for r in runners:
                pos = portfolio.get(r.sid)
                if pos.qty != 0 and r.last_price > 0:
                    from engine.types import OrderIntent
                    side = "SELL" if pos.qty > 0 else "BUY"
                    fill = await executor.submit(OrderIntent(
                        security_id=r.sid, exchange_segment=cfg.watchlist_exchange_segment,
                        side=side, qty=abs(pos.qty), strategy="ORB",
                        reason=f"risk halt: {reason}"), ref_price=r.last_price)
                    if fill:
                        await portfolio.apply_fill(fill, strategy="ORB")
                        r.strategy.notify_flat()
                        risk.release_risk(r.sid)

        # ── Strategy runners ───────────────────────────────────────────────────
        from engine.runner import StrategyRunner
        from strategies.orb import ORB, ORBParams
        params = ORBParams(orb_minutes=cfg.orb_range_minutes)
        stagger = cfg.poll_interval / max(len(watchlist_ids), 1)
        for idx, sid in enumerate(watchlist_ids):
            orb = ORB(sid, params)
            pos = portfolio.get(sid)
            if pos.qty != 0:   # resync reconciled position into the strategy
                orb.position = pos.qty
                orb.entry_price = pos.avg_price
            runners.append(StrategyRunner(
                orb, client=dhan, feed=feed, gate=gate, risk=risk,
                executor=executor, portfolio=portfolio,
                exchange_segment=cfg.watchlist_exchange_segment,
                poll_interval=cfg.poll_interval, poll_offset=idx * stagger,
                max_entries_per_session=cfg.max_orders_per_session))
        logger.info("ORB engine on %d securities (gate: %s)",
                    len(runners), "shadow" if cfg.kronos_shadow_mode else "enforcing")

        # Mid-session restart? Rebuild today's opening ranges before any
        # runner polls — reconciled positions need stops/targets immediately.
        await seed_opening_ranges(runners, dhan,
                                  cfg.watchlist_exchange_segment,
                                  cfg.orb_range_minutes)

        # ── Optional Kronos live scanner ───────────────────────────────────────
        kronos_scanner = None
        if cfg.kronos_scanner_enabled:
            from core.kronos_scanner import KronosScanner
            kronos_scanner = KronosScanner(kronos, db_backend=db, n=cfg.watchlist_n)

        # ── Launch ────────────────────────────────────────────────────────────
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _shutdown(sig_, _frame=None):
            logger.info("Signal %s — shutting down…", getattr(sig_, "name", sig_))
            stop_event.set()

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, _shutdown, s)
            except NotImplementedError:
                pass

        tasks = [
            asyncio.create_task(feed.run(), name="feed"),
            asyncio.create_task(bar_builder.run(), name="bars"),
            asyncio.create_task(risk.run(), name="risk"),
            asyncio.create_task(master_tm.run(), name="token"),
            asyncio.create_task(write_heartbeat(
                runners=runners, portfolio=portfolio, risk=risk, feed=feed,
                bar_builder=bar_builder, kronos_scanner=kronos_scanner,
                start_time=start_time, names=names), name="heartbeat"),
            *[asyncio.create_task(r.run(), name=f"orb_{r.sid}") for r in runners],
        ]
        if kronos_scanner:
            tasks.append(asyncio.create_task(kronos_scanner.run(), name="kronos_scanner"))

        logger.info("🚀 dhan-trader running (%d tasks)", len(tasks))
        await stop_event.wait()

        # ── Graceful shutdown: flush bars, stop everything ─────────────────────
        for r in runners:
            r.stop()
        feed.stop()
        bar_builder.stop()
        await bar_builder.flush()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await db.log_run_stop(run_id, outcome="stopped")
        logger.info("✅ dhan-trader shut down cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
