"""
MCX (commodities) futures backtest — evening-session ORB + momentum strategies.

The FIRST commodities backtest on the platform. Replays ``mcx_bars`` 5-min bars
for a symbol through the SAME pure strategy classes the equity engine runs
(``strategies/{orb,supertrend,donchian_breakout,ema_crossover}.py``), now bound
to the **MCX** market session (``core/sessions.MCX`` — one continuous 09:00 →
evening-close session whose evening close flips with US DST). The thesis lives
in the EVENING sub-window: ``orb_evening`` anchors the opening range at 18:00 IST
(US energy/metals overlap) while the EOD square-off keys off the session close.

Fill discipline (mirrors ``research/backtest/engine.py`` — NEVER fill on the
signal bar):
  • A decision made on bar i (using its close/high/low) executes at bar i+1's
    OPEN, plus adverse slippage. A decision on the session's last bar fills at
    that bar's close (square-off approximation).
  • Intrabar wick detection: once a position is open, each bar is first tested
    for stop/target hits using bar.low/bar.high (stop fills first if both hit).
  • Every closed round trip pays the full MCX statutory + brokerage stack via
    ``research/backtest/mcx_costs.mcx_futures_roundtrip`` on the per-side notional
    ``lot_size × fill_price``.

Sizing: ONE lot per trade (fixed). MCX margins/lot-notionals are large and
per-symbol; a fixed-lot sizing keeps the first study honest and comparable
across symbols (no equity-style risk-fraction sizing that would over-leverage on
a thin contract). Notional and P&L are therefore in ``lot_size``-denominated ₹.

Lot sizes come from a small documented per-symbol map (``MCX_LOT_SIZES``); every
value is ``# [verify-me]`` against the live MCX contract spec / Dhan scrip master
before any number is published.

HONESTY CAVEATS (inherited by every row; never dropped):
  • NEAR-MONTH recent data only (~Jan–Jun 2026, varies by contract — whatever
    ``mcx_bars`` holds) — NOT a multi-year sample.
  • FORWARD-ACCRUAL for depth: the sample grows only as live MCX bars are ingested.
  • 5-MIN granularity (no tick fidelity; intrabar fills approximated from OHLC).
  • Fixed ONE-lot sizing; no SPAN-margin model (return is on lot notional).
  • Costs are [verify-me] working figures (CTT/MCX-txn/stamp/GST).
  • PRELIMINARY · PAPER / research only — no live order paths.

CLI::

    python -m research.backtest.mcx_backtest \
        --symbols CRUDEOIL,CRUDEOILM,NATURALGAS,NATGASMINI \
        --strategies orb_evening,supertrend,donchian_breakout,ema_crossover \
        [--upload-s3]

``orb_evening`` = ORB(session=MCX, or_start_time=18:00). The other strategies run
with session=MCX (continuous-session square-off at the evening close). Results
(results.json + summary.txt) archive to ``s3://$S3_BUCKET/fno/mcx/<ts>/``
(fail-open; bucket from env).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from core.sessions import MCX, MarketSession
from research.backtest.mcx_costs import mcx_futures_roundtrip
from strategies.donchian_breakout import DonchianBreakout, DonchianBreakoutParams
from strategies.ema_crossover import EmaCrossover, EmaCrossoverParams
from strategies.orb import ORB, ORBParams, Decision
from strategies.supertrend import Supertrend, SupertrendParams

logger = logging.getLogger("dhan.backtest.mcx")
IST = ZoneInfo("Asia/Kolkata")

# Evening ORB anchor — 18:00 IST (US energy/metals overlap). The thesis window.
EVENING_OR_START = dtime(18, 0)

# ---------------------------------------------------------------------------
# Per-symbol lot sizes (units per lot) — ALL [verify-me] against the live MCX
# contract spec / Dhan scrip master. These set the per-side notional
# (lot_size × price) handed to the cost model AND the ₹ denomination of P&L.
# ---------------------------------------------------------------------------
MCX_LOT_SIZES: dict[str, int] = {
    "CRUDEOIL":    100,    # 100 bbl    # [verify-me]
    "CRUDEOILM":   10,     # 10 bbl (mini) # [verify-me]
    "NATURALGAS":  1250,   # 1250 mmBtu # [verify-me]
    "NATGASMINI":  250,    # 250 mmBtu (mini) # [verify-me]
    "GOLD":        100,    # 1 kg quoted per 10g → 100 units of the 10g price # [verify-me]
    "GOLDM":       10,     # 10 units (GOLD mini, 100g) # [verify-me]
    "SILVER":      30,     # 30 kg      # [verify-me]
    "SILVERM":     5,      # 5 kg (mini) # [verify-me]
    "COPPER":      2500,   # 2500 kg    # [verify-me]
}
DEFAULT_LOT_SIZE = 1  # fail-safe; logs a warning when a symbol is unmapped


def lot_size_for(symbol: str) -> int:
    """Lot size (units/lot) for a symbol from the documented [verify-me] map.

    An unmapped symbol falls back to ``DEFAULT_LOT_SIZE`` (1) with a warning — the
    notional/P&L will then be per-unit, which is wrong for a real contract, so the
    caller must add the symbol to ``MCX_LOT_SIZES`` before trusting the numbers.
    """
    s = symbol.strip().upper()
    if s not in MCX_LOT_SIZES:
        logger.warning(
            "mcx_backtest: lot size for %r not in MCX_LOT_SIZES — using %d (add it "
            "to MCX_LOT_SIZES [verify-me] before trusting P&L)", symbol, DEFAULT_LOT_SIZE,
        )
        return DEFAULT_LOT_SIZE
    return MCX_LOT_SIZES[s]


# ---------------------------------------------------------------------------
# Strategy registry for the MCX run (session-bound). Each builder takes a symbol
# (security_id stand-in for logging) + the MCX session and returns a strategy.
# ---------------------------------------------------------------------------
def _build_strategy(name: str, symbol: str, session: MarketSession):
    """Instantiate a session-bound strategy by MCX-CLI name. ``orb_evening`` is
    ORB anchored at 18:00 IST; the momentum names run with session=MCX."""
    if name == "orb_evening":
        return ORB(symbol, ORBParams(), session=session, or_start_time=EVENING_OR_START)
    if name == "orb":
        return ORB(symbol, ORBParams(), session=session)
    if name == "supertrend":
        return Supertrend(symbol, SupertrendParams(), session=session)
    if name == "donchian_breakout":
        return DonchianBreakout(symbol, DonchianBreakoutParams(), session=session)
    if name == "ema_crossover":
        return EmaCrossover(symbol, EmaCrossoverParams(), session=session)
    raise ValueError(f"unknown MCX strategy {name!r}")


MCX_STRATEGY_NAMES = (
    "orb_evening", "orb", "supertrend", "donchian_breakout", "ema_crossover",
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class McxBacktestParams:
    slippage_bps: float = 2.0          # adverse fill slippage (MCX top contracts liquid)
    tick_size: float = 0.0             # 0 ⇒ pure bps slippage (MCX ticks vary by symbol)
    min_session_bars: int = 6          # skip junk sessions shorter than this many 5m bars


@dataclass
class McxTrade:
    symbol: str
    day: date
    side: str                  # LONG | SHORT
    qty_lots: int
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    exit_reason: str
    gross_pnl: float
    costs: float
    net_pnl: float


@dataclass
class McxResult:
    symbol: str
    strategy: str
    lot_size: int
    n_trades: int
    net_pnl: float
    gross_pnl: float
    total_costs: float
    win_rate: float
    sharpe: float
    sharpe_oos: float
    max_drawdown: float
    avg_notional: float
    return_pct: float          # net_pnl / total deployed notional
    trades: list[McxTrade] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fill / metrics helpers (mirror engine.py discipline)
# ---------------------------------------------------------------------------
def _slip(price: float, side: str, bps: float, tick: float = 0.0) -> float:
    """Adverse slippage — flat bps, floored at half a tick when tick>0 (matches
    research/backtest/engine.py._slip). BUY pays up, SELL receives less."""
    s = max(price * bps / 10_000, tick / 2)
    return round(price + s if side == "BUY" else price - s, 2)


def _intrabar_exit(
    pos_side: str, bar_high: float, bar_low: float, bar_open: float,
    stop: float, target: float, bps: float, tick: float,
) -> Optional[tuple[float, str]]:
    """Stop/target wick detection (mirrors engine.py). Stop fills first if both
    are breached by the same bar (conservative). Gap-aware: a gap through the stop
    fills at the open, not the stop level."""
    if pos_side == "LONG":
        if bar_low <= stop:
            fill = min(bar_open, stop)
            return _slip(fill, "SELL", bps, tick), f"Stop-loss ₹{fill:.2f} (wick)"
        if bar_high >= target:
            return _slip(target, "SELL", bps, tick), f"Target hit ₹{target:.2f} (wick)"
    else:  # SHORT
        if bar_high >= stop:
            fill = max(bar_open, stop)
            return _slip(fill, "BUY", bps, tick), f"Stop-loss ₹{fill:.2f} (wick)"
        if bar_low <= target:
            return _slip(target, "BUY", bps, tick), f"Target hit ₹{target:.2f} (wick)"
    return None


def _sharpe(pnls: list[float]) -> float:
    """Per-trade Sharpe (mean/stdev of trade P&Ls). 0.0 with <2 trades or zero
    variance — kept simple and comparable to the F&O harness's per-trade Sharpe."""
    n = len(pnls)
    if n < 2:
        return 0.0
    mu = sum(pnls) / n
    var = sum((p - mu) ** 2 for p in pnls) / (n - 1)
    sd = math.sqrt(var)
    return mu / sd if sd > 0 else 0.0


def _max_drawdown(pnls: list[float]) -> float:
    """Max peak-to-trough drawdown of the cumulative trade-P&L curve (₹, <= 0)."""
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return mdd


# ---------------------------------------------------------------------------
# Session replay (one session, one strategy) — no look-ahead, next-bar fill
# ---------------------------------------------------------------------------
def replay_session(
    symbol: str, day: date, bars: list[dict[str, Any]],
    strategy, lot_size: int, params: McxBacktestParams,
) -> list[McxTrade]:
    """Replay one IST session's 5-min bars through ``strategy`` with next-bar-open
    fills. ``bars`` is a time-ascending list of dicts with keys
    time/open/high/low/close/volume (time = tz-aware IST datetime).

    Fixed ONE-lot sizing. Returns the closed round trips for the session.
    """
    n = len(bars)
    if n < params.min_session_bars:
        return []

    trades: list[McxTrade] = []
    pending: Optional[tuple[str, Decision]] = None
    pos_qty = 0  # in lots (0 or 1)
    pos_entry_price = 0.0
    pos_entry_ts: Optional[datetime] = None
    pos_side = ""
    pos_stop = 0.0
    pos_target = 0.0

    def close_position(exit_price: float, exit_ts: datetime, reason: str) -> None:
        nonlocal pos_qty, pos_entry_price, pos_entry_ts, pos_side, pos_stop, pos_target
        direction = 1 if pos_side == "LONG" else -1
        units = lot_size  # one lot
        gross = (exit_price - pos_entry_price) * units * direction
        notional_per_side = pos_entry_price * units
        costs = mcx_futures_roundtrip(notional_per_side, slippage_pct=0).total
        trades.append(McxTrade(
            symbol=symbol, day=day, side=pos_side, qty_lots=pos_qty,
            entry_ts=pos_entry_ts, entry_price=pos_entry_price,
            exit_ts=exit_ts, exit_price=exit_price, exit_reason=reason,
            gross_pnl=round(gross, 2), costs=costs,
            net_pnl=round(gross - costs, 2),
        ))
        pos_qty, pos_entry_price, pos_entry_ts, pos_side = 0, 0.0, None, ""
        pos_stop, pos_target = 0.0, 0.0
        strategy.notify_flat()

    for i in range(n):
        bar = bars[i]
        ts = bar["time"]
        bar_open = float(bar["open"])
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])
        vol = float(bar["volume"]) if bar.get("volume") is not None else 0.0

        # 1. Execute the decision queued on the previous bar at THIS bar's open
        #    (next-bar fill — never the signal bar).
        if pending is not None:
            kind, d = pending
            pending = None
            if kind == "ENTER":
                fill_px = _slip(bar_open, d.side, params.slippage_bps, params.tick_size)
                pos_qty = 1
                pos_entry_price = fill_px
                pos_entry_ts = ts
                pos_side = "LONG" if d.side == "BUY" else "SHORT"
                pos_stop = d.stop
                pos_target = d.target
                strategy.notify_fill(d.side, 1, fill_px)
            else:  # EXIT
                exit_side = "SELL" if pos_side == "LONG" else "BUY"
                fill_px = _slip(bar_open, exit_side, params.slippage_bps, params.tick_size)
                close_position(fill_px, ts, d.reason)

        # 2. Intrabar wick check BEFORE on_tick so a stale open position is never
        #    seen after a wick-triggered close.
        if pos_qty != 0:
            wick = _intrabar_exit(
                pos_side, bar_high, bar_low, bar_open,
                pos_stop, pos_target, params.slippage_bps, params.tick_size,
            )
            if wick is not None:
                fill_px, reason = wick
                close_position(fill_px, ts, reason)

        # 3. Strategy sees the bar close.
        decision = strategy.on_tick(ts, bar_close, high=bar_high, low=bar_low, volume=vol)
        if decision is None:
            continue

        if decision.action == "ENTER" and pos_qty == 0:
            if i == n - 1:
                continue  # no next bar to fill on
            pending = ("ENTER", decision)
        elif decision.action == "EXIT" and pos_qty != 0:
            if i == n - 1:
                exit_side = "SELL" if pos_side == "LONG" else "BUY"
                fill_px = _slip(bar_close, exit_side, params.slippage_bps, params.tick_size)
                close_position(fill_px, ts, decision.reason)
            else:
                pending = ("EXIT", decision)

    # 4. Session ended with an open position — force flat at the last bar's close.
    if pos_qty != 0:
        last = bars[-1]
        exit_side = "SELL" if pos_side == "LONG" else "BUY"
        fill_px = _slip(float(last["close"]), exit_side, params.slippage_bps, params.tick_size)
        close_position(fill_px, last["time"], "forced EOD close")

    return trades


# ---------------------------------------------------------------------------
# DB load + session grouping
# ---------------------------------------------------------------------------
def load_symbol_bars(symbol: str, timeframe: str = "5min") -> list[dict[str, Any]]:
    """Load all ``mcx_bars`` for one symbol/timeframe, time-ascending, IST-aware.

    Point-in-time: ordered strictly by time so the replay never sees a future bar.
    When more than one physical contract is stored under the same symbol (across
    monthly rolls), the highest open-interest row per timestamp is kept — the
    front/near-month line (a simple, roll-agnostic front-month proxy; continuous
    back-adjusted stitching is the unsolved TODO(mcx-continuous)).
    """
    from sqlalchemy import text

    from db import get_session

    with get_session() as s:
        rows = s.execute(text("""
            SELECT time, open, high, low, close, volume, open_interest
            FROM mcx_bars
            WHERE symbol = :sym AND timeframe = :tf
            ORDER BY time
        """), {"sym": symbol, "tf": timeframe}).fetchall()

    # Collapse duplicate timestamps to the front-month (max OI) row.
    by_ts: dict[datetime, dict[str, Any]] = {}
    for time_, o, h, lo, c, v, oi in rows:
        ts = time_
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(IST)
        oi_v = float(oi) if oi is not None else 0.0
        prev = by_ts.get(ts)
        if prev is None or oi_v >= prev["_oi"]:
            by_ts[ts] = {
                "time": ts, "open": float(o), "high": float(h),
                "low": float(lo), "close": float(c),
                "volume": float(v) if v is not None else 0.0, "_oi": oi_v,
            }
    out = [by_ts[k] for k in sorted(by_ts)]
    for r in out:
        r.pop("_oi", None)
    return out


def group_by_session(bars: list[dict[str, Any]]) -> dict[date, list[dict[str, Any]]]:
    """Bucket time-ascending bars by IST session date (MCX is one continuous
    09:00→evening session, so the IST calendar date is the session date)."""
    sessions: dict[date, list[dict[str, Any]]] = {}
    for b in bars:
        d = b["time"].date()
        sessions.setdefault(d, []).append(b)
    return sessions


# ---------------------------------------------------------------------------
# Per (symbol, strategy) backtest
# ---------------------------------------------------------------------------
def backtest_symbol_strategy(
    symbol: str, strategy_name: str,
    sessions: dict[date, list[dict[str, Any]]],
    params: McxBacktestParams,
    session_profile: MarketSession = MCX,
) -> McxResult:
    """Run one strategy over every session of one symbol; aggregate to McxResult.

    A fresh strategy instance is built per symbol (its session-reset handles the
    per-day boundary, but a clean instance avoids any cross-symbol state). Sessions
    are replayed in chronological order so the OOS split is time-honest.
    """
    lot_size = lot_size_for(symbol)
    strategy = _build_strategy(strategy_name, symbol, session_profile)

    all_trades: list[McxTrade] = []
    for day in sorted(sessions):
        all_trades.extend(
            replay_session(symbol, day, sessions[day], strategy, lot_size, params)
        )

    pnls = [t.net_pnl for t in all_trades]
    n = len(all_trades)
    net = sum(pnls)
    gross = sum(t.gross_pnl for t in all_trades)
    costs = sum(t.costs for t in all_trades)
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / n if n else 0.0

    # Chronological 70/30 OOS split (last 30% of trades).
    split = max(1, int(0.7 * n)) if n else 0
    pnls_oos = pnls[split:]
    sharpe_oos = _sharpe(pnls_oos) if len(pnls_oos) >= 2 else 0.0

    notionals = [t.entry_price * lot_size for t in all_trades]
    total_notional = sum(notionals)
    avg_notional = (total_notional / n) if n else 0.0
    return_pct = (net / total_notional) if total_notional > 0 else 0.0

    return McxResult(
        symbol=symbol, strategy=strategy_name, lot_size=lot_size,
        n_trades=n, net_pnl=round(net, 2), gross_pnl=round(gross, 2),
        total_costs=round(costs, 2), win_rate=win_rate,
        sharpe=_sharpe(pnls), sharpe_oos=sharpe_oos,
        max_drawdown=round(_max_drawdown(pnls), 2),
        avg_notional=round(avg_notional, 2), return_pct=return_pct,
        trades=all_trades,
    )


def run_backtest(
    symbols: list[str], strategies: list[str], timeframe: str,
    params: McxBacktestParams,
) -> list[McxResult]:
    """Full grid: every (symbol, strategy). Loads each symbol's bars once, groups
    into sessions, runs each strategy. Symbols with no bars are skipped (warned)."""
    results: list[McxResult] = []
    for symbol in symbols:
        bars = load_symbol_bars(symbol, timeframe)
        if not bars:
            logger.warning("mcx_backtest: no %s bars for %s — skipping", timeframe, symbol)
            continue
        sessions = group_by_session(bars)
        logger.info(
            "mcx_backtest: %s — %d %s bars across %d sessions",
            symbol, len(bars), timeframe, len(sessions),
        )
        for strat in strategies:
            results.append(
                backtest_symbol_strategy(symbol, strat, sessions, params)
            )
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
CAVEATS = (
    "NEAR-MONTH recent data only (~Jan–Jun 2026, varies by contract) · "
    "FORWARD-ACCRUAL for depth · 5-min granularity · fixed 1-lot (no SPAN) · "
    "costs [verify-me] · PRELIMINARY · PAPER only"
)


def _fmt_table(results: list[McxResult]) -> str:
    """Ranked (by net P&L) plain-text summary table."""
    lines: list[str] = []
    lines.append("MCX FUTURES BACKTEST — evening ORB + momentum (5-min, fixed 1-lot)")
    lines.append(f"caveats: {CAVEATS}")
    lines.append("")
    hdr = (
        f"{'symbol':<12} {'strategy':<18} {'lot':>5} {'n':>4} {'net':>12} "
        f"{'ret%':>8} {'win':>7} {'sharpe':>7} {'sh_oos':>7} {'maxDD':>12} {'costs':>11}"
    )
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for r in sorted(results, key=lambda x: x.net_pnl, reverse=True):
        lines.append(
            f"{r.symbol:<12} {r.strategy:<18} {r.lot_size:>5} {r.n_trades:>4} "
            f"{r.net_pnl:>12,.0f} {r.return_pct:>7.2%} {r.win_rate:>6.1%} "
            f"{r.sharpe:>7.2f} {r.sharpe_oos:>7.2f} {r.max_drawdown:>12,.0f} "
            f"{r.total_costs:>11,.0f}"
        )
    lines.append("-" * len(hdr))
    lines.append(
        "VERDICT (PRELIMINARY): ranked by net P&L on near-month recent 5-min bars. "
        "Real forward paper-log (full session + deeper history) is the truth test. PAPER only."
    )
    return "\n".join(lines)


def _result_to_json(r: McxResult) -> dict[str, Any]:
    """JSON-safe row (trades dropped — only the per-(symbol,strategy) metrics)."""
    return {
        "symbol": r.symbol, "strategy": r.strategy, "lot_size": r.lot_size,
        "n_trades": r.n_trades, "net_pnl": r.net_pnl, "gross_pnl": r.gross_pnl,
        "total_costs": r.total_costs, "win_rate": r.win_rate,
        "sharpe": r.sharpe, "sharpe_oos": r.sharpe_oos,
        "max_drawdown": r.max_drawdown, "avg_notional": r.avg_notional,
        "return_pct": r.return_pct,
    }


# ---------------------------------------------------------------------------
# S3 archival — fail-open, bucket from env (mirrors fno_orchestrator/finetune)
# ---------------------------------------------------------------------------
def archive_s3(
    results: list[McxResult], *, summary_text: str, args: dict[str, Any],
) -> Optional[str]:  # pragma: no cover — needs network/boto3
    """Write results.json + summary.txt + manifest.json under
    ``s3://<S3_BUCKET>/fno/mcx/<ts>/``. Fail-open: a missing bucket or any S3
    error warns but never crashes the run (stdout table is the deliverable)."""
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        print("WARNING: --upload-s3 set but S3_BUCKET is empty — skipping upload.")
        return None
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    prefix = f"fno/mcx/{run_ts}"
    results_json = {
        "results": [_result_to_json(r) for r in results],
        "caveats": CAVEATS.split(" · "),
    }
    manifest = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "args": args,
        "caveats": CAVEATS.split(" · "),
        "decision": "PRELIMINARY",
    }
    try:
        import boto3

        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=bucket, Key=f"{prefix}/results.json",
            Body=json.dumps(results_json, indent=2, default=str).encode(),
        )
        s3.put_object(Bucket=bucket, Key=f"{prefix}/summary.txt", Body=summary_text.encode())
        s3.put_object(
            Bucket=bucket, Key=f"{prefix}/manifest.json",
            Body=json.dumps(manifest, indent=2, default=str).encode(),
        )
        uri = f"s3://{bucket}/{prefix}/"
        print(f"Archived MCX backtest results to {uri}")
        return uri
    except Exception as exc:  # noqa: BLE001 — fail-open
        print(f"WARNING: S3 archival failed ({exc}) — results NOT uploaded.")
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def main() -> None:  # pragma: no cover — DB + CLI wiring
    parser = argparse.ArgumentParser(
        description="MCX futures backtest — evening ORB + momentum (5-min, near-month)",
    )
    parser.add_argument(
        "--symbols", default="CRUDEOIL,CRUDEOILM,NATURALGAS,NATGASMINI",
        help="CSV of MCX symbols (must exist in mcx_bars)",
    )
    parser.add_argument(
        "--strategies", default="orb_evening,supertrend,donchian_breakout,ema_crossover",
        help=f"CSV of strategies; choices: {', '.join(MCX_STRATEGY_NAMES)}",
    )
    parser.add_argument("--timeframe", default="5min", help="mcx_bars timeframe label (default 5min)")
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument(
        "--upload-s3", action="store_true", dest="upload_s3",
        help="archive results.json + summary.txt to s3://$S3_BUCKET/fno/mcx/<ts>/",
    )
    args = parser.parse_args()

    symbols = _parse_csv(args.symbols)
    strategies = _parse_csv(args.strategies)
    for st in strategies:
        if st not in MCX_STRATEGY_NAMES:
            raise SystemExit(
                f"unknown strategy {st!r}; choices: {', '.join(MCX_STRATEGY_NAMES)}"
            )

    params = McxBacktestParams(slippage_bps=args.slippage_bps)

    # DB-init only after arg-parse so --help never touches it.
    from config import get_config
    from db import init_db

    init_db(get_config().db_url)

    results = run_backtest(symbols, strategies, args.timeframe, params)
    summary_text = _fmt_table(results)
    print(summary_text)

    if args.upload_s3:
        archive_s3(results, summary_text=summary_text, args=vars(args))


if __name__ == "__main__":  # pragma: no cover
    main()
