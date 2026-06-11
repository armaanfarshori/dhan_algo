"""
Event-driven session replay — the SAME pure ORB class the live engine runs,
driven bar-by-bar with no lookahead.

Fill model:
  • A decision made on bar i (using its close/high/low) executes at bar i+1's
    OPEN, plus adverse slippage — you cannot fill at the price that generated
    the signal. A decision on the session's last bar fills at that bar's close
    (square-off approximation).
  • Every closed round trip pays the full intraday cost stack (costs.py).

Sizing reuses engine.risk.RiskEngine.size_position — identical stop-distance
math to the live trader, so backtest P&L is denominated in the same units.

Kronos gating is pluggable: gate_fn(security_id, direction, bars_df_so_far)
→ bool. None = ORB standalone. research.backtest.kronos_gate provides the
zero-shot adapter for the three-way comparison.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from engine.portfolio import Portfolio
from engine.risk import RiskEngine, RiskParams
from research.backtest.costs import round_trip_costs
from strategies.orb import ORB, ORBParams, Decision

logger = logging.getLogger("dhan.backtest.engine")
IST = ZoneInfo("Asia/Kolkata")

GateFn = Callable[[str, str, pd.DataFrame], Awaitable[bool]]


@dataclass
class BacktestParams:
    equity: float = 500_000.0
    risk_per_trade: float = 0.01
    max_notional_per_trade: float = 100_000.0
    slippage_bps: float = 2.0
    orb: ORBParams = field(default_factory=ORBParams)


@dataclass
class BTTrade:
    security_id: str
    day: date
    side: str                  # direction of the position: LONG | SHORT
    qty: int
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    exit_reason: str
    gross_pnl: float
    costs: float
    net_pnl: float


def load_day_bars(security_id: str, day: date) -> pd.DataFrame:
    """1-minute bars for one security/session, IST-indexed, time-ascending."""
    from db import get_session
    with get_session() as s:
        rows = s.execute(text("""
            SELECT time, open, high, low, close, volume
            FROM bars
            WHERE security_id = :sid AND timeframe = '1m'
              AND time >= :d0 AND time < :d1
            ORDER BY time
        """), {"sid": security_id, "d0": day, "d1": day + pd.Timedelta(days=1)}).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(IST)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    return df.reset_index(drop=True)


def _slip(price: float, side: str, bps: float) -> float:
    s = price * bps / 10_000
    return round(price + s if side == "BUY" else price - s, 2)


async def replay_security_day(
    security_id: str,
    day: date,
    params: BacktestParams,
    gate_fn: Optional[GateFn] = None,
    bars: Optional[pd.DataFrame] = None,
) -> list[BTTrade]:
    """Replay one security's session through the live ORB class."""
    df = bars if bars is not None else load_day_bars(security_id, day)
    # Need at least the OR window plus a few bars to trade — skip junk sessions
    if df.empty or len(df) < params.orb.orb_minutes + 3:
        return []

    orb = ORB(security_id, params.orb)
    sizer = RiskEngine(
        RiskParams(equity=params.equity, risk_per_trade=params.risk_per_trade,
                   max_notional_per_trade=params.max_notional_per_trade),
        Portfolio(mode="BACKTEST"), ltp_lookup=lambda _s: 0.0)

    trades: list[BTTrade] = []
    pending: Optional[tuple[str, Decision, int]] = None   # (kind, decision, qty)
    pos_qty = 0
    pos_entry_price = 0.0
    pos_entry_ts: Optional[datetime] = None
    pos_side = ""

    def close_position(exit_price: float, exit_ts: datetime, reason: str):
        nonlocal pos_qty, pos_entry_price, pos_entry_ts, pos_side
        direction = 1 if pos_side == "LONG" else -1
        gross = (exit_price - pos_entry_price) * pos_qty * direction
        buy_px, sell_px = ((pos_entry_price, exit_price) if direction == 1
                           else (exit_price, pos_entry_price))
        costs = round_trip_costs(buy_px, sell_px, pos_qty).total
        trades.append(BTTrade(
            security_id=security_id, day=day, side=pos_side, qty=pos_qty,
            entry_ts=pos_entry_ts, entry_price=pos_entry_price,
            exit_ts=exit_ts, exit_price=exit_price, exit_reason=reason,
            gross_pnl=round(gross, 2), costs=costs,
            net_pnl=round(gross - costs, 2)))
        pos_qty, pos_entry_price, pos_entry_ts, pos_side = 0, 0.0, None, ""
        orb.notify_flat()

    n = len(df)
    for i in range(n):
        bar = df.iloc[i]
        ts = bar["time"].to_pydatetime()

        # 1. Execute the decision queued on the previous bar at THIS bar's open
        if pending is not None:
            kind, d, qty = pending
            pending = None
            if kind == "ENTER":
                fill_px = _slip(float(bar["open"]), d.side, params.slippage_bps)
                pos_qty = qty
                pos_entry_price = fill_px
                pos_entry_ts = ts
                pos_side = "LONG" if d.side == "BUY" else "SHORT"
                orb.notify_fill(d.side, qty, fill_px)
            else:   # EXIT
                exit_side = "SELL" if pos_side == "LONG" else "BUY"
                fill_px = _slip(float(bar["open"]), exit_side, params.slippage_bps)
                close_position(fill_px, ts, d.reason)

        # 2. Let the strategy see the bar
        decision = orb.on_tick(ts, float(bar["close"]),
                               high=float(bar["high"]), low=float(bar["low"]))
        if decision is None:
            continue

        if decision.action == "ENTER" and pos_qty == 0:
            if gate_fn is not None:
                allowed = await gate_fn(security_id, decision.side, df.iloc[: i + 1])
                if not allowed:
                    continue   # direction already consumed inside ORB
            qty = sizer.size_position(entry=float(bar["close"]), stop=decision.stop)
            if qty <= 0:
                continue
            if i == n - 1:
                continue       # no next bar to fill on
            pending = ("ENTER", decision, qty)

        elif decision.action == "EXIT" and pos_qty != 0:
            if i == n - 1:
                exit_side = "SELL" if pos_side == "LONG" else "BUY"
                fill_px = _slip(float(bar["close"]), exit_side, params.slippage_bps)
                close_position(fill_px, ts, decision.reason)
            else:
                pending = ("EXIT", decision, 0)

    # 3. Session ended with an open position (data gap / halt) — close at last bar
    if pos_qty != 0:
        last = df.iloc[-1]
        exit_side = "SELL" if pos_side == "LONG" else "BUY"
        fill_px = _slip(float(last["close"]), exit_side, params.slippage_bps)
        close_position(fill_px, last["time"].to_pydatetime(), "forced EOD close")

    return trades


def trading_days(from_date: date, to_date: date) -> list[date]:
    """Distinct session dates present in the bars table for the range."""
    from db import get_session
    with get_session() as s:
        rows = s.execute(text("""
            SELECT DISTINCT time::date FROM bars
            WHERE timeframe = '1m' AND time >= :d0 AND time < :d1
            ORDER BY 1
        """), {"d0": from_date, "d1": to_date + pd.Timedelta(days=1)}).fetchall()
    return [r[0] for r in rows]
