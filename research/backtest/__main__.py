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
import inspect
import json
import logging
import os
import pickle
from dataclasses import asdict
from datetime import date, datetime

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("dhan.backtest")

# Run-3 OOS contamination guard: fine-tuned Kronos was trained/selected on
# pre-2026 data, so any split_date before this cutoff would produce a
# contaminated OOS window.
FINETUNE_OOS_CUTOFF = date(2026, 1, 1)


def _universe_lookback_default() -> int:
    """Read the actual default for ``lookback_days`` from point_in_time_universe's signature."""
    from research.backtest.universe import point_in_time_universe

    sig = inspect.signature(point_in_time_universe)
    param = sig.parameters.get("lookback_days")
    if param is not None and param.default is not inspect.Parameter.empty:
        return int(param.default)
    return 30  # fallback if signature ever changes


async def run(args) -> int:
    from config import get_config
    from db import init_db
    from research.backtest.engine import trading_days
    from research.backtest.portfolio_engine import PortfolioParams, replay_portfolio
    from research.backtest.registry import STRATEGIES
    from research.backtest.report import Report, m3_panel
    from research.backtest.provenance import provenance, clean_db_build_dates
    from research.backtest.universe import point_in_time_universe
    from strategies.orb import ORBParams

    cfg = get_config()
    # M3 runs on the cleaned replica (dhan_clean), not raw dhan_trading — every
    # bars/universe query in the backtester goes through this engine.
    init_db(cfg.backtest_db_url)
    logger.info("Backtest DB = %s (clean replica)", cfg.backtest_db_name)
    logger.info("Strategy = %s  gate = %s", args.strategy, args.gate)

    if args.strategy not in STRATEGIES:
        logger.error("Unknown strategy %r; registered: %s", args.strategy,
                     ", ".join(STRATEGIES))
        return 1

    # OOS contamination guard for Run 3 (fine-tuned Kronos).
    # The fine-tune was trained and early-stopped on pre-2026 bars, so any OOS
    # window that overlaps the training data is contaminated.
    if (
        args.gate == "kronos"
        and cfg.kronos_checkpoint
        and args.split_date is not None
        and args.split_date < FINETUNE_OOS_CUTOFF
    ):
        logger.error(
            "CONTAMINATED OOS: --split-date %s is before FINETUNE_OOS_CUTOFF (%s). "
            "The fine-tuned Kronos checkpoint was trained/selected on pre-%s data — "
            "an OOS window starting before %s overlaps the training set and will "
            "produce invalid results. Either use a later split date or remove "
            "--gate kronos when evaluating this period.",
            args.split_date,
            FINETUNE_OOS_CUTOFF,
            FINETUNE_OOS_CUTOFF.year,
            FINETUNE_OOS_CUTOFF,
        )
        return 1

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
        strategy_name=args.strategy,
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

    # Derive the universe lookback default directly from the function signature so
    # the cache key and provenance stay in sync if the default ever changes.
    _UNIVERSE_LOOKBACK_DAYS = _universe_lookback_default()

    # Build the per-day universe (point-in-time, no look-ahead). This is the slow
    # part (one screener-ranking query per day) and is IDENTICAL across the three-way
    # runs (it doesn't depend on the gate), so --universe-cache lets the three runs
    # share a single build instead of repeating it 3x.
    fixed_ids = [s.strip() for s in args.ids.split(",")] if args.ids else None
    universe_by_day: dict = {}
    cache_path = args.universe_cache
    if cache_path and os.path.exists(cache_path) and not fixed_ids:
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            # Reuse only if it matches this run's universe definition AND covers every day.
            if (cached.get("n") == args.n
                    and cached.get("min_volume") == args.min_volume
                    and cached.get("lookback_days") == _UNIVERSE_LOOKBACK_DAYS
                    and all(d in cached["universe_by_day"] for d in days)):
                universe_by_day = {d: cached["universe_by_day"][d] for d in days}
                logger.info("Loaded universe for %d days from cache %s", len(days), cache_path)
            else:
                logger.info("Universe cache %s present but doesn't match (n/min_volume/"
                            "lookback_days/range) — rebuilding", cache_path)
        except Exception as exc:
            logger.warning("Universe cache %s unreadable (%s) — rebuilding", cache_path, exc)
    if not universe_by_day:
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
        if cache_path and not fixed_ids:
            # Guard: refuse to persist an all-empty universe — indicates a failed build.
            if not any(v for v in universe_by_day.values()):
                logger.warning("Universe is entirely empty — skipping cache write to avoid "
                               "poisoning future runs (check DB connectivity)")
            else:
                os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
                with open(cache_path, "wb") as f:
                    pickle.dump({"n": args.n, "min_volume": args.min_volume,
                                 "lookback_days": _UNIVERSE_LOOKBACK_DAYS,
                                 "universe_by_day": universe_by_day}, f)
                logger.info("Wrote universe cache %s (reuse across the three-way runs)", cache_path)

    trades, daily = await replay_portfolio(universe_by_day, pparams, gate_fn)

    # Gate failure visibility — must happen before JSON so error_rate is final.
    if gate is not None:
        gate.log_failure_summary()
        if gate.error_rate is not None and gate.error_rate >= 0.10:
            logger.error(
                "DEGRADED RUN: Kronos gate error rate %.1f%% (%d/%d calls failed). "
                "Run 3 likely degenerated to Run 1 (fail-open on every error). "
                "This result MUST NOT be compared against valid ungated runs. "
                "Inspect the warnings above for the root cause.",
                gate.error_rate * 100,
                gate.scoring_errors,
                gate.scoring_attempts,
            )

    panel = m3_panel(trades, args.equity, split_date=args.split_date)
    if args.gate == "kronos":
        if cfg.kronos_checkpoint:
            _gate_label = f"+ Kronos fine-tuned ({cfg.kronos_checkpoint})"
        else:
            _gate_label = "+ Kronos zero-shot"
    else:
        _gate_label = "standalone"
    label = f"{args.strategy.upper()} {_gate_label}  {args.from_date} → {args.to_date}  (portfolio)"
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
        # Determine whether the gate ever fired before trusting _loaded_from.
        _gate_scoring_attempts: int | None = None
        _gate_scoring_errors: int | None = None
        _gate_error_rate: float | None = None
        _kronos_loaded_from: str | None = None
        _gate_min_history: int | None = None
        _kronos_scorer_version: str | None = None

        if gate is not None:
            _gate_scoring_attempts = gate.scoring_attempts
            _gate_scoring_errors = gate.scoring_errors
            _gate_error_rate = gate.error_rate
            _gate_min_history = gate.min_history

            if gate.scoring_attempts == 0:
                # Gate was constructed but never invoked — "" would falsely imply
                # zero-shot; use a sentinel that makes the situation explicit.
                _kronos_loaded_from = "<never invoked>"
            else:
                try:
                    _kronos_loaded_from = getattr(gate._engine, "_loaded_from", None)
                except Exception:
                    pass

            try:
                from core.kronos_signal import scorer_version
                _kronos_scorer_version = scorer_version()
            except Exception:
                pass

        # Gather dhan_clean build dates (best-effort; None when unavailable).
        _build_dates = clean_db_build_dates()

        _is_degraded = (
            gate is not None
            and _gate_error_rate is not None
            and _gate_error_rate >= 0.10
        )

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
                "kronos_model": cfg.kronos_model if args.gate == "kronos" else None,
                "kronos_checkpoint": cfg.kronos_checkpoint if args.gate == "kronos" else None,
                "kronos_loaded_from": _kronos_loaded_from,
                "kronos_scorer_version": _kronos_scorer_version,
                "kronos_min_confidence": (cfg.kronos_min_confidence
                                          if args.gate == "kronos" else None),
                "kronos_seed": gate.seed if gate else None,
                "gate_min_history": _gate_min_history,
                "gate_scoring_attempts": _gate_scoring_attempts,
                "gate_scoring_errors": _gate_scoring_errors,
                "gate_error_rate": _gate_error_rate,
                "universe_lookback_days": _UNIVERSE_LOOKBACK_DAYS,
                "dhan_clean_min_built_at": _build_dates["min_built_at"],
                "dhan_clean_max_built_at": _build_dates["max_built_at"],
                "db": cfg.backtest_db_name,
            }),
            "label": label,
            "degraded": _is_degraded,
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
    # Delay the import so the CLI stays importable even before the registry is
    # populated (e.g. in CI where strategies beyond ORB are not yet installed).
    from research.backtest.registry import STRATEGIES as _STRATEGIES

    p = argparse.ArgumentParser(description="Intraday strategy backtest on real 1m bars")
    p.add_argument("--from", dest="from_date", required=True,
                   type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    p.add_argument("--to", dest="to_date", required=True,
                   type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    p.add_argument("--strategy", choices=list(_STRATEGIES), default="orb",
                   help=("Strategy to backtest (default: orb). "
                         f"Registered: {', '.join(_STRATEGIES)}"))
    p.add_argument("--ids", help="Comma-separated security IDs (skips the screener)")
    p.add_argument("--n", type=int, default=5, help="Universe size per day (default 5)")
    p.add_argument("--gate", choices=["none", "kronos"], default="none")
    p.add_argument("--equity", type=float, default=500_000.0)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--split-date", dest="split_date", default=None,
                   type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                   help="First OOS day (date-split): trades before = IS, on/after = OOS")
    p.add_argument("--min-volume", dest="min_volume", type=int, default=50_000,
                   help="Universe avg-volume floor (default 50k = live screener)")
    p.add_argument("--kronos-seed", dest="kronos_seed", type=int, default=0,
                   help="Seed for Kronos sampling (reproducible gate runs; default 0)")
    p.add_argument("--universe-cache", dest="universe_cache", default=None,
                   help="Path to build/reuse the point-in-time universe (shared across "
                        "the three-way runs so the slow per-day build happens once)")
    p.add_argument("--json", help="Write full results to this JSON file")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
