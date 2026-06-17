"""KPI panel, IS/OOS split, and provenance — pure (no DB)."""
from datetime import date, datetime

from research.backtest.engine import BTTrade
from research.backtest.report import Report, m3_panel
from research.backtest.provenance import provenance, git_sha


def _t(day: date, net: float, gross: float = None, costs: float = 10.0) -> BTTrade:
    gross = net + costs if gross is None else gross
    return BTTrade(security_id="X", day=day, side="LONG", qty=1,
                   entry_ts=datetime(day.year, day.month, day.day, 9, 30),
                   entry_price=100.0, exit_ts=datetime(day.year, day.month, day.day, 15, 0),
                   exit_price=101.0, exit_reason="t", gross_pnl=gross, costs=costs, net_pnl=net)


def test_payoff_and_cost_retention():
    trades = [_t(date(2024, 1, 2), 300, gross=320),
              _t(date(2024, 1, 3), -100, gross=-90),
              _t(date(2024, 1, 4), 200, gross=215)]
    r = Report(trades, 100_000)
    # avg win 250, avg loss 100 → payoff 2.5
    assert r.payoff_ratio == 2.5
    # net 400 / gross 445
    assert r.net_over_gross == round(400 / 445, 2)


def test_concentration_and_months():
    trades = [_t(date(2024, 1, 2), 1000), _t(date(2024, 2, 2), 50),
              _t(date(2024, 3, 2), -20)]
    r = Report(trades, 100_000)
    # one huge day dominates → top5 share ~ (1000+50-20)/1030*100... top5 of 3 days = all
    assert r.top5_day_share == 100.0
    # 2 of 3 months positive
    assert r.months_positive_pct == round(100 * 2 / 3, 1)


def test_longest_losing_streak():
    trades = [_t(date(2024, 1, 2), 10), _t(date(2024, 1, 3), -5),
              _t(date(2024, 1, 4), -5), _t(date(2024, 1, 5), -5),
              _t(date(2024, 1, 8), 10)]
    assert Report(trades, 100_000).longest_losing_streak == 3


def test_m3_panel_is_oos_split():
    trades = [_t(date(2024, 1, 2), 100), _t(date(2024, 6, 2), 80),
              _t(date(2024, 9, 2), -40)]
    panel = m3_panel(trades, 100_000, split_date=date(2024, 6, 1))
    assert panel["full"]["trades"] == 3
    assert panel["is"]["trades"] == 1          # only Jan
    assert panel["oos"]["trades"] == 2         # Jun + Sep
    assert panel["split_date"] == "2024-06-01"
    assert "oos_is_sharpe_ratio" in panel


def test_m3_panel_no_split_is_full_only():
    panel = m3_panel([_t(date(2024, 1, 2), 100)], 100_000)
    assert set(panel.keys()) == {"full"}


def test_provenance_has_sha_and_params():
    p = provenance({"equity": 500000, "gate": "none"})
    assert "git_sha" in p and "generated_at" in p
    assert p["params"]["gate"] == "none"
    assert isinstance(git_sha(), str)
