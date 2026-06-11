"""Report metrics — drawdown and Sharpe on known series."""
from datetime import date, datetime

from research.backtest.engine import BTTrade
from research.backtest.report import Report


def t(day, net, gross=None, sid="999"):
    g = gross if gross is not None else net
    ts = datetime(day.year, day.month, day.day, 10, 0)
    return BTTrade(security_id=sid, day=day, side="LONG", qty=10,
                   entry_ts=ts, entry_price=100, exit_ts=ts, exit_price=100,
                   exit_reason="t", gross_pnl=g, costs=round(g - net, 2), net_pnl=net)


def test_aggregates():
    trades = [t(date(2026, 1, 1), 100), t(date(2026, 1, 1), -50),
              t(date(2026, 1, 2), 200)]
    r = Report(trades, starting_equity=100_000)
    assert r.total_net == 250
    assert r.daily_pnl == {date(2026, 1, 1): 50, date(2026, 1, 2): 200}
    assert r.win_rate == 66.7
    assert r.profit_factor == 6.0


def test_max_drawdown():
    trades = [t(date(2026, 1, 1), 1000), t(date(2026, 1, 2), -2000),
              t(date(2026, 1, 3), 500)]
    r = Report(trades, starting_equity=100_000)
    # peak 101000 → trough 99000 → dd = 2000/101000 ≈ 1.98%
    assert r.max_drawdown_pct == 1.98


def test_sharpe_zero_variance():
    trades = [t(date(2026, 1, 1), 100), t(date(2026, 1, 2), 100)]
    r = Report(trades, starting_equity=100_000)
    assert r.sharpe == 0.0     # stdev 0 → defined as 0, not inf


def test_empty_report():
    r = Report([], starting_equity=100_000)
    assert r.total_net == 0
    assert r.win_rate == 0
    assert r.max_drawdown_pct == 0
    assert r.summary()["final_equity"] == 100_000
