"""Portfolio-level replay tests — finite capital, concurrent cap, kill-switch,
no look-ahead, gate path, risk-budget rejection, SHORT, EOD. Bars are synthetic
+ the DB loader is monkeypatched, so these run with no database."""
import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from research.backtest import portfolio_engine as pe
from research.backtest.costs import round_trip_costs
from research.backtest.portfolio_engine import PortfolioParams, replay_portfolio

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2024, 3, 1)


def _session(*, or_high=101.0, or_low=100.0, after_close=103.0, after_closes=None,
             after_high=None, after_low=None, n_after=40, vol=5_000_000, day=DAY):
    """One synthetic 1m session: a flat 15-min opening range [or_low, or_high],
    then post-OR bars. Either a constant `after_close` (×n_after) or an explicit
    `after_closes` list (a price ramp, for target/stop/short paths)."""
    rows, t = [], datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    mid = (or_high + or_low) / 2
    for _ in range(15):                       # OR window 09:15–09:29
        rows.append((t, mid, or_high, or_low, mid, vol)); t += pd.Timedelta(minutes=1)
    closes = after_closes if after_closes is not None else [after_close] * n_after
    for c in closes:                          # post-OR
        hi = after_high if after_high is not None else c + 0.5
        lo = after_low if after_low is not None else c - 0.5
        rows.append((t, c, hi, lo, c, vol)); t += pd.Timedelta(minutes=1)
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])


def _patch_bars(monkeypatch, mapping: dict):
    monkeypatch.setattr(pe, "load_day_bars", lambda sid, day: mapping.get(sid, pd.DataFrame()))


def _run(universe_by_day, params, gate_fn=None):
    return asyncio.run(replay_portfolio(universe_by_day, params, gate_fn))


# ── basic / no-lookahead ───────────────────────────────────────────────────────

def test_long_breakout_produces_trade(monkeypatch):
    _patch_bars(monkeypatch, {"1": _session(after_close=103.0)})
    trades, daily = _run({DAY: ["1"]}, PortfolioParams(slippage_bps=0.0))
    assert len(trades) == 1 and trades[0].side == "LONG"
    assert daily[0]["kill_switch"] is False


def test_winning_long_applies_full_cost_stack(monkeypatch):
    # Breakout to 103, then ramp to 105 → ORB target (entry + 1.5×range) hits → WIN.
    ramp = [103.0, 103.0, 105.0, 105.0, 105.0, 105.0]
    _patch_bars(monkeypatch, {"1": _session(after_closes=ramp)})
    trades, _ = _run({DAY: ["1"]}, PortfolioParams(slippage_bps=0.0, tick_size=0.0,
                                                   partial_fill_pct=0.0))
    assert len(trades) == 1
    t = trades[0]
    assert t.gross_pnl > 0                       # genuinely profitable, not just costs
    # net is EXACTLY gross minus the independently-computed round-trip cost
    expect = round_trip_costs(t.entry_price, t.exit_price, t.qty).total
    assert t.costs == pytest.approx(expect)
    assert t.net_pnl == pytest.approx(round(t.gross_pnl - t.costs, 2))


def test_no_lookahead_fill_at_next_bar_open(monkeypatch):
    _patch_bars(monkeypatch, {"1": _session(after_close=103.0)})
    trades, _ = _run({DAY: ["1"]}, PortfolioParams(slippage_bps=0.0, tick_size=0.0))
    assert trades[0].entry_price == pytest.approx(103.0)


def test_tick_floor_slippage(monkeypatch):
    _patch_bars(monkeypatch, {"1": _session(after_close=103.0)})
    trades, _ = _run({DAY: ["1"]},
                     PortfolioParams(slippage_bps=0.0, tick_size=0.05, partial_fill_pct=0.0))
    assert trades[0].entry_price == pytest.approx(103.025, abs=0.01)
    assert trades[0].entry_price > 103.0


# ── SHORT side ─────────────────────────────────────────────────────────────────

def test_short_breakdown_end_to_end(monkeypatch):
    # Breakdown below or_low to 97, then fall to 95 → short target hit → winning short.
    ramp = [97.0, 97.0, 95.0, 95.0, 95.0, 95.0]
    _patch_bars(monkeypatch, {"1": _session(after_closes=ramp)})
    trades, _ = _run({DAY: ["1"]}, PortfolioParams(slippage_bps=0.0, tick_size=0.0,
                                                   partial_fill_pct=0.0))
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "SHORT"
    assert t.entry_price == pytest.approx(97.0)   # short entry at next-bar open
    assert t.gross_pnl > 0                        # price fell → short profits
    assert t.net_pnl == pytest.approx(round(t.gross_pnl - t.costs, 2))


# ── gate path ──────────────────────────────────────────────────────────────────

def test_gate_blocks_all_entries(monkeypatch):
    _patch_bars(monkeypatch, {"1": _session(after_close=103.0)})

    async def block_all(_sid, _side, _df):
        return False
    trades, _ = _run({DAY: ["1"]}, PortfolioParams(slippage_bps=0.0), gate_fn=block_all)
    assert trades == []


def test_gate_blocks_one_of_two(monkeypatch):
    _patch_bars(monkeypatch, {"1": _session(after_close=103.0), "2": _session(after_close=103.0)})

    async def block_sid1(sid, _side, _df):
        return sid != "1"          # allow everything except "1"
    trades, _ = _run({DAY: ["1", "2"]}, PortfolioParams(slippage_bps=0.0), gate_fn=block_sid1)
    assert len(trades) == 1 and trades[0].security_id == "2"


# ── portfolio constraints ──────────────────────────────────────────────────────

def test_concurrent_position_cap(monkeypatch):
    mapping = {str(i): _session(after_close=103.0) for i in range(1, 6)}
    _patch_bars(monkeypatch, mapping)
    params = PortfolioParams(slippage_bps=0.0, max_open_positions=2)
    trades, _ = _run({DAY: [str(i) for i in range(1, 6)]}, params)
    assert len(trades) == 2


def test_risk_budget_rejects_second_after_first_fills(monkeypatch):
    # sid "1" breaks out early and HOLDS (risk registered); sid "2" breaks out
    # later — by then committed_risk already ≈ the whole daily budget → "2" is
    # rejected by the aggregate risk-budget gate.
    s1 = _session(after_close=103.0)                              # breakout at bar 15
    later = [100.5] * 12 + [103.0] * 28                          # breakout ~bar 27
    s2 = _session(after_closes=later)
    _patch_bars(monkeypatch, {"1": s1, "2": s2})
    # risk_per_trade small enough that the RISK budget (not the notional cap)
    # binds: per-position risk ≈ equity×risk_per_trade = 500 = the whole daily
    # budget, so one position consumes it and the second is rejected.
    params = PortfolioParams(slippage_bps=0.0, tick_size=0.0, equity=100_000.0,
                             risk_per_trade=0.005, max_daily_loss_pct=0.005,
                             max_open_positions=10)
    trades, _ = _run({DAY: ["1", "2"]}, params)
    assert trades, "sid 1 should have entered"
    assert all(t.security_id == "1" for t in trades)             # "2" rejected by risk budget


def test_daily_loss_kill_switch_trips_and_blocks(monkeypatch):
    # Gap-aware fill design (all OHLC bars are physically valid: low ≤ open,close ≤ high).
    #
    # _session(after_closes=[103.0, 103.0, 90.0, 90.0, 90.0]) generates:
    #   • 15 OR bars  [or_low=100, or_high=101]
    #   • bar 15: open=103, high=103.5, low=102.5, close=103  ← ORB sees close>101 → ENTER LONG
    #   • bar 16: open=103, high=103.5, low=102.5, close=103  ← fill at open=103 (valid)
    #   • bar 17: open=90,  high=90.5,  low=89.5,  close=90   ← GAP-DOWN through stop (valid)
    #   • bars 18-19: same (session ends)
    #
    # Stop = or_low × (1 − 0.002) = 100 × 0.998 = 99.8.
    # Bar 17: bar_low=89.5 ≤ stop=99.8 → intrabar stop fires.
    # Gap-aware: fill_px = min(open=90, stop=99.8) = 90 (gapped through → fills at gap-open).
    # With risk_per_trade=0.01, equity=100_000: trade_budget=1_000, dist=3.2, qty≈312.
    # Realized loss = (90 − 103) × 312 = −4_056.
    # Daily budget = 100_000 × 0.02 = 2_000.  −4_056 << −2_000 → kill-switch trips on bar 18
    # kill-switch check (step 2): realized_today=−4_056 + unrealized=0 ≤ −budget → halt.
    _patch_bars(monkeypatch, {"1": _session(after_closes=[103.0, 103.0, 90.0, 90.0, 90.0])})
    params = PortfolioParams(slippage_bps=0.0, tick_size=0.0,
                             equity=100_000.0, risk_per_trade=0.01,
                             max_daily_loss_pct=0.02)
    _, daily = _run({DAY: ["1"]}, params)
    assert daily[0]["kill_switch"] is True
    assert daily[0]["net_pnl"] < 0


# ── fills ──────────────────────────────────────────────────────────────────────

def test_partial_fill_caps_qty(monkeypatch):
    _patch_bars(monkeypatch, {"1": _session(after_close=103.0, vol=5_000)})
    trades, _ = _run({DAY: ["1"]},
                     PortfolioParams(slippage_bps=0.0, tick_size=0.0, partial_fill_pct=0.10))
    assert trades[0].qty == 500       # 10% of 5,000


def test_partial_fill_zero_opens_nothing(monkeypatch):
    # vol=100, pct=0.001 → cap=int(0.1)=0 → no fill, no trade, no entry-slot burned.
    _patch_bars(monkeypatch, {"1": _session(after_close=103.0, vol=100)})
    trades, _ = _run({DAY: ["1"]},
                     PortfolioParams(slippage_bps=0.0, tick_size=0.0, partial_fill_pct=0.001))
    assert trades == []


def test_eod_forced_close(monkeypatch):
    # Session ends right after breakout (no target/stop) → forced EOD close.
    _patch_bars(monkeypatch, {"1": _session(after_close=103.0, n_after=3)})
    trades, _ = _run({DAY: ["1"]}, PortfolioParams(slippage_bps=0.0, tick_size=0.0))
    assert len(trades) == 1
    assert trades[0].exit_reason == "forced EOD close"


# ── multi-day ──────────────────────────────────────────────────────────────────

def test_equity_compounds_across_winning_days(monkeypatch):
    d1, d2 = date(2024, 3, 1), date(2024, 3, 4)
    ramp = [103.0, 103.0, 105.0, 105.0, 105.0, 105.0]   # winning long both days

    def loader(sid, day):
        return _session(after_closes=ramp, day=day)
    monkeypatch.setattr(pe, "load_day_bars", loader)
    _, daily = _run({d1: ["1"], d2: ["1"]},
                    PortfolioParams(slippage_bps=0.0, tick_size=0.0, partial_fill_pct=0.0))
    assert len(daily) == 2
    assert daily[0]["net_pnl"] > 0                       # day 1 wins (non-vacuous)
    assert daily[1]["equity_end"] > daily[0]["equity_end"]   # compounds


def test_kill_switch_day1_does_not_bleed_into_day2(monkeypatch):
    d1, d2 = date(2024, 3, 1), date(2024, 3, 4)

    # Day 1: same gap-down design as test_daily_loss_kill_switch_trips_and_blocks.
    # _session(after_closes=[103.0, 103.0, 90.0, 90.0, 90.0], day=d1) — all bars valid:
    #   bar 15 close=103 → ENTER LONG; bar 16 open=103 → fill; bar 17 open=90 (gap-down) →
    #   intrabar stop fires at fill_px=90; realized loss ≈ −4_056 >> budget −2_000.
    #   Kill-switch trips when bar 18's step-2 check sees realized_today ≤ −budget.
    crash = _session(after_closes=[103.0, 103.0, 90.0, 90.0, 90.0], day=d1)

    # Day 2: normal winning session — breakout to 103, ramp to 105 → target hit.
    win = _session(after_closes=[103.0, 103.0, 105.0, 105.0, 105.0, 105.0], day=d2)

    def loader(sid, day):
        return crash if day == d1 else win
    monkeypatch.setattr(pe, "load_day_bars", loader)
    _, daily = _run({d1: ["1"], d2: ["1"]},
                    PortfolioParams(slippage_bps=0.0, tick_size=0.0, partial_fill_pct=0.0,
                                    equity=100_000.0, risk_per_trade=0.01,
                                    max_daily_loss_pct=0.02))
    assert daily[0]["kill_switch"] is True
    assert daily[1]["kill_switch"] is False              # halt resets next day
