# Strategies

Detailed documentation of each strategy's signal logic, state management, and exit mechanics. All strategies extend `BaseStrategy` and can run against either `DhanClient` (live/paper) or `MockClient` (backtesting) without code changes.

---

## 1. ORB — Opening Range Breakout (`strategies/strategy_orb.py`)

### Overview

A single-security intraday breakout strategy gated by the Kronos OHLCV foundation model. During the opening range window it tracks the high and low. After the range is locked, it takes a directional position when price breaks out. Kronos must agree with the direction before the trade is placed (fail-open on model errors). One trade per direction per security per session.

### State Machine

```
  09:15 ─────────────────────────────────────────────────────────────
  BUILDING RANGE
    Every tick: update OR_HIGH = max(OR_HIGH, candle_high)
                               OR_LOW  = min(OR_LOW,  candle_low)
    No trades during this phase.

  09:30 (for 15-min ORB) ────────────────────────────────────────────
  OR LOCKED  (OR_HIGH, OR_LOW fixed for the session)

    ┌──────────────────────────────────┐
    │  FLAT — watching for breakout    │
    └──────────────┬───────────────────┘
                   │
    price > OR_HIGH  AND  NOT long_taken
         │  Kronos agrees (or unavailable)?
         │  YES → BUY at market
         │  NO  → skip, set long_taken=True (don't retry same direction)
         ▼
    ┌──────────────────────────────────┐
    │  LONG POSITION                   │
    │  target = entry + 1.5 × OR_range │
    │  stop   = OR_LOW − sl_buffer     │
    └──────────────┬───────────────────┘
         price >= target → EXIT (Target hit)
         price <= stop   → EXIT (Stop-loss)
         15:15 IST       → EXIT (EOD square-off)

    price < OR_LOW  AND  NOT short_taken
         │  Kronos agrees?
         │  YES → SELL at market
         ▼
    ┌──────────────────────────────────┐
    │  SHORT POSITION                  │
    │  target = entry − 1.5 × OR_range │
    │  stop   = OR_HIGH + sl_buffer    │
    └──────────────────────────────────┘
         price <= target → EXIT (Target hit)
         price >= stop   → EXIT (Stop-loss)
         15:15 IST       → EXIT (EOD square-off)

  15:30 — session resets at next market open
```

### Kronos Gate (`_kronos_allows`)

```python
async def _kronos_allows(self, direction: str) -> bool:
    if not self.orb_cfg.use_kronos or self._kronos is None:
        return True   # no model configured — allow all trades

    result = await self._kronos.score_from_db(self.config.security_id, ...)
    agrees = (result["side"] == direction) and (result["confidence"] >= 0.4)

    # FAIL-OPEN: any exception → return True (never block on model error)
    return agrees
```

Kronos fetches the last 400 1-minute bars from the `ohlcv_1min` table, runs the Kronos-small model with 5 sampling paths, and returns a `{side, score, confidence, forecasted_return}` dict. The signal is valid only if Kronos's directional call matches the ORB direction AND confidence >= `kronos_min_confidence` (default 0.4).

### Key Parameters

| Parameter | Default | Description |
|---|---|---|
| `orb_minutes` | `15` | Opening range window (minutes from 9:15) |
| `sl_buffer_pct` | `0.002` | Stop offset as fraction of OR range |
| `target_multiplier` | `1.5` | Target = entry ± (1.5 × OR range) |
| `squareoff_before_close_min` | `15` | Force exit N minutes before 15:30 |
| `min_range_pct` | `0.003` | Skip if OR range < 0.3% of price |
| `kronos_min_confidence` | `0.4` | Min Kronos confidence to allow entry |

### Wire-up Example

```python
from strategies.strategy_orb import ORBStrategy, ORBConfig
from core.kronos_signal import get_kronos_engine

orb_cfg = ORBConfig(orb_minutes=15, use_kronos=True, kronos_min_confidence=0.4)
strategy = ORBStrategy(
    client=dhan,
    risk_manager=risk,
    config=StrategyConfig(security_id="2885", exchange_segment="NSE_EQ", ...),
    orb_config=orb_cfg,
    kronos_engine=get_kronos_engine(),
)
asyncio.create_task(strategy.run())
```

---

## 2. Options Scalper (`strategies/options_scalper.py`)

### Overview

A mean-reversion strategy for NIFTY index options. Monitors the NIFTY 50 index via the DhanHQ OHLC endpoint, computes RSI-14, and enters an ATM option when RSI crosses an extreme threshold. After every fill it immediately places a DhanHQ Forever OCO order at target and stop prices computed from the breakeven premium.

Designed for intraday use only. Force-closes all positions at 15:15 IST.

### State Machine

```
  ┌──────────────────────────────────────────┐
  │  FLAT — waiting for RSI crossover        │
  │  polling underlying every 10s            │
  └──────────┬───────────────────────────────┘
             │
  RSI crosses below 30 → BUY ATM Call
  RSI crosses above 70 → BUY ATM Put
  (crossover = prev > threshold, current <= threshold)
             │
             ▼
  ┌──────────────────────────────────────────┐
  │  ENTERING  (live mode only)              │
  │  market buy order placed                 │
  │  polling for TRADED status (max 10s)     │
  └──────────┬───────────────────────────────┘
             │  fill confirmed
             ▼
  ┌──────────────────────────────────────────┐
  │  IN_POSITION                             │
  │  Forever OCO placed immediately          │
  │  target = breakeven + ₹5 per unit        │
  │  stop   = entry − ₹5 per unit            │
  └──────────┬───────────────────────────────┘
             │
  OCO fills (TRADED) → back to FLAT
  15:15 IST          → cancel OCO, market sell → FLAT
```

In paper mode, ENTERING is skipped. The strategy goes directly FLAT → IN_POSITION with the quoted premium as the simulated fill price.

### RSI Calculation

Wilder's simple RSI (not exponentially smoothed). Chosen because it warms up in exactly `rsi_period + 1` ticks and produces sharper crossovers for scalping.

```python
gains  = [max(price[i] - price[i-1], 0) for i in range(1, period+1)]
losses = [abs(min(price[i] - price[i-1], 0)) for i in range(1, period+1)]
avg_gain = sum(gains) / period
avg_loss = sum(losses) / period
rsi = 100 - (100 / (1 + avg_gain / avg_loss))
```

At `poll_interval=10s` and `rsi_period=14`, warmup takes approximately 2.5 minutes from startup.

### Entry Filters (all must pass)

1. Current IST time is within `trade_start` (09:20) to `trade_end` (15:00)
2. Session order count below `max_orders` (10)
3. `risk.check_order()` returns True
4. ATM contract exists in the instrument master for the active expiry
5. ATM premium within `[min_premium, max_premium]` (₹10–₹200)

### ATM Discovery

ATM security IDs are resolved at entry time — never hardcoded.

1. `master.find_atm(underlying_price, expiry, strike_step=50)`
2. ATM strike = `round(underlying_price / 50) * 50`
3. If the exact ATM is missing (common near expiry), search expands outward in ±50 increments up to ±250. First strike with both CE and PE in the master is used.
4. Resolved IDs cached in `self.current_atm` until position closes. Visible at `/api/scalper`.

### Breakeven Calculation

Example with entry premium ₹45.00, qty = 75 (one lot):

```
buy_turnover  = ₹45.00 × 75 = ₹3,375.00
sell_turnover = ₹45.00 × 75 = ₹3,375.00  (estimated at entry price)

brokerage     = ₹20 × 2 legs           =   ₹40.00
stt           = ₹3,375 × 0.001         =    ₹3.38  (sell only)
exchange_fee  = ₹6,750 × 0.00053       =    ₹3.58  (both sides)
sebi_fee      = ₹6,750 / ₹10,000,000 × ₹10 = ₹0.01
gst           = (₹40 + ₹3.58 + ₹0.01) × 0.18 = ₹7.84
stamp_duty    = ₹3,375 × 0.00003       =    ₹0.10

total_charges = ₹54.91
breakeven_premium = ₹45.00 + (₹54.91 / 75) = ₹45.73

OCO target = ₹45.73 + ₹5.00 = ₹50.73   ← profitable by ₹5 net after all charges
OCO stop   = ₹45.00 − ₹5.00 = ₹40.00
```

### OCO Order Structure

The platform uses DhanHQ Forever Orders with `orderFlag="OCO"`. This is a bracket order that cancels the opposing leg when one leg fills.

```json
{
  "orderFlag":   "OCO",
  "transactionType": "SELL",
  "exchangeSegment": "NSE_FNO",
  "productType": "MARGIN",
  "orderType":   "LIMIT",
  "securityId":  "<ATM CE or PE>",
  "quantity":    75,
  "price":        50.73,
  "triggerPrice": 50.73,
  "price1":        40.00,
  "triggerPrice1": 40.00,
  "quantity1":   75
}
```

---

## 3. IndexOptionsScanner (`strategies/index_options.py`)

### Overview

Monitors all six tradeable NSE/BSE index underlyings simultaneously using a single bulk IDX_I LTP call per tick. For each index it maintains an independent RSI-14 state machine and position tracker. This is the primary strategy running in the default `main.py` configuration.

### Indices Monitored

| Index | Underlying ID | Segment | Lot Size |
|---|---|---|---|
| NIFTY | 13 | IDX_I | 75 |
| BANKNIFTY | 25 | IDX_I | 15 |
| SENSEX | 51 | IDX_I | 10 |
| FINNIFTY | 27 | IDX_I | 40 |
| NIFTYNXT50 | 38 | IDX_I | 25 |
| MIDCPNIFTY | 93 | IDX_I | 75 |

### Logic

Same RSI crossover logic as the Options Scalper but operating on all six indices in parallel. Max one position per index. Capital per position = 35% of paper balance divided across active positions.

---

## 4. SMA 9/21 Crossover (`strategies/strategy_base.py`)

### Overview

A dual-moving-average crossover strategy for NSE equities. Operates on a rolling deque of closing prices. Configured by default for Reliance Industries (`security_id="2885"`, `NSE_EQ`) with 1-share lots.

### Signal Logic

```
  Warmup phase: accumulate 21 ticks (slow_period)
    fast deque: maxlen=9
    slow deque: maxlen=21

  Active — evaluate every tick:

    fast_sma = mean(last 9 prices)
    slow_sma = mean(last 21 prices)

    prev_fast <= prev_slow  AND  fast > slow
    ──────────────────────────▶  GOLDEN CROSS → BUY

    prev_fast >= prev_slow  AND  fast < slow
    ──────────────────────────▶  DEATH CROSS → SELL/EXIT
```

### Position Management

| Current position | Signal | Action |
|---|---|---|
| 0 (flat) | Golden cross | BUY — go long |
| 0 (flat) | Death cross | SELL — go short |
| Long | Death cross | EXIT long |
| Short | Golden cross | EXIT short |

The strategy never reverses directly. It exits first (position → 0), then the next tick handles the new entry if the signal persists.

### Warmup Progress

The `/api/status` endpoint includes warmup information:

```json
{
  "warmup": {
    "fast_current": 7,
    "fast_required": 9,
    "slow_current": 7,
    "slow_required": 21,
    "ready": false
  }
}
```

---

## 5. Backtest Strategies (`strategies/backtest_strategies.py`)

Five additional strategies primarily used for backtesting via `/api/backtest/run`. They can also run live via `POST /api/strategy/switch`.

### RSI Scalper (`rsi_scalper`)

RSI-14 crossover on equities. Buys when RSI crosses above oversold (30); sells when RSI crosses above overbought (70).

| Config field | Default |
|---|---|
| `rsi_period` | `14` |
| `oversold` | `30.0` |
| `overbought` | `70.0` |

### Momentum Breakout (`momentum_breakout`)

N-day high/low breakout with ATR filter. Long when price breaks above a 20-day high; short when it breaks below a 20-day low. ATR-14 confirms momentum (price move must exceed a fraction of ATR).

| Config field | Default |
|---|---|
| `lookback` | `20` |
| `atr_period` | `14` |

### Mean Reversion (`mean_reversion`)

Bollinger Band mean reversion. Goes long when price touches the lower band; short when it touches the upper band. Exits at the midline.

### Bollinger Reversion (`bollinger`)

Same logic as mean reversion but with configurable band width and period.

### VWAP Reversion (`vwap_reversion`)

Tracks intraday VWAP. Buys when price is significantly below VWAP; sells when significantly above.

---

## Building a Custom Strategy

Subclass `BaseStrategy` and implement `on_tick()`:

```python
from strategies.strategy_base import BaseStrategy, StrategyConfig, Signal

class MyStrategy(BaseStrategy):
    async def on_tick(self, tick: dict) -> Signal | None:
        price = tick["last_price"]

        if <buy condition>:
            return Signal(action="BUY", price=price, reason="my condition")
        return None
```

`BaseStrategy` provides:

| Attribute / Method | Description |
|---|---|
| `self.buy(price, reason)` | Risk-checked buy; paper/live branching |
| `self.sell(price, reason)` | Risk-checked sell |
| `self.exit_position(price, reason)` | Market exit in either direction |
| `self.position` | Net quantity (positive = long, negative = short, 0 = flat) |
| `self.entry_price` | Last fill price |
| `self.signals` | List of all signals generated (appears in `/api/signals`) |
| `self.orders_placed` | Counter against `max_orders` |
| `self.config` | `StrategyConfig` dataclass with `security_id`, `exchange_segment`, `paper_trading`, etc. |

Wire the strategy in `main.py` the same way as the existing strategies, or switch it at runtime via `POST /api/strategy/switch` with `{"strategy": "my_strategy_key"}`.

---

## Kronos Signal Engine (`core/kronos_signal.py`)

Used as a pre-filter inside ORBStrategy. Can also be called directly.

```python
from core.kronos_signal import get_kronos_engine

engine = get_kronos_engine()
await engine.load()   # downloads model once (~300MB); cached

# Score from TimescaleDB
signal = await engine.score_from_db(security_id="2885", lookback=400)
# Returns: {"side": "BUY"|"SELL"|"HOLD", "score": float, "confidence": float, ...}

# Score from a DataFrame
signal = await engine.score(ohlcv_df, pred_len=30, sample_count=5)

# Batch scoring
results = await engine.score_batch(["2885", "1333", "1594"], exchange_segment="NSE_EQ")
```

The `score` and `confidence` values:
- `score` = absolute value of `forecasted_return` (directional strength)
- `confidence` = `1.0 - std(pred_close) / price * 10`, capped at 1.0 (sampling agreement)
- `side` = `"BUY"` if `forecasted_return > KRONOS_THRESH`, `"SELL"` if `< -KRONOS_THRESH`, else `"HOLD"`

Kronos is a singleton — `get_kronos_engine()` always returns the same instance.
