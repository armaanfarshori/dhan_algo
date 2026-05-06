"""
Backtesting Engine
==================
Replay historical OHLCV data through any BaseStrategy to measure performance
before deploying to sandbox or live.

Usage:
    bt = Backtester(strategy_class=SMACrossoverStrategy, config=my_config)
    results = await bt.run(ohlcv_bars)
    bt.report()
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Type

import pandas as pd

from strategies.strategy_base import BaseStrategy, StrategyConfig, Signal

logger = logging.getLogger("dhan.backtest")


@dataclass
class BacktestResult:
    trades: List[Dict] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    starting_capital: float = 100_000.0

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winning_trades(self) -> int:
        return sum(1 for t in self.trades if t.get("pnl", 0) > 0)

    @property
    def losing_trades(self) -> int:
        return sum(1 for t in self.trades if t.get("pnl", 0) <= 0)

    @property
    def win_rate(self) -> float:
        if not self.total_trades:
            return 0.0
        return self.winning_trades / self.total_trades * 100

    @property
    def total_pnl(self) -> float:
        return sum(t.get("pnl", 0) for t in self.trades)

    @property
    def max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for val in self.equity_curve:
            if val > peak:
                peak = val
            dd = (peak - val) / peak * 100
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @property
    def sharpe_ratio(self) -> float:
        if len(self.equity_curve) < 2:
            return 0.0
        returns = [
            (self.equity_curve[i] - self.equity_curve[i - 1]) / self.equity_curve[i - 1]
            for i in range(1, len(self.equity_curve))
        ]
        import statistics
        avg = statistics.mean(returns) if returns else 0
        std = statistics.stdev(returns) if len(returns) > 1 else 1e-9
        return (avg / std) * (252 ** 0.5)  # annualised

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.trades)

    def summary(self) -> Dict:
        return {
            "total_trades":   self.total_trades,
            "win_rate_pct":   round(self.win_rate, 1),
            "total_pnl_inr":  round(self.total_pnl, 2),
            "max_drawdown_pct": round(self.max_drawdown, 2),
            "sharpe_ratio":   round(self.sharpe_ratio, 3),
            "final_capital":  round(self.starting_capital + self.total_pnl, 2),
        }


class MockClient:
    """Lightweight synchronous mock that replays bar data to the strategy."""
    def __init__(self, bars: List[Dict], security_id: str, exchange_segment: str):
        self.bars = bars
        self._idx = 0
        self.security_id = security_id
        self.exchange_segment = exchange_segment

    async def get_ohlc(self, instruments: Dict) -> Dict:
        if self._idx >= len(self.bars):
            raise StopIteration("No more bars")
        bar = self.bars[self._idx]
        self._idx += 1
        return {
            "data": {
                self.exchange_segment: {
                    self.security_id: {
                        "last_price": bar["close"],
                        "ohlc": {
                            "open":  bar.get("open",  bar["close"]),
                            "high":  bar.get("high",  bar["close"]),
                            "low":   bar.get("low",   bar["close"]),
                            "close": bar.get("close", bar["close"]),
                        },
                        "volume": bar.get("volume", 0),
                    }
                }
            }
        }

    # Stub out order calls so no real orders fire
    async def place_order(self, **kwargs):
        return {"orderId": "PAPER", "orderStatus": "TRADED"}


class MockRiskManager:
    """Risk manager stub that always approves for backtesting."""
    def check_order(self, *args, **kwargs):
        return True, "OK"

    def get_summary(self):
        return {}


class Backtester:
    """
    Run any BaseStrategy subclass against historical OHLCV data.

    Parameters
    ----------
    strategy_class : Type[BaseStrategy]
        The strategy class to instantiate.
    config : StrategyConfig
        Config for the strategy (paper_trading will be forced True).
    starting_capital : float
        Notional capital for equity curve tracking.
    """

    def __init__(
        self,
        strategy_class: Type[BaseStrategy],
        config: StrategyConfig,
        starting_capital: float = 100_000.0,
    ):
        self.strategy_class = strategy_class
        self.config = config
        self.starting_capital = starting_capital
        self.result = BacktestResult(starting_capital=starting_capital)

    async def run(self, bars: List[Dict]) -> BacktestResult:
        """
        bars: list of {"open":, "high":, "low":, "close":, "volume":, "date": "YYYY-MM-DD"}
        """
        self.config.paper_trading = True  # safety
        mock_client = MockClient(bars, self.config.security_id, self.config.exchange_segment)
        mock_risk   = MockRiskManager()

        strategy: BaseStrategy = self.strategy_class(mock_client, mock_risk, self.config)

        capital = self.starting_capital
        entry_trade: Dict = {}
        self.result.equity_curve = [capital]

        for bar_idx, bar in enumerate(bars):
            try:
                ohlc_data = await mock_client.get_ohlc({})
                tick = ohlc_data["data"][self.config.exchange_segment][self.config.security_id]
                signal: Signal = await strategy.on_tick(tick)

                if signal is None:
                    self.result.equity_curve.append(capital)
                    continue

                price = bar["close"]

                if signal.action == "BUY" and strategy.position == 0:
                    strategy.position = self.config.quantity
                    strategy.entry_price = price
                    entry_trade = {
                        "entry_date": bar.get("date", bar_idx),
                        "entry_price": price,
                        "direction": "LONG",
                        "reason": signal.reason,
                    }

                elif signal.action == "SELL" and strategy.position == 0:
                    strategy.position = -self.config.quantity
                    strategy.entry_price = price
                    entry_trade = {
                        "entry_date": bar.get("date", bar_idx),
                        "entry_price": price,
                        "direction": "SHORT",
                        "reason": signal.reason,
                    }

                elif signal.action == "EXIT" and strategy.position != 0:
                    direction = "LONG" if strategy.position > 0 else "SHORT"
                    pnl = (price - strategy.entry_price) * strategy.position
                    trade = {
                        **entry_trade,
                        "exit_date": bar.get("date", bar_idx),
                        "exit_price": price,
                        "pnl": round(pnl, 2),
                        "direction": direction,
                        "exit_reason": signal.reason,
                    }
                    self.result.trades.append(trade)
                    capital += pnl
                    strategy.position = 0
                    entry_trade = {}

                self.result.equity_curve.append(capital)

            except StopIteration:
                break
            except Exception as e:
                logger.error(f"Backtest bar {bar_idx} error: {e}")
                self.result.equity_curve.append(capital)

        logger.info(f"Backtest complete: {len(bars)} bars, {len(self.result.trades)} trades")
        return self.result

    def report(self):
        summary = self.result.summary()
        print("\n" + "=" * 50)
        print(f"  BACKTEST REPORT — {self.config.name}")
        print("=" * 50)
        for k, v in summary.items():
            label = k.replace("_", " ").title()
            print(f"  {label:<25} {v}")
        print("=" * 50)

        if self.result.trades:
            df = self.result.to_dataframe()
            print("\n  Last 5 Trades:")
            print(df[["entry_date", "exit_date", "direction", "pnl"]].tail(5).to_string(index=False))
        print()
