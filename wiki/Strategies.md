# Strategies

Detailed documentation of each strategy's signal logic, state management, and exit mechanics.

---

## Options Scalper (`strategies/options_scalper.py`)

### Overview

The Options Scalper is a mean-reversion strategy for NIFTY index options. It monitors the NIFTY 50 index price via the DhanHQ OHLC endpoint, computes RSI-14, and takes a directional position in ATM options when RSI crosses an extreme threshold. The position is immediately bracketed by a Forever OCO order with target and stop prices derived from the breakeven premium.

The strategy is designed for intraday use only. All positions are forcibly closed at 15:15 IST regardless of OCO status.

---

### State Machine

```
          ┌──────────────────────────────────────────┐
          │              FLAT                        │
          │  Waiting for RSI crossover               │
          │  Scanning underlying every 10s           │
          └──────────┬───────────────────────────────┘
                     │
          RSI crosses oversold (30) → buy CALL
          RSI crosses overbought (70) → buy PUT
                     │
                     ▼
          ┌──────────────────────────────────────────┐
          │           ENTERING  (live only)          │
          │  Market buy order placed                 │
          │  Polling for TRADED status (max 10s)     │
          └──────────┬───────────────────────────────┘
                     │
              Fill confirmed
                     │
                     ▼
          ┌──────────────────────────────────────────┐
          │           IN_POSITION                    │
          │  Forever OCO placed (target + stop)      │
          │  OCO status polled every 10s             │
          │  Squareoff check at 15:15 IST            │
          └──────────┬───────────────────────────────┘
                     │
          OCO TRADED/CANCELLED/EXPIRED
          or squareoff time reached
                     │
                     ▼
                   FLAT
```

In paper mode the `ENTERING` state is skipped — the strategy transitions directly from `FLAT` to `IN_POSITION` with the quoted premium as the fill price.

---

### RSI Calculation

The platform uses Wilder's simple RSI (not Wilder's smoothed EMA version). This is intentional — it is faster to warm up (requires exactly `rsi_period + 1` ticks) and produces sharper crossover signals for scalping.

```python
gains  = [max(price[i] - price[i-1], 0) for i in range(1, period+1)]
losses = [abs(min(price[i] - price[i-1], 0)) for i in range(1, period+1)]
avg_gain = sum(gains) / period
avg_loss = sum(losses) / period
rs  = avg_gain / avg_loss
rsi = 100 - (100 / (1 + rs))
```

With `rsi_period=14`, the strategy requires 15 price ticks before any RSI value is available. At `poll_interval=10s`, warmup takes approximately 2.5 minutes from startup.

---

### Entry Signal Logic

A crossover is detected by comparing the current RSI to the previous RSI. This is stricter than a threshold-breach check — it fires exactly once per crossing, not on every tick while RSI is extreme.

**Call entry (RSI oversold crossover):**
```
prev_rsi > rsi_oversold (30)  AND  current_rsi <= rsi_oversold (30)
```
Interpretation: RSI just crossed below 30 — index is oversold, buy the ATM call.

**Put entry (RSI overbought crossover):**
```
prev_rsi < rsi_overbought (70)  AND  current_rsi >= rsi_overbought (70)
```
Interpretation: RSI just crossed above 70 — index is overbought, buy the ATM put.

Additional pre-entry filters applied in order:
1. Current IST time must be within `trade_start` (09:20) to `trade_end` (15:00)
2. Session order count must be below `max_orders` (10)
3. Risk manager must approve the order (`check_order()` returns `True`)
4. ATM contracts must exist in the instrument master for the active expiry
5. ATM option premium must be within `[min_premium, max_premium]` (₹10–₹200)

All filters must pass. Any failure logs a reason and skips the entry without changing state.

---

### ATM Discovery

ATM security IDs are never hardcoded. They are resolved at entry time:

1. The instrument master is queried: `master.find_atm(underlying_price, expiry, strike_step=50)`
2. ATM strike = `round(underlying_price / 50) * 50`
3. If the exact ATM is missing from the master (can happen near expiry), the search expands outward: ATM ± 50, ± 100, ± 150, ± 200, ± 250. The first strike with both CE and PE available is used.
4. The resolved security IDs are cached in `self.current_atm` for the duration of the position and visible via `/api/scalper`.

---

### Breakeven Calculation

All statutory charges are computed before placing the OCO so the target leg is guaranteed to be profitable after costs.

Given:
- `entry_premium` = fill price per unit (e.g., ₹45.00)
- `lot_size` = 75 (one NIFTY lot)
- `num_lots` = 1
- `qty` = 75

```
buy_turnover  = 45.00 × 75  = ₹3,375.00
sell_turnover = 45.00 × 75  = ₹3,375.00  (approximated at entry; actual exit price unknown)

brokerage    = ₹20 × 2 legs            = ₹40.00
stt          = 3,375 × 0.001           = ₹3.38   (sell side only)
exchange_fee = (3,375 + 3,375) × 0.00053 = ₹3.58
sebi_fee     = (6,750 / 10,000,000) × 10 = ₹0.0068
gst          = (40 + 3.58 + 0.0068) × 0.18 = ₹7.84
stamp_duty   = 3,375 × 0.00003         = ₹0.10

total_charges = 40 + 3.38 + 3.58 + 0.0068 + 7.84 + 0.10 = ₹54.91

breakeven_premium = 45.00 + (54.91 / 75) = 45.00 + 0.73 = ₹45.73
```

OCO target price  = `45.73 + 5.0 (target_buffer)` = **₹50.73**
OCO stop price    = `45.00 - 5.0 (stop_buffer)`   = **₹40.00**

The trade is profitable only if the option exits at or above ₹45.73. The target at ₹50.73 provides ₹5.00 of net profit per unit after all charges.

---

### OCO Order Structure

The platform uses DhanHQ's "Forever Order" API with `orderFlag="OCO"`. This is a bracket order that cancels the opposing leg automatically when one leg fills.

```json
{
  "orderFlag": "OCO",
  "transactionType": "SELL",
  "exchangeSegment": "NSE_FNO",
  "productType": "MARGIN",
  "orderType": "LIMIT",
  "securityId": "<ATM CE or PE sid>",
  "quantity": 75,
  "price": 50.73,
  "triggerPrice": 50.73,
  "price1": 40.00,
  "triggerPrice1": 40.00,
  "quantity1": 75
}
```

Leg 1 (primary): limit sell at the target price.
Leg 2 (secondary): limit sell at the stop price.
When either leg executes, DhanHQ cancels the other automatically.

---

### End-of-Day Squareoff

At 15:15 IST, if a position is open:
1. Any pending OCO order is cancelled via `DELETE /forever/orders/{id}`
2. A market sell order is placed for the full position quantity
3. State resets to `FLAT`

In paper mode, only the state reset occurs.

---

## SMA 9/21 Crossover (`strategies/strategy_base.py`)

### Overview

A classic dual-moving-average crossover strategy for NSE equities. Operates on a rolling deque of closing prices — no external data library required.

Configured by default for Reliance Industries (`security_id="2885"`, `NSE_EQ`) with 1-share lots. Change `security_id` in `main.py` to trade any NSE equity.

---

### Signal Logic

```
          ┌────────────────────────────────────┐
          │  Warmup Phase                      │
          │  Accumulate 21 ticks               │
          │  (fast deque: maxlen=9, slow: 21)  │
          └───────────┬────────────────────────┘
                      │  21 ticks received
                      ▼
          ┌────────────────────────────────────┐
          │  Active — evaluate every tick      │
          │                                    │
          │  fast_sma = mean(last 9 prices)    │
          │  slow_sma = mean(last 21 prices)   │
          │                                    │
          │  prev_fast <= prev_slow            │
          │  AND fast > slow                   │
          │  ──────────▶  GOLDEN CROSS → BUY  │
          │                                    │
          │  prev_fast >= prev_slow            │
          │  AND fast < slow                   │
          │  ──────────▶  DEATH CROSS → SELL  │
          └────────────────────────────────────┘
```

### Position Management

The strategy tracks a single integer position (long or short):

| Current Position | Signal | Action |
|---|---|---|
| 0 (flat) | Golden cross | BUY — go long |
| 0 (flat) | Death cross | SELL — go short |
| Long (+) | Death cross | EXIT — close long |
| Short (-) | Golden cross | EXIT — close short |

The strategy never reverses directly. It exits first (which brings position to 0), and the next tick handles the new entry if the signal persists.

### Warmup Progress

The `/api/status` endpoint includes warmup information while the strategy is accumulating ticks:

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

`ready: true` appears when `slow_current >= slow_required`.

---

## Building a Custom Strategy

Subclass `BaseStrategy` and implement `on_tick()`:

```python
from strategies.strategy_base import BaseStrategy, StrategyConfig, Signal

class MyStrategy(BaseStrategy):
    async def on_tick(self, tick: dict) -> Signal | None:
        price = tick["last_price"]
        # Your signal logic here
        if <buy condition>:
            return Signal(action="BUY", price=price, reason="my reason")
        return None
```

`BaseStrategy` provides:
- `self.buy(price, reason)` — risk-checked buy with paper/live branching
- `self.sell(price, reason)` — risk-checked sell
- `self.exit_position(price, reason)` — market exit in either direction
- `self.position` — net quantity (positive = long, negative = short)
- `self.entry_price` — last fill price
- `self.signals` — list of all signals generated (visible in `/api/signals`)
- `self.orders_placed` — counter towards `max_orders`

Wire it up in `main.py` the same way as the existing strategies. The web app, risk manager, and postback handler all work automatically with any `BaseStrategy` subclass.
