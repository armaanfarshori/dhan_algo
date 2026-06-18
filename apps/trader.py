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
# T4: emit UTC timestamps regardless of host OS timezone, so the literal "+00:00"
# offset above is always honest (and the api log-tail date matching stays correct).
logging.Formatter.converter = time.gmtime
logger = logging.getLogger("dhan.trader")

from config import get_config

cfg = get_config()

RUN_DIR = Path(__file__).parent.parent / "run"
HEARTBEAT_FILE = RUN_DIR / "trader_heartbeat.json"
KILLSWITCH_FILE = RUN_DIR / "killswitch"
RESUME_FILE = RUN_DIR / "resume"
HEARTBEAT_INTERVAL = 5.0

# Holds the live ApiUsageFlusher created by write_heartbeat() so that the
# shutdown path can reuse it (with its accumulated _last state) instead of
# constructing a fresh instance that would re-flush the entire calls_today as
# a delta and double-count API calls in the DB.
_heartbeat_flusher = None


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


def reregister_position_risk(runners, portfolio, risk):
    """After seed_opening_ranges() rebuilds each ORB's OR, replace the flat
    placeholder risk (booked at boot before stops were known) with the TRUE
    stop-distance risk for every reconciled position. A wrong placeholder
    mis-sizes the daily budget; the real stop is the ORB's SL edge. If the OR
    never recovered (seed failed / too early) the placeholder is kept so the
    slot is never under-counted."""
    for r in runners:
        pos = portfolio.get(r.sid)
        if pos.qty == 0:
            continue
        orb = r.strategy
        if orb.or_locked:
            if pos.qty > 0:
                stop = orb.or_low * (1 - orb.p.sl_buffer_pct)
            else:
                stop = orb.or_high * (1 + orb.p.sl_buffer_pct)
            risk.register_risk(
                r.sid, risk.position_risk(pos.avg_price, stop, abs(pos.qty)))
            logger.info("Re-registered real risk for %s: stop=%.2f", r.sid, stop)
        else:
            logger.warning("Runner %s: OR not locked after seed — keeping "
                           "placeholder risk budget (real stop unknown)", r.sid)


async def flatten_all(runners, portfolio, executor, risk, reason: str, segment: str):
    """Flatten every open position on a halt, then UNCONDITIONALLY release each
    risk slot. A halt blocks new entries regardless, so the slot serves no
    purpose; a failed flatten must not leak committed risk forever. apply_fill +
    notify_flat only run when the executor actually returned a fill."""
    from engine.types import OrderIntent
    for r in runners:
        pos = portfolio.get(r.sid)
        if pos.qty != 0:
            # ref_price priority: live last_price → entry avg_price. A never-ticked
            # position has last_price==0; the executor rejects ref_price<=0, so
            # without the avg_price fallback such a position could never be closed
            # on a halt — left naked past the kill switch. Surface the fallback.
            ref_price = r.last_price if r.last_price > 0 else pos.avg_price
            if ref_price <= 0:
                logger.critical(
                    "flatten_all %s: cannot flatten qty=%+d — no last_price AND no "
                    "avg_price; position left OPEN (release risk slot anyway)",
                    r.sid, pos.qty)
            else:
                if r.last_price <= 0:
                    logger.critical(
                        "flatten_all %s: no last_price — flattening on avg_price ₹%.2f",
                        r.sid, pos.avg_price)
                side = "SELL" if pos.qty > 0 else "BUY"
                fill = await executor.submit(OrderIntent(
                    security_id=r.sid, exchange_segment=segment,
                    side=side, qty=abs(pos.qty), strategy="ORB",
                    reason=f"risk halt: {reason}"), ref_price=ref_price)
                if fill:
                    await portfolio.apply_fill(fill, strategy="ORB")
                    r.strategy.notify_flat()
            # Release UNCONDITIONALLY (even when the flatten produced no fill).
            risk.release_risk(r.sid)


def make_ltp_lookup(feed, runners, stale_s: float, warn_every_s: float = 60.0):
    """Build the risk loop's price source with a staleness guard (QA P1).

    The risk loop must not price the daily-loss halt / equity snapshot off a
    frozen feed. Resolution order:
      1. FRESH WS price — get_ltp > 0 AND tick age <= stale_s.
      2. The runner's last_price (kept refreshed by its REST poll loop).
      3. As a last resort, the (stale, non-zero) WS price over 0 — so the risk
         loop never silently treats a held position as worthless.
    When nothing fresh is available a WARNING is logged, rate-limited per sid so
    a frozen feed is visible without flooding the log every risk tick.
    """
    stale_warned_at: dict = {}

    def _runner_last_price(sid: str) -> float:
        for r in runners:
            if r.sid == sid:
                return r.last_price
        return 0.0

    def ltp_lookup(sid: str) -> float:
        sid = str(sid)
        age = feed.get_tick_age_s(sid) if hasattr(feed, "get_tick_age_s") else None
        ltp = feed.get_ltp(sid)
        fresh = ltp > 0 and (age is not None and age <= stale_s)
        if fresh:
            return ltp
        fallback = _runner_last_price(sid)
        if fallback > 0:
            return fallback
        now = time.monotonic()
        last = stale_warned_at.get(sid)
        # `last is None` → first stale event for this sid: ALWAYS warn. Don't use a
        # 0.0 sentinel — time.monotonic() can start near 0, which would suppress
        # the first warning for the first warn_every_s of process uptime.
        if last is None or now - last >= warn_every_s:
            stale_warned_at[sid] = now
            logger.warning(
                "ltp_lookup %s: no fresh price (feed age=%s, ws_ltp=%.2f, "
                "runner_last=%.2f) — risk loop pricing may be stale",
                sid, "None" if age is None else f"{age:.0f}s", ltp, fallback)
        return ltp if ltp > 0 else fallback

    return ltp_lookup


async def write_heartbeat(*, runners, portfolio, risk, feed, bar_builder,
                          kronos_scanner, start_time, client=None, names=None):
    """Atomically export trader state for the api process."""
    global _heartbeat_flusher
    from core.api_usage import ApiUsageFlusher
    names = names or {}
    flusher = ApiUsageFlusher(process="trader")
    _heartbeat_flusher = flusher  # expose to shutdown path so it reuses _last
    _flush_ticks = 0
    _FLUSH_EVERY = max(1, round(60.0 / HEARTBEAT_INTERVAL))  # ~every 60s

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
                "rate_limits": client.rate_limit_usage() if client else {},
            }
            tmp = HEARTBEAT_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(HEARTBEAT_FILE)
        except Exception as exc:
            logger.warning("Heartbeat write failed: %s", exc)

        # Flush API usage deltas to DB roughly every 60s (not every heartbeat)
        if client:
            _flush_ticks += 1
            if _flush_ticks >= _FLUSH_EVERY:
                _flush_ticks = 0
                await flusher.flush(client)

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

        # Audit log: persist today's screened set + which names made the final
        # tradeable watchlist. Best-effort — record_daily_screen never raises.
        from core.screen_log import record_daily_screen
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: record_daily_screen(
                screener_results, selected_ids=watchlist_ids))

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

        # Stale-LTP guard for the risk loop (QA P1) — see make_ltp_lookup. The
        # closure captures `runners` (a list mutated below), so the fallback sees
        # every runner once they are appended.
        ltp_lookup = make_ltp_lookup(feed, runners, cfg.risk_ltp_stale_s)

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
                resume_file=RESUME_FILE,
            ),
            portfolio, ltp_lookup, db_backend=db)
        # Restart-proofing: restore an in-scope loss halt, seed the DB-backed
        # P&L cache, and conservatively book one full risk budget against each
        # reconciled position as a PLACEHOLDER. The real stop-distance risk is
        # re-registered after seed_opening_ranges() rebuilds each ORB's stop.
        risk.load_persisted_halt()
        # Discard any resume flag left on disk from before this boot: it must NOT
        # auto-clear a halt that load_persisted_halt() just restored (a persisted
        # loss-halt requires a deliberate, post-boot resume). The operator can
        # re-issue resume from the dashboard if they still want it.
        if RESUME_FILE.exists():
            RESUME_FILE.unlink()
            logger.warning("Discarded a stale run/resume flag at boot")
        await risk.refresh_pnl()
        for p in portfolio.open_positions():
            risk.register_risk(p.security_id, risk.risk_budget_per_trade)

        @risk.on_halt
        async def on_halt(reason: str):
            logger.critical("⛔ HALT: %s — flattening open positions", reason)
            from core.notify import send_async
            await send_async(f"⛔ TRADING HALTED ({portfolio.mode})\n{reason}\n"
                             f"Open positions are being flattened.")
            await flatten_all(runners, portfolio, executor, risk, reason,
                              cfg.watchlist_exchange_segment)

        @risk.on_resume
        async def on_resume():
            logger.warning("▶ TRADING RESUMED (%s) — halt cleared via dashboard",
                           portfolio.mode)
            from core.notify import send_async
            await send_async(f"▶ TRADING RESUMED ({portfolio.mode})\n"
                             "Halt cleared from the dashboard. Risk loop re-armed; "
                             "new entries allowed if no loss budget is breached.")

        # ── Strategy runners ───────────────────────────────────────────────────
        from engine.runner import StrategyRunner
        from strategies.orb import ORB, ORBParams
        from core.batched_ohlc import BatchedOhlc
        params = ORBParams(orb_minutes=cfg.orb_range_minutes)
        stagger = cfg.poll_interval / max(len(watchlist_ids), 1)
        # ONE shared batched-OHLC fallback for every runner: concurrent stale
        # lookups collapse into a single get_ohlc({seg: all sids}) call (no 429
        # burst when the feed goes quiet). TTL-cached for ~5s, coalesced.
        batched_ohlc = BatchedOhlc(
            dhan, cfg.watchlist_exchange_segment, feed.all_subscribed_sids)
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
                max_entries_per_session=cfg.max_orders_per_session,
                batched_ohlc=batched_ohlc))
        logger.info("ORB engine on %d securities (gate: %s)",
                    len(runners), "shadow" if cfg.kronos_shadow_mode else "enforcing")

        # Mid-session restart? Rebuild today's opening ranges before any
        # runner polls — reconciled positions need stops/targets immediately.
        await seed_opening_ranges(runners, dhan,
                                  cfg.watchlist_exchange_segment,
                                  cfg.orb_range_minutes)

        # Now that each ORB has its real OR back, swap the flat placeholder risk
        # booked at boot for the true stop-distance risk per reconciled position.
        reregister_position_risk(runners, portfolio, risk)

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
                start_time=start_time, client=dhan, names=names), name="heartbeat"),
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
        # Final API usage flush so the last ~60s of calls are recorded.
        # Reuse the live flusher from write_heartbeat (which has _last up-to-date)
        # so we only flush the true remaining delta — not the full calls_today.
        # A fresh instance would have _last={} and re-send the entire count,
        # doubling the DB total (QR-C2).
        from core.api_usage import ApiUsageFlusher
        _shutdown_flusher = _heartbeat_flusher or ApiUsageFlusher(process="trader")
        await _shutdown_flusher.flush(dhan)
        await db.log_run_stop(run_id, outcome="stopped")
        logger.info("✅ dhan-trader shut down cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
