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
    from research.backtest.engine import BacktestParams, replay_security_day, trading_days
    from research.backtest.report import Report
    from research.backtest.universe import point_in_time_universe
    from strategies.orb import ORBParams

    cfg = get_config()
    init_db(cfg.db_url)

    params = BacktestParams(
        equity=args.equity,
        risk_per_trade=cfg.risk_per_trade,
        max_notional_per_trade=cfg.max_notional_per_trade,
        slippage_bps=args.slippage_bps,
        orb=ORBParams(orb_minutes=cfg.orb_range_minutes),
    )

    gate_fn = None
    gate = None
    if args.gate == "kronos":
        from research.backtest.kronos_gate import KronosBacktestGate
        gate = KronosBacktestGate(min_confidence=cfg.kronos_min_confidence)
        gate_fn = gate

    days = trading_days(args.from_date, args.to_date)
    if not days:
        logger.error("No bars in range %s → %s — is the backfill far enough?",
                     args.from_date, args.to_date)
        return 1
    logger.info("Backtest %s → %s: %d sessions, gate=%s",
                args.from_date, args.to_date, len(days), args.gate)

    fixed_ids = [s.strip() for s in args.ids.split(",")] if args.ids else None
    all_trades = []
    for i, day in enumerate(days):
        if fixed_ids:
            universe = fixed_ids
        else:
            try:
                universe = [u["security_id"]
                            for u in point_in_time_universe(day, n=args.n)]
            except Exception as exc:
                logger.warning("%s: universe query failed (%s) — skipping day", day, exc)
                continue
        day_trades = []
        for sid in universe:
            day_trades.extend(await replay_security_day(sid, day, params, gate_fn))
        all_trades.extend(day_trades)
        if (i + 1) % 20 == 0 or i == len(days) - 1:
            logger.info("  [%d/%d] %s  universe=%d  trades so far=%d",
                        i + 1, len(days), day, len(universe), len(all_trades))

    report = Report(all_trades, starting_equity=args.equity)
    label = f"ORB{' + Kronos zero-shot' if args.gate == 'kronos' else ' standalone'}  " \
            f"{args.from_date} → {args.to_date}"
    report.print_report(label)

    if args.json:
        payload = {
            "generated_at": datetime.utcnow().isoformat(),
            "label": label,
            "params": {"equity": args.equity, "slippage_bps": args.slippage_bps,
                       "gate": args.gate, "n": args.n, "ids": fixed_ids},
            "summary": report.summary(),
            "per_security": report.per_security(),
            "daily_pnl": {str(k): v for k, v in report.daily_pnl.items()},
            "trades": [{**asdict(t), "day": str(t.day),
                        "entry_ts": t.entry_ts.isoformat(),
                        "exit_ts": t.exit_ts.isoformat()} for t in report.trades],
            "gate_decisions": gate.decisions if gate else None,
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
    p.add_argument("--json", help="Write full results to this JSON file")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
