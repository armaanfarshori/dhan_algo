"""
Backtest report — metrics computed the boring, correct way.

Sharpe is annualized from DAILY net P&L (√252), never from per-bar equity
points (the old backtester's √252 on 1-minute bars overstated it ~20×).
Max drawdown runs over the cumulative daily equity curve.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from math import sqrt
from statistics import mean, stdev

from research.backtest.engine import BTTrade


@dataclass
class Report:
    trades: list[BTTrade]
    starting_equity: float
    daily_pnl: dict[date, float] = field(init=False)

    def __post_init__(self):
        d = defaultdict(float)
        for t in self.trades:
            d[t.day] += t.net_pnl
        self.daily_pnl = dict(sorted(d.items()))

    # ── Metrics ───────────────────────────────────────────────────────────────

    @property
    def total_net(self) -> float:
        return round(sum(t.net_pnl for t in self.trades), 2)

    @property
    def total_gross(self) -> float:
        return round(sum(t.gross_pnl for t in self.trades), 2)

    @property
    def total_costs(self) -> float:
        return round(sum(t.costs for t in self.trades), 2)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return round(100 * sum(1 for t in self.trades if t.net_pnl > 0) / len(self.trades), 1)

    @property
    def profit_factor(self) -> float:
        wins = sum(t.net_pnl for t in self.trades if t.net_pnl > 0)
        losses = abs(sum(t.net_pnl for t in self.trades if t.net_pnl < 0))
        return round(wins / losses, 2) if losses else float("inf")

    @property
    def sharpe(self) -> float:
        """Annualized from daily net returns on starting equity."""
        if len(self.daily_pnl) < 2:
            return 0.0
        rets = [p / self.starting_equity for p in self.daily_pnl.values()]
        sd = stdev(rets)
        if sd == 0:
            return 0.0
        return round(mean(rets) / sd * sqrt(252), 2)

    @property
    def max_drawdown_pct(self) -> float:
        equity = self.starting_equity
        peak = equity
        max_dd = 0.0
        for pnl in self.daily_pnl.values():
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)
        return round(max_dd * 100, 2)

    def per_security(self) -> dict[str, dict]:
        agg: dict[str, dict] = {}
        for t in self.trades:
            a = agg.setdefault(t.security_id, {"trades": 0, "net": 0.0, "wins": 0})
            a["trades"] += 1
            a["net"] = round(a["net"] + t.net_pnl, 2)
            a["wins"] += 1 if t.net_pnl > 0 else 0
        return dict(sorted(agg.items(), key=lambda kv: kv[1]["net"], reverse=True))

    def summary(self) -> dict:
        return {
            "trades": len(self.trades),
            "days": len(self.daily_pnl),
            "gross_pnl": self.total_gross,
            "costs": self.total_costs,
            "net_pnl": self.total_net,
            "win_rate_pct": self.win_rate,
            "profit_factor": self.profit_factor,
            "sharpe_daily_ann": self.sharpe,
            "max_drawdown_pct": self.max_drawdown_pct,
            "final_equity": round(self.starting_equity + self.total_net, 2),
        }

    def print_report(self, label: str = "BACKTEST"):
        s = self.summary()
        print("\n" + "=" * 62)
        print(f"  {label}")
        print("=" * 62)
        for k, v in s.items():
            print(f"  {k.replace('_', ' '):<24} {v}")
        print("-" * 62)
        print(f"  {'per security':<24} trades   wins   net")
        for sid, a in self.per_security().items():
            print(f"  {sid:<24} {a['trades']:>6} {a['wins']:>6}   ₹{a['net']:>+12,.2f}")
        print("=" * 62)
        if self.trades and self.total_costs > 0 and self.total_gross != 0:
            ratio = abs(self.total_costs / self.total_gross) * 100
            print(f"  Costs consumed {ratio:.0f}% of gross P&L — "
                  f"if this is large, the edge is not real.\n")
