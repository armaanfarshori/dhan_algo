"""
Strategy registry — maps strategy names to (StrategyClass, ParamsClass) pairs.

To add a new strategy:
1. Create ``strategies/<name>.py`` implementing the contract in
   ``research/backtest/strategy_specs/_CONTRACT.md``.
2. Import the class and its params dataclass below.
3. Add one line: ``STRATEGIES["<name>"] = (<Name>, <Name>Params)``

The backtester CLI (``python -m research.backtest --strategy <name>``) will then
accept the new name, instantiate ``<Name>(security_id, <Name>Params())`` per
security, and run the same engine / cost stack as ORB.

Required strategy interface (Protocol):

    class StrategyProtocol:
        def on_tick(
            self, now: datetime, price: float,
            high: Optional[float] = None,
            low: Optional[float] = None,
            volume: Optional[float] = None,
        ) -> Optional[Decision]: ...
        def notify_fill(self, side: str, qty: int, price: float) -> None: ...
        def notify_flat(self) -> None: ...
        # Optional: strategies needing prior-session levels (gap, CPR) expose
        #   def seed_prior_day(self, prior_high, prior_low, prior_close) -> None
        # which the engine calls once before the session if present.

``on_tick`` must return:
  - ``Decision(action="ENTER", side="BUY"|"SELL", stop=<float>, target=<float>, reason=<str>)``
    when a new position should be opened.  ``stop`` and ``target`` are ABSOLUTE price
    levels; the engine stores them and uses them for intrabar wick detection.
  - ``Decision(action="EXIT", reason=<str>)`` to close the current position.
  - ``None`` to do nothing.

See ``research/backtest/strategy_specs/_CONTRACT.md`` for the full contract
(session-reset, EOD square-off, future-skew guard, etc.).
"""

from strategies.orb import ORB, ORBParams
from strategies.ema_crossover import EmaCrossover, EmaCrossoverParams
from strategies.rsi2_meanrev import Rsi2MeanRev, Rsi2MeanRevParams
from strategies.macd_crossover import MacdCrossover, MacdCrossoverParams
from strategies.supertrend import Supertrend, SupertrendParams
from strategies.donchian_breakout import DonchianBreakout, DonchianBreakoutParams
from strategies.vwap_meanrev import VwapMeanReversion, VwapMeanRevParams
from strategies.vwap_trend import VwapTrend, VwapTrendParams
from strategies.bollinger_meanrev import BollingerMeanReversion, BollingerMeanReversionParams
from strategies.opening_gap import OpeningGap, OpeningGapParams
from strategies.cpr_pivot import CprPivot, CprPivotParams

# Maps CLI name → (StrategyClass, ParamsClass).
# Adding a strategy: append one entry; no other file needs to change.
STRATEGIES: dict[str, tuple[type, type]] = {
    "orb": (ORB, ORBParams),
    "ema_crossover": (EmaCrossover, EmaCrossoverParams),
    "rsi2_meanrev": (Rsi2MeanRev, Rsi2MeanRevParams),
    "macd_crossover": (MacdCrossover, MacdCrossoverParams),
    "supertrend": (Supertrend, SupertrendParams),
    "donchian_breakout": (DonchianBreakout, DonchianBreakoutParams),
    "vwap_meanrev": (VwapMeanReversion, VwapMeanRevParams),
    "vwap_trend": (VwapTrend, VwapTrendParams),
    "bollinger_meanrev": (BollingerMeanReversion, BollingerMeanReversionParams),
    "opening_gap": (OpeningGap, OpeningGapParams),
    "cpr_pivot": (CprPivot, CprPivotParams),
}
