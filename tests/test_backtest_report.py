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


def test_edge_guards_no_trades_and_zero_equity():
    # No trades: profit_factor must be 0.0 (not inf), and metrics don't crash.
    r = Report([], 100_000)
    assert r.profit_factor == 0.0
    assert r.sharpe == 0.0 and r.max_drawdown_pct == 0.0
    assert r.net_over_gross == 0.0 and r.win_rate == 0.0
    # Zero starting equity + a losing day must not ZeroDivisionError.
    r0 = Report([_t(date(2024, 1, 2), -50)], 0.0)
    assert isinstance(r0.max_drawdown_pct, float)   # no crash


def test_net_over_gross_all_losing_is_computed():
    # All-losing system: gross<0 → ratio is still computed (costs make it worse),
    # not silently 0.0.
    trades = [_t(date(2024, 1, 2), -110, gross=-100),
              _t(date(2024, 1, 3), -210, gross=-200)]
    r = Report(trades, 100_000)
    assert r.net_over_gross == round(-320 / -300, 2)   # 1.07, not 0.0


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


# ── Fix #1: profit_factor all-wins sentinel ────────────────────────────────

def test_profit_factor_all_wins_returns_sentinel_not_inf():
    """All-win run: no losses → profit_factor must be a finite sentinel, not inf,
    so json.dumps of the panel does not crash."""
    import json
    trades = [_t(date(2024, 1, 2), 200), _t(date(2024, 1, 3), 100)]
    r = Report(trades, 100_000)
    pf = r.profit_factor
    assert pf == 9999.0, f"expected 9999.0, got {pf}"
    assert pf != float("inf")
    # Must serialize without error
    s = r.summary()
    json.dumps(s)  # raises ValueError if inf slips through


# ── Fix #2: OOS equity base ────────────────────────────────────────────────

def test_m3_panel_oos_equity_base_is_is_end_equity():
    """OOS Report must be initialised from IS-end equity, not IS-starting equity."""
    # IS: one big win → IS ends at 100_000 + 5000 = 105_000
    # OOS: a drawdown that is 5% of OOS starting equity
    is_trade = _t(date(2024, 1, 2), 5000)
    # OOS equity after IS = 105_000; a 5250-unit loss is exactly 5% of 105_000
    oos_trade = _t(date(2024, 7, 2), -5250)
    panel = m3_panel([is_trade, oos_trade], 100_000, split_date=date(2024, 6, 1))
    # max_drawdown_pct in OOS should be ~5 % (5250/105000), not ~5.25 % (5250/100000)
    oos_dd = panel["oos"]["max_drawdown_pct"]
    assert abs(oos_dd - 5.0) < 0.1, f"expected ~5.0%, got {oos_dd}%"
    # final_equity in OOS = OOS starting equity + OOS net_pnl = 105_000 + (-5250) = 99_750
    oos_final = panel["oos"]["final_equity"]
    assert abs(oos_final - 99_750.0) < 1, f"expected ~99750, got {oos_final}"


def test_m3_panel_oos_sharpe_unchanged_by_equity_base():
    """Sharpe is a ratio of (mean/stdev)*constant — the equity base cancels out.
    Verify: OOS Sharpe is the same regardless of whether OOS starting equity is
    the IS-start or IS-end, as long as the DAILY P&L series is the same.

    We confirm this numerically by recomputing OOS Sharpe both ways and asserting
    they match (or differ only by floating-point rounding)."""
    from math import sqrt
    from statistics import mean, stdev as _stdev
    is_trade = _t(date(2024, 1, 2), 3000)
    oos_trades = [_t(date(2024, 7, 2), 200), _t(date(2024, 8, 2), -80),
                  _t(date(2024, 9, 2), 150)]
    panel = m3_panel([is_trade] + oos_trades, 100_000, split_date=date(2024, 6, 1))
    oos_sharpe_panel = panel["oos"]["sharpe_daily_ann"]
    # Recompute by hand using IS-end equity (103_000 + 10 costs = 103_010 after costs)
    # The helper adds 10 costs, so IS net = 3000 - 10 = 2990; is_end = 102_990 ... but
    # our _t() helper sets gross=net+costs and net=net, so is_r.total_net = 3000.
    is_end_equity = 100_000 + 3000  # = 103_000
    daily_pnls = [200.0, -80.0, 150.0]
    rets_new = [p / is_end_equity for p in daily_pnls]
    rets_old = [p / 100_000 for p in daily_pnls]
    sharpe_new = round(mean(rets_new) / _stdev(rets_new) * sqrt(252), 2)
    sharpe_old = round(mean(rets_old) / _stdev(rets_old) * sqrt(252), 2)
    # Both should equal the panel value; the ratio mean/stdev cancels the equity scalar
    assert oos_sharpe_panel == sharpe_new, (
        f"panel OOS Sharpe {oos_sharpe_panel} != recomputed {sharpe_new}")
    assert sharpe_new == sharpe_old, (
        f"Sharpe changed with equity base: new={sharpe_new} old={sharpe_old} "
        "(Sharpe should be equity-base-invariant)")


# ── Fix #3: oos_is_sharpe_ratio with negative IS Sharpe ───────────────────

def test_oos_is_sharpe_ratio_none_on_negative_is_sharpe():
    """When IS Sharpe is negative, the ratio is misleading → must be None."""
    # Two IS days with losses: Sharpe will be negative
    is_trades = [_t(date(2024, 1, 2), -200), _t(date(2024, 2, 3), -100)]
    oos_trades = [_t(date(2024, 7, 2), 50), _t(date(2024, 8, 5), 30)]
    panel = m3_panel(is_trades + oos_trades, 100_000, split_date=date(2024, 6, 1))
    is_sharpe = panel["is"]["sharpe_daily_ann"]
    assert is_sharpe < 0, f"precondition failed: IS Sharpe should be negative, got {is_sharpe}"
    assert panel["oos_is_sharpe_ratio"] is None, (
        f"expected None for negative IS Sharpe, got {panel['oos_is_sharpe_ratio']}")


def test_oos_is_sharpe_ratio_present_on_positive_is_sharpe():
    """When IS Sharpe is positive, ratio must be a finite float (not None)."""
    is_trades = [_t(date(2024, 1, 2), 500), _t(date(2024, 2, 3), 300)]
    oos_trades = [_t(date(2024, 7, 2), 200), _t(date(2024, 8, 5), 100)]
    panel = m3_panel(is_trades + oos_trades, 100_000, split_date=date(2024, 6, 1))
    is_sharpe = panel["is"]["sharpe_daily_ann"]
    assert is_sharpe > 0, f"precondition: IS Sharpe should be positive, got {is_sharpe}"
    ratio = panel["oos_is_sharpe_ratio"]
    assert ratio is not None and isinstance(ratio, float)


# ── Fix #4: win_rate breakeven consistency ─────────────────────────────────

def test_win_rate_breakeven_counts_as_win():
    """A trade with net_pnl == 0 must count as a win (>= 0 semantics)."""
    trades = [_t(date(2024, 1, 2), 100),    # win
              _t(date(2024, 1, 3), 0),       # breakeven — should count as win
              _t(date(2024, 1, 4), -50)]     # loss
    r = Report(trades, 100_000)
    assert r.win_rate == round(100 * 2 / 3, 1), (
        f"breakeven should count as win; expected {round(100*2/3,1)}, got {r.win_rate}")


def test_per_security_wins_consistent_with_win_rate():
    """per_security() win counts must use the same >= 0 threshold as win_rate."""
    trades = [_t(date(2024, 1, 2), 100), _t(date(2024, 1, 3), 0), _t(date(2024, 1, 4), -50)]
    r = Report(trades, 100_000)
    ps = r.per_security()
    assert ps["X"]["wins"] == 2, (
        f"breakeven should count in per_security wins; got {ps['X']['wins']}")
