"""
Backtest CLI — replay ORB (optionally Kronos-gated) over real bars.

Run on the agent or DB-adjacent host (reads the bars hypertable):

    # ORB standalone, point-in-time screener universe, 3 months
    python -m research.backtest --from 2026-03-01 --to 2026-06-01

    # Fixed securities, skip the screener
    python -m research.backtest --from 2026-01-01 --to 2026-06-01 --ids 2885,1333

    # Run 2 of the three-way comparison: ORB + Kronos zero-shot
    python -m research.backtest --from 2026-03-01 --to 2026-06-01 --gate kronos

    # JSON output for diffing runs
    python -m research.backtest ... --json out.json
"""
import argparse
import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("dhan.backtest")


async def run(args) -> int:
    from config import get_config
    from db import init_db
    from research.backtest.engine import trading_days
    from research.backtest.portfolio_engine import PortfolioParams, replay_portfolio
    from research.backtest.report import Report, m3_panel
    from research.backtest.provenance import provenance
    from research.backtest.universe import point_in_time_universe
    from strategies.orb import ORBParams

    cfg = get_config()
    # M3 runs on the cleaned replica (dhan_clean), not raw dhan_trading — every
    # bars/universe query in the backtester goes through this engine.
    init_db(cfg.backtest_db_url)
    logger.info("Backtest DB = %s (clean replica)", cfg.backtest_db_name)

    # Portfolio-level params — finite capital + concurrent cap + daily kill-switch
    # mirror the live config (apps/trader.py shares one RiskEngine across runners).
    pparams = PortfolioParams(
        equity=args.equity,
        risk_per_trade=cfg.risk_per_trade,
        max_notional_pct=cfg.max_notional_per_trade_pct,
        slippage_bps=args.slippage_bps,
        max_open_positions=cfg.max_open_positions,
        max_orders_per_session=cfg.max_orders_per_session,
        max_daily_loss_pct=cfg.max_daily_loss_pct,
        min_stop_distance_pct=cfg.min_stop_distance_pct,
        adv_participation_pct=cfg.adv_participation_pct,
        orb=ORBParams(orb_minutes=cfg.orb_range_minutes),
    )

    gate_fn = None
    gate = None
    if args.gate == "kronos":
        from research.backtest.kronos_gate import KronosBacktestGate
        gate = KronosBacktestGate(min_confidence=cfg.kronos_min_confidence,
                                  seed=args.kronos_seed)
        gate_fn = gate

    days = trading_days(args.from_date, args.to_date)
    if not days:
        logger.error("No bars in range %s → %s — is the backfill far enough?",
                     args.from_date, args.to_date)
        return 1
    logger.info("Backtest %s → %s: %d sessions, gate=%s, split=%s",
                args.from_date, args.to_date, len(days), args.gate, args.split_date)

    # Build the per-day universe (point-in-time, no look-ahead) once.
    fixed_ids = [s.strip() for s in args.ids.split(",")] if args.ids else None
    universe_by_day: dict = {}
    for i, day in enumerate(days):
        if fixed_ids:
            universe_by_day[day] = fixed_ids
        else:
            try:
                universe_by_day[day] = [
                    u["security_id"]
                    for u in point_in_time_universe(day, n=args.n,
                                                    min_avg_volume=args.min_volume)]
            except Exception as exc:
                logger.warning("%s: universe query failed (%s) — skipping day", day, exc)
                universe_by_day[day] = []
        if (i + 1) % 50 == 0 or i == len(days) - 1:
            logger.info("  universe built %d/%d days", i + 1, len(days))

    trades, daily = await replay_portfolio(universe_by_day, pparams, gate_fn)

    panel = m3_panel(trades, args.equity, split_date=args.split_date)
    label = f"ORB{' + Kronos zero-shot' if args.gate == 'kronos' else ' standalone'}  " \
            f"{args.from_date} → {args.to_date}  (portfolio)"
    Report(trades, starting_equity=args.equity).print_report(label)
    ks_days = sum(1 for d in daily if d["kill_switch"])
    if args.split_date:
        logger.info("IS Sharpe=%s  OOS Sharpe=%s  OOS÷IS=%s",
                    panel["is"]["sharpe_daily_ann"], panel["oos"]["sharpe_daily_ann"],
                    panel["oos_is_sharpe_ratio"])
    logger.info("⚠️ SURVIVORSHIP CEILING: universe = current scrip master (delisted "
                "names absent) → results are an OPTIMISTIC UPPER BOUND. Kill-switch days: %d",
                ks_days)

    if args.json:
        payload = {
            "provenance": provenance({
                "cli": {
                    "from": str(args.from_date), "to": str(args.to_date),
                    "split_date": str(args.split_date) if args.split_date else None,
                    "equity": args.equity, "slippage_bps": args.slippage_bps,
                    "gate": args.gate, "n": args.n, "min_volume": args.min_volume,
                    "ids": fixed_ids,
                },
                # Full PortfolioParams (sizing, caps, tick, partial-fill, ORB) so
                # the result is fully reproducible from the JSON alone.
                "portfolio_params": asdict(pparams),
                "kronos_min_confidence": (cfg.kronos_min_confidence
                                          if args.gate == "kronos" else None),
                "kronos_seed": gate.seed if gate else None,
                "db": cfg.backtest_db_name,
            }),
            "label": label,
            "survivorship": "CEILING — universe from current scrip master; results are an upper bound",
            "panel": panel,
            "per_security": Report(trades, args.equity).per_security(),
            "daily": daily,
            "gate_decisions": gate.decisions if gate else None,
            "trades": [{**asdict(t), "day": str(t.day),
                        "entry_ts": t.entry_ts.isoformat(),
                        "exit_ts": t.exit_ts.isoformat()} for t in trades],
        }
        with open(args.json, "w") as f:
            json.dump(payload, f, indent=2)
        logger.info("Wrote %s", args.json)
    return 0


def main():
    p = argparse.ArgumentParser(description="ORB backtest on real 1m bars")
    p.add_argument("--from", dest="from_date", required=True,
                   type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    p.add_argument("--to", dest="to_date", required=True,
                   type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    p.add_argument("--ids", help="Comma-separated security IDs (skips the screener)")
    p.add_argument("--n", type=int, default=5, help="Universe size per day (default 5)")
    p.add_argument("--gate", choices=["none", "kronos"], default="none")
    p.add_argument("--equity", type=float, default=500_000.0)
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.add_argument("--split-date", dest="split_date", default=None,
                   type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                   help="First OOS day (date-split): trades before = IS, on/after = OOS")
    p.add_argument("--min-volume", dest="min_volume", type=int, default=50_000,
                   help="Universe avg-volume floor (default 50k = live screener)")
    p.add_argument("--kronos-seed", dest="kronos_seed", type=int, default=0,
                   help="Seed for Kronos sampling (reproducible gate runs; default 0)")
    p.add_argument("--json", help="Write full results to this JSON file")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
