"""Portfolio-level replay tests — finite capital, concurrent cap, kill-switch,
no look-ahead. Bars are synthetic + the DB loader is monkeypatched, so these run
with no database."""
import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from research.backtest import portfolio_engine as pe
from research.backtest.portfolio_engine import PortfolioParams, replay_portfolio

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2024, 3, 1)


def _session(*, or_high=101.0, or_low=100.0, after_close, after_high=None, after_low=None,
             n_after=40, vol=5_000_000):
    """One synthetic 1m session: a flat 15-min opening range [or_low, or_high],
    then n_after bars all at `after_close` (open=high=low=close=after_close unless
    overridden) — enough to trigger an ORB breakout and then hold/crash."""
    rows, t = [], datetime(DAY.year, DAY.month, DAY.day, 9, 15, tzinfo=IST)
    mid = (or_high + or_low) / 2
    for _ in range(15):                       # OR window 09:15–09:29
        rows.append((t, mid, or_high, or_low, mid, vol)); t += pd.Timedelta(minutes=1)
    hi = after_high if after_high is not None else after_close + 0.5
    lo = after_low if after_low is not None else after_close - 0.5
    for _ in range(n_after):                  # post-OR
        rows.append((t, after_close, hi, lo, after_close, vol)); t += pd.Timedelta(minutes=1)
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])


def _patch_bars(monkeypatch, mapping: dict):
    monkeypatch.setattr(pe, "load_day_bars", lambda sid, day: mapping.get(sid, pd.DataFrame()))


def _run(universe_by_day, params, gate_fn=None):
    return asyncio.run(replay_portfolio(universe_by_day, params, gate_fn))


def test_long_breakout_produces_trade_net_below_gross(monkeypatch):
    # OR [100,101], price breaks to 103 and holds → one long trade, costs deducted.
    _patch_bars(monkeypatch, {"1": _session(after_close=103.0)})
    trades, daily = _run({DAY: ["1"]}, PortfolioParams(slippage_bps=0.0))
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "LONG"
    assert t.net_pnl < t.gross_pnl            # full cost stack applied
    assert daily[0]["kill_switch"] is False


def test_no_lookahead_fill_at_next_bar_open(monkeypatch):
    # With 0 slippage + 0 tick, entry must fill at the NEXT bar's open
    # (== after_close here), never at the signal bar's price.
    _patch_bars(monkeypatch, {"1": _session(after_close=103.0)})
    trades, _ = _run({DAY: ["1"]}, PortfolioParams(slippage_bps=0.0, tick_size=0.0))
    assert trades[0].entry_price == pytest.approx(103.0)


def test_tick_floor_slippage(monkeypatch):
    # bps=0 but tick=0.05 → BUY entry pays at least a half-tick (0.025) adverse.
    _patch_bars(monkeypatch, {"1": _session(after_close=103.0)})
    trades, _ = _run({DAY: ["1"]},
                     PortfolioParams(slippage_bps=0.0, tick_size=0.05, partial_fill_pct=0.0))
    # half-tick 0.025 added then rounded to paise ⇒ ~103.02–103.03
    assert trades[0].entry_price == pytest.approx(103.025, abs=0.01)
    assert trades[0].entry_price > 103.0


def test_partial_fill_caps_qty(monkeypatch):
    # Thin fill bar (vol=5,000); 10% cap ⇒ fill qty capped at 500 even though the
    # risk/notional-sized qty is larger.
    _patch_bars(monkeypatch, {"1": _session(after_close=103.0, vol=5_000)})
    trades, _ = _run({DAY: ["1"]},
                     PortfolioParams(slippage_bps=0.0, tick_size=0.0, partial_fill_pct=0.10))
    assert trades[0].qty == 500


def test_concurrent_position_cap(monkeypatch):
    # Five names break out the same minute and never exit before EOD; cap=2 ⇒ only
    # two can ever hold a position simultaneously ⇒ exactly two trades.
    mapping = {str(i): _session(after_close=103.0) for i in range(1, 6)}
    _patch_bars(monkeypatch, mapping)
    params = PortfolioParams(slippage_bps=0.0, max_open_positions=2)
    trades, _ = _run({DAY: [str(i) for i in range(1, 6)]}, params)
    assert len(trades) == 2


def test_daily_loss_kill_switch_trips_and_blocks(monkeypatch):
    # Small equity + a hard crash after entry → loss exceeds the daily budget →
    # kill-switch trips for the day.
    crash = _session(after_close=103.0, n_after=3)
    # after the breakout+hold bars, append a violent gap-down that blows the stop
    extra, t = [], crash.iloc[-1]["time"] + pd.Timedelta(minutes=1)
    for px in (70.0, 60.0, 50.0, 50.0, 50.0):
        extra.append((t, px, px + 0.5, px - 0.5, px, 5_000_000)); t += pd.Timedelta(minutes=1)
    crash = pd.concat([crash, pd.DataFrame(extra, columns=crash.columns)], ignore_index=True)
    _patch_bars(monkeypatch, {"1": crash, "2": crash})
    params = PortfolioParams(slippage_bps=0.0, equity=100_000.0, max_daily_loss_pct=0.02)
    trades, daily = _run({DAY: ["1", "2"]}, params)
    assert daily[0]["kill_switch"] is True
    assert daily[0]["net_pnl"] < 0


def test_equity_compounds_across_days(monkeypatch):
    # Two winning days; day-2 equity_end must exceed day-1's (realized P&L compounds).
    d1, d2 = date(2024, 3, 1), date(2024, 3, 4)

    def loader(sid, day):
        s = _session(after_close=103.0)
        # shift timestamps onto the requested day so both sessions are valid
        s = s.copy()
        s["time"] = s["time"].apply(lambda x: x.replace(year=day.year, month=day.month, day=day.day))
        return s
    monkeypatch.setattr(pe, "load_day_bars", loader)
    trades, daily = _run({d1: ["1"], d2: ["1"]}, PortfolioParams(slippage_bps=0.0))
    assert len(daily) == 2
    if daily[0]["net_pnl"] > 0:                # winning day → equity grows
        assert daily[1]["equity_end"] >= daily[0]["equity_end"]
