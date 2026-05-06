"""
Strategy implementations for both live trading and backtesting.
All strategies extend BaseStrategy and can be used with DhanClient
(live) or MockClient (backtest) transparently.

Live usage:  strategy = MomentumBreakoutStrategy(dhan, risk, config)
Backtest:    bt = Backtester(MomentumBreakoutStrategy, config); await bt.run(bars)
"""

from collections import deque
from dataclasses import dataclass
from typing import Optional, Dict

from strategies.strategy_base import BaseStrategy, StrategyConfig, Signal


# ── RSI helper ────────────────────────────────────────────────────────────────

def _rsi(prices: deque, period: int) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    pts = list(prices)[-(period + 1):]
    gains  = [max(pts[i] - pts[i-1], 0) for i in range(1, len(pts))]
    losses = [abs(min(pts[i] - pts[i-1], 0)) for i in range(1, len(pts))]
    avg_g  = sum(gains)  / period
    avg_l  = sum(losses) / period
    if avg_l == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_g / avg_l)), 2)


# ── 1. RSI Scalper (equity proxy) ─────────────────────────────────────────────

@dataclass
class RSIConfig(StrategyConfig):
    name: str       = "RSI_Scalper"
    rsi_period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0


class RSIScalperStrategy(BaseStrategy):
    """Buy when RSI crosses above oversold; sell when crosses below overbought."""

    def __init__(self, client, risk, config: RSIConfig):
        super().__init__(client, risk, config)
        self.rsi_cfg = config
        self._prices  = deque(maxlen=config.rsi_period + 2)
        self._prev_rsi: Optional[float] = None

    async def on_tick(self, tick: Dict) -> Optional[Signal]:
        price = tick.get("last_price", 0.0)
        if not price:
            return None
        self._prices.append(price)
        rsi = _rsi(self._prices, self.rsi_cfg.rsi_period)
        if rsi is None or self._prev_rsi is None:
            self._prev_rsi = rsi
            return None

        prev, self._prev_rsi = self._prev_rsi, rsi

        if prev > self.rsi_cfg.oversold >= rsi and self.position <= 0:
            return Signal("BUY",  price, f"RSI cross oversold {rsi:.1f}")
        if prev < self.rsi_cfg.overbought <= rsi and self.position >= 0:
            return Signal("SELL", price, f"RSI cross overbought {rsi:.1f}")
        if self.position > 0 and rsi >= self.rsi_cfg.overbought:
            return Signal("EXIT", price, f"RSI exit overbought {rsi:.1f}")
        if self.position < 0 and rsi <= self.rsi_cfg.oversold:
            return Signal("EXIT", price, f"RSI exit oversold {rsi:.1f}")
        return None


# ── 2. Momentum Breakout ──────────────────────────────────────────────────────

@dataclass
class MomentumConfig(StrategyConfig):
    name: str       = "Momentum_Breakout"
    lookback: int   = 20   # N-day high/low window
    atr_period: int = 14


class MomentumBreakoutStrategy(BaseStrategy):
    """Buy on N-day high break; exit on N-day low break."""

    def __init__(self, client, risk, config: MomentumConfig):
        super().__init__(client, risk, config)
        self.mom_cfg = config
        self._highs = deque(maxlen=config.lookback)
        self._lows  = deque(maxlen=config.lookback)

    async def on_tick(self, tick: Dict) -> Optional[Signal]:
        ohlc  = tick.get("ohlc", {})
        high  = ohlc.get("high",  tick.get("last_price", 0))
        low   = ohlc.get("low",   tick.get("last_price", 0))
        close = tick.get("last_price", 0)
        if not close:
            return None

        if len(self._highs) == self.mom_cfg.lookback:
            prev_high = max(self._highs)
            prev_low  = min(self._lows)

            if close > prev_high and self.position <= 0:
                sig = "EXIT" if self.position < 0 else "BUY"
                self._highs.append(high); self._lows.append(low)
                return Signal(sig, close, f"Breakout above {prev_high:.2f}")

            if close < prev_low and self.position >= 0:
                sig = "EXIT" if self.position > 0 else "SELL"
                self._highs.append(high); self._lows.append(low)
                return Signal(sig, close, f"Breakdown below {prev_low:.2f}")

        self._highs.append(high)
        self._lows.append(low)
        return None


# ── 3. Mean Reversion (RSI bands) ────────────────────────────────────────────

@dataclass
class MeanReversionConfig(StrategyConfig):
    name: str        = "Mean_Reversion"
    rsi_period: int  = 14
    entry_rsi: float = 25.0   # buy below this
    exit_rsi:  float = 55.0   # exit above this


class MeanReversionStrategy(BaseStrategy):
    """Buy extreme RSI oversold; exit at mean RSI."""

    def __init__(self, client, risk, config: MeanReversionConfig):
        super().__init__(client, risk, config)
        self.mr_cfg  = config
        self._prices = deque(maxlen=config.rsi_period + 2)

    async def on_tick(self, tick: Dict) -> Optional[Signal]:
        price = tick.get("last_price", 0.0)
        if not price:
            return None
        self._prices.append(price)
        rsi = _rsi(self._prices, self.mr_cfg.rsi_period)
        if rsi is None:
            return None

        if rsi < self.mr_cfg.entry_rsi and self.position <= 0:
            return Signal("BUY",  price, f"Extreme oversold RSI {rsi:.1f}")
        if self.position > 0 and rsi > self.mr_cfg.exit_rsi:
            return Signal("EXIT", price, f"Mean reversion RSI {rsi:.1f}")
        return None


# ── 4. Bollinger Band Reversion ───────────────────────────────────────────────

@dataclass
class BollingerConfig(StrategyConfig):
    name: str      = "Bollinger_Reversion"
    period: int    = 20
    std_dev: float = 2.0


class BollingerReversionStrategy(BaseStrategy):
    """Buy lower band touch; sell upper band touch."""

    def __init__(self, client, risk, config: BollingerConfig):
        super().__init__(client, risk, config)
        self.bb_cfg  = config
        self._prices = deque(maxlen=config.period)

    async def on_tick(self, tick: Dict) -> Optional[Signal]:
        import math
        price = tick.get("last_price", 0.0)
        if not price:
            return None
        self._prices.append(price)
        if len(self._prices) < self.bb_cfg.period:
            return None

        pts  = list(self._prices)
        mean = sum(pts) / len(pts)
        var  = sum((p - mean) ** 2 for p in pts) / len(pts)
        std  = math.sqrt(var)
        upper = mean + self.bb_cfg.std_dev * std
        lower = mean - self.bb_cfg.std_dev * std

        if price <= lower and self.position <= 0:
            return Signal("BUY",  price, f"Lower band ₹{lower:.2f} touch")
        if price >= upper and self.position >= 0:
            sig = "EXIT" if self.position > 0 else "SELL"
            return Signal(sig, price, f"Upper band ₹{upper:.2f} touch")
        if self.position > 0 and price >= mean:
            return Signal("EXIT", price, f"Mean reversion ₹{mean:.2f}")
        return None


# ── 5. VWAP Mean Reversion (intraday) ────────────────────────────────────────

@dataclass
class VWAPConfig(StrategyConfig):
    name: str        = "VWAP_Reversion"
    deviation_pct: float = 0.5   # % away from VWAP to trigger


class VWAPReversionStrategy(BaseStrategy):
    """Buy when price is deviation_pct% below VWAP; exit at VWAP."""

    def __init__(self, client, risk, config: VWAPConfig):
        super().__init__(client, risk, config)
        self.vwap_cfg     = config
        self._cum_vol_px  = 0.0
        self._cum_vol     = 0.0

    async def on_tick(self, tick: Dict) -> Optional[Signal]:
        price  = tick.get("last_price", 0.0)
        volume = tick.get("volume", 1)
        if not price:
            return None

        self._cum_vol_px += price * volume
        self._cum_vol    += volume
        vwap = self._cum_vol_px / self._cum_vol if self._cum_vol else price

        pct_dev = (price - vwap) / vwap * 100

        if pct_dev < -self.vwap_cfg.deviation_pct and self.position <= 0:
            return Signal("BUY",  price, f"Below VWAP {pct_dev:.2f}% · VWAP ₹{vwap:.2f}")
        if self.position > 0 and pct_dev > 0:
            return Signal("EXIT", price, f"VWAP recovery · VWAP ₹{vwap:.2f}")
        return None


# ── Registry ──────────────────────────────────────────────────────────────────

STRATEGY_REGISTRY = {
    "sma_crossover":     None,   # handled separately via SMAConfig
    "rsi_scalper":       (RSIScalperStrategy,       RSIConfig),
    "momentum_breakout": (MomentumBreakoutStrategy, MomentumConfig),
    "mean_reversion":    (MeanReversionStrategy,    MeanReversionConfig),
    "bollinger":         (BollingerReversionStrategy, BollingerConfig),
    "vwap_reversion":    (VWAPReversionStrategy,    VWAPConfig),
}
