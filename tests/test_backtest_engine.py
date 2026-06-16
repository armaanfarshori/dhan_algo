"""Backtest replay — next-bar fills, no lookahead, costs deducted, gating."""
import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from research.backtest.engine import BacktestParams, replay_security_day
from strategies.orb import ORBParams

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 6, 11)


def make_bars(specs):
    """specs: list of (hh, mm, open, high, low, close)."""
    rows = [{"time": pd.Timestamp(datetime(2026, 6, 11, h, m, tzinfo=IST)),
             "open": o, "high": hi, "low": lo, "close": c, "volume": 1000}
            for h, m, o, hi, lo, c in specs]
    return pd.DataFrame(rows)


def session(breakout_then_target=True):
    """OR window 100-102; breakout at 9:31; then either target run or stop."""
    bars = [(9, 15 + i, 101, 102, 100, 101) for i in range(15)]   # OR build
    bars += [(9, 30, 101, 101.5, 100.5, 101)]                     # locked, no breakout
    bars += [(9, 31, 101, 103.2, 101, 103)]                       # close 103 > OR high
    bars += [(9, 32, 103.5, 104, 103, 103.8)]                     # fill bar (open 103.5)
    if breakout_then_target:
        # target = entry 103(close at decision) + 1.5×2 = 106 → run through it
        bars += [(9, 40, 105, 107, 105, 106.8)]                   # decision: target hit
        bars += [(9, 41, 106.5, 107, 106, 106.6)]                 # exit fill (open 106.5)
        bars += [(10, 0, 106, 106.5, 105.5, 106)]
    else:
        bars += [(9, 40, 100, 100.5, 99, 99.4)]                   # decision: stop
        bars += [(9, 41, 99.3, 99.5, 99, 99.2)]                   # exit fill
        bars += [(10, 0, 99, 99.5, 98.5, 99)]
    return make_bars(bars)


def run(bars, gate_fn=None, **pkw):
    params = BacktestParams(orb=ORBParams(), slippage_bps=0.0, **pkw)
    return asyncio.run(replay_security_day("999", DAY, params, gate_fn, bars=bars))


def test_next_bar_fill_no_lookahead():
    trades = run(session())
    assert len(trades) == 1
    t = trades[0]
    # Decision on the 9:31 bar (close 103) — fill at 9:32 OPEN 103.5, not 103
    assert t.entry_price == 103.5
    assert t.entry_ts.minute == 32
    # Exit decision on 9:40 bar — fill at 9:41 open 106.5
    assert t.exit_price == 106.5
    assert t.side == "LONG"


def test_costs_reduce_pnl():
    t = run(session())[0]
    assert t.costs > 0
    assert t.net_pnl == round(t.gross_pnl - t.costs, 2)
    assert t.gross_pnl == round((106.5 - 103.5) * t.qty, 2)


def test_stop_loss_path():
    t = run(session(breakout_then_target=False))[0]
    assert "Stop" in t.exit_reason
    assert t.net_pnl < 0


def test_risk_based_sizing():
    t = run(session(), equity=500_000, risk_per_trade=0.01)
    qty = t[0].qty
    # stop ≈ 100×(1−0.002)=99.8; entry at decision price 103 → distance 3.2
    # budget 5000/3.2 = 1562 → notional cap 100000/103 = 970
    assert qty == 970


def test_gate_blocks_entry():
    async def deny(_sid, _direction, _bars):
        return False
    trades = run(session(), gate_fn=deny)
    assert trades == []


def test_gate_receives_only_past_bars():
    seen = {}

    async def spy(_sid, _direction, bars):
        seen["last_ts"] = bars.iloc[-1]["time"]
        seen["n"] = len(bars)
        return True

    run(session(), gate_fn=spy)
    # Decision happens on the 9:31 bar — the gate must not see 9:32 onwards
    assert seen["last_ts"].minute == 31
    assert seen["n"] == 17    # 15 OR bars + 9:30 + 9:31


def test_open_position_forced_closed_at_session_end():
    bars = [(9, 15 + i, 101, 102, 100, 101) for i in range(15)]
    bars += [(9, 30, 101, 101.5, 100.5, 101),
             (9, 31, 101, 103.2, 101, 103),     # breakout decision
             (9, 32, 103.5, 104, 103, 103.8),   # fill
             (9, 33, 104, 104.5, 103.5, 104)]   # session data ends mid-flight
    trades = run(make_bars(bars))
    assert len(trades) == 1
    assert trades[0].exit_reason == "forced EOD close"
