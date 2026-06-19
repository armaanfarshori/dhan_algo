"""
Event-driven session replay — the SAME pure ORB class the live engine runs,
driven bar-by-bar with no lookahead.

Fill model:
  • A decision made on bar i (using its close/high/low) executes at bar i+1's
    OPEN, plus adverse slippage — you cannot fill at the price that generated
    the signal. A decision on the session's last bar fills at that bar's close
    (square-off approximation).
  • Every closed round trip pays the full intraday cost stack (costs.py).
  • Intrabar wick detection: once a position is open, each bar is first tested
    for stop/target hits using bar.low/bar.high rather than bar.close — so a
    bar whose wick pierces a stop but closes above it is correctly recognised.
    Fill price is the stop or target level (with adverse slippage). If both
    fall inside one bar's range, stop fills first (conservative assumption).

Sizing reuses engine.risk.RiskEngine.size_position — identical stop-distance
math to the live trader, so backtest P&L is denominated in the same units.
Qty is calculated at the ACTUAL fill price (next-bar open), not the signal-bar
close, so gap opens cannot produce over- or under-leverage.

Kronos gating is pluggable: gate_fn(security_id, direction, bars_df_so_far)
→ bool. None = ORB standalone. research.backtest.kronos_gate provides the
zero-shot adapter for the three-way comparison.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
    risk_per_trade: float = 0.005      # matches live RiskParams/PortfolioParams default
    max_notional_pct: float = 0.20
    slippage_bps: float = 2.0
    tick_size: float = 0.05            # half-tick slippage floor (cheap/thin names)
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
    # Use IST-aware datetime bounds so the day window is correct regardless of
    # the DB session TZ. An implicit DATE→TIMESTAMPTZ coercion would be session-
    # TZ-dependent and produce wrong results under a non-UTC session.
    d0 = datetime(day.year, day.month, day.day, tzinfo=IST)
    d1 = d0 + timedelta(days=1)
    with get_session() as s:
        rows = s.execute(text("""
            SELECT time, open, high, low, close, volume
            FROM bars
            WHERE security_id = :sid AND timeframe = '1m'
              AND time >= :d0 AND time < :d1
            ORDER BY time
        """), {"sid": security_id, "d0": d0, "d1": d1}).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(IST)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    return df.reset_index(drop=True)


def _slip(price: float, side: str, bps: float, tick: float = 0.0) -> float:
    """Adverse slippage. Flat-bps by default; with a `tick` (e.g. NSE ₹0.05) the
    slippage is floored at half a tick, so cheap/thin names — where bps under-
    states real cost — pay at least the tick-driven minimum. tick=0 ⇒ pure bps."""
    s = max(price * bps / 10_000, tick / 2)
    return round(price + s if side == "BUY" else price - s, 2)


def _intrabar_exit(
    pos_side: str,
    bar_high: float,
    bar_low: float,
    bar_open: float,
    orb: "ORB",
    bps: float,
    tick: float,
) -> tuple[float, str] | None:
    """
    Detect a stop-loss or target hit using the bar's wick (high/low), mirroring
    the exact level formulas in strategies/orb.py section 4.

    Fidelity rationale: the live engine receives individual ticks, so a bar whose
    LOW pierces a long stop but CLOSES above it would correctly trigger a stop in
    production.  The backtester only has OHLC, so we approximate by treating any
    bar whose wick crosses the level as an intrabar fill at that level.

    Conservative tie-break: if both stop and target are breached by the same bar's
    wick, the stop fills first (realises the loss rather than the gain).

    Returns (fill_price, reason) or None if neither level is hit.
    """
    if pos_side == "LONG":
        # Mirror orb.py: stop = or_low * (1 - sl_buffer_pct)
        #                target = entry_price + target_multiplier * or_range
        stop_lvl   = orb.or_low * (1 - orb.p.sl_buffer_pct)
        target_lvl = orb.entry_price + orb.p.target_multiplier * orb.or_range
        stop_hit   = bar_low <= stop_lvl
        target_hit = bar_high >= target_lvl
        if stop_hit:
            # Gap-aware: a gap-down through the stop fills at the open, not the
            # stop (worse of stop vs gap-open) — gap losses must not be understated.
            fill = min(bar_open, stop_lvl)
            return _slip(fill, "SELL", bps, tick), f"Stop-loss ₹{fill:.2f} (wick)"
        if target_hit:
            return _slip(target_lvl, "SELL", bps, tick), f"Target hit ₹{target_lvl:.2f} (wick)"
    else:  # SHORT
        # Mirror orb.py: stop = or_high * (1 + sl_buffer_pct)
        #                target = entry_price - target_multiplier * or_range
        stop_lvl   = orb.or_high * (1 + orb.p.sl_buffer_pct)
        target_lvl = orb.entry_price - orb.p.target_multiplier * orb.or_range
        stop_hit   = bar_high >= stop_lvl
        target_hit = bar_low <= target_lvl
        if stop_hit:
            # Gap-aware (SHORT): a gap-up through the stop fills at the open.
            fill = max(bar_open, stop_lvl)
            return _slip(fill, "BUY", bps, tick), f"Stop-loss ₹{fill:.2f} (wick)"
        if target_hit:
            return _slip(target_lvl, "BUY", bps, tick), f"Target hit ₹{target_lvl:.2f} (wick)"
    return None


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
        RiskParams(equity_base=params.equity, risk_per_trade=params.risk_per_trade,
                   max_notional_pct=params.max_notional_pct),
        Portfolio(mode="BACKTEST"), ltp_lookup=lambda _s: 0.0)

    trades: list[BTTrade] = []
    # pending ENTER carries (kind, decision) — qty is deferred to fill time so it
    # is sized off the ACTUAL fill price (next-bar open), not the signal-bar close.
    pending: Optional[tuple[str, Decision]] = None
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
        bar_open  = float(bar["open"])
        bar_high  = float(bar["high"])
        bar_low   = float(bar["low"])
        bar_close = float(bar["close"])

        # 1. Execute the decision queued on the previous bar at THIS bar's open.
        if pending is not None:
            kind, d = pending
            pending = None
            if kind == "ENTER":
                # FIX (P0-sizing): size off the ACTUAL fill price (this bar's open
                # with slippage), not the signal-bar close.  A gap open can shift
                # the price materially; computing qty here prevents over-leverage.
                fill_px = _slip(bar_open, d.side, params.slippage_bps, params.tick_size)
                qty = sizer.size_position(entry=fill_px, stop=d.stop)
                if qty <= 0:
                    # Gap caused stop distance to collapse — skip the trade.
                    pass
                else:
                    pos_qty = qty
                    pos_entry_price = fill_px
                    pos_entry_ts = ts
                    pos_side = "LONG" if d.side == "BUY" else "SHORT"
                    orb.notify_fill(d.side, qty, fill_px)
            else:   # EXIT (strategy-driven, e.g. EOD square-off queued at prev bar)
                exit_side = "SELL" if pos_side == "LONG" else "BUY"
                fill_px = _slip(bar_open, exit_side, params.slippage_bps, params.tick_size)
                close_position(fill_px, ts, d.reason)

        # 2. Intrabar wick check — must run BEFORE on_tick so the strategy never
        #    sees a stale open position after a wick-triggered close.
        #
        # FIX (P0-wicks): the live engine receives individual ticks; a bar whose
        # LOW pierces a long stop but CLOSES above it correctly triggers the stop
        # in production.  With only OHLC available we approximate by treating any
        # bar whose wick crosses the stop/target level as an intrabar fill at that
        # level.  Stop fills before target (conservative: realises the loss).
        if pos_qty != 0:
            wick_result = _intrabar_exit(
                pos_side, bar_high, bar_low, bar_open, orb,
                params.slippage_bps, params.tick_size,
            )
            if wick_result is not None:
                fill_px, reason = wick_result
                close_position(fill_px, ts, reason)
                # Position is now flat; fall through so on_tick can build new
                # signals (e.g. the other direction) if the OR allows it.

        # 3. Let the strategy see the bar close (entry/EOD signals).
        decision = orb.on_tick(ts, bar_close, high=bar_high, low=bar_low)
        if decision is None:
            continue

        if decision.action == "ENTER" and pos_qty == 0:
            if gate_fn is not None:
                allowed = await gate_fn(security_id, decision.side, df.iloc[: i + 1])
                if not allowed:
                    continue   # direction already consumed inside ORB
            # Defer qty calculation to fill time (step 1, next bar) so it uses
            # the actual fill price — see FIX (P0-sizing) comment above.
            if i == n - 1:
                continue       # no next bar to fill on
            pending = ("ENTER", decision)

        elif decision.action == "EXIT" and pos_qty != 0:
            # on_tick exit via close price (EOD square-off or close crossing level).
            # The wick check above already handles intrabar stop/target, so this
            # branch is mainly the EOD square-off path and rare close-equals-stop.
            if i == n - 1:
                exit_side = "SELL" if pos_side == "LONG" else "BUY"
                fill_px = _slip(bar_close, exit_side, params.slippage_bps, params.tick_size)
                close_position(fill_px, ts, decision.reason)
            else:
                pending = ("EXIT", decision)

    # 4. Session ended with an open position (data gap / halt) — close at last bar.
    if pos_qty != 0:
        last = df.iloc[-1]
        exit_side = "SELL" if pos_side == "LONG" else "BUY"
        fill_px = _slip(float(last["close"]), exit_side, params.slippage_bps, params.tick_size)
        close_position(fill_px, last["time"].to_pydatetime(), "forced EOD close")

    return trades


def trading_days(from_date: date, to_date: date) -> list[date]:
    """Distinct session dates present in the bars table for the range."""
    from db import get_session
    # Use (time AT TIME ZONE 'Asia/Kolkata')::date so the date bucket matches the
    # IST calendar that universe.py enforces, regardless of DB session TZ.
    # Use IST-aware datetime bounds for the same reason as load_day_bars().
    d0 = datetime(from_date.year, from_date.month, from_date.day, tzinfo=IST)
    d1 = datetime(to_date.year, to_date.month, to_date.day, tzinfo=IST) + timedelta(days=1)
    with get_session() as s:
        rows = s.execute(text("""
            SELECT DISTINCT (time AT TIME ZONE 'Asia/Kolkata')::date FROM bars
            WHERE timeframe = '1m' AND time >= :d0 AND time < :d1
            ORDER BY 1
        """), {"d0": d0, "d1": d1}).fetchall()
    return [r[0] for r in rows]
