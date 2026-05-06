# Configuration Reference

All runtime behaviour is controlled through environment variables loaded from `.env` at startup. Strategy internals are controlled through dataclass config objects in code. This page documents both.

---

## Environment Variables (`.env`)

### Required

| Variable | Type | Description |
|---|---|---|
| `DHAN_CLIENT_ID` | string | Your DhanHQ client ID. Found in the DhanHQ developer portal. Used in every API request as the `client-id` header and in order payloads as `dhanClientId`. |
| `DHAN_ACCESS_TOKEN` | string | JWT access token from the DhanHQ developer portal. Passed as the `access-token` header. Tokens expire — regenerate them when you see 401 errors. |

### Trading Mode

| Variable | Type | Default | Description |
|---|---|---|---|
| `PAPER_TRADING` | boolean string | `true` | When `true`, all order calls are simulated in memory. No API order endpoints are called. Position state is tracked locally. Set to `false` only for live trading. |
| `STRATEGY` | string | `scalper` | Selects the active strategy. Valid values: `scalper` (Options Scalper on NIFTY F&O) or `sma` (SMA 9/21 crossover on NSE equities). Case-insensitive. |

### Risk Controls

| Variable | Type | Default | Description |
|---|---|---|---|
| `MAX_DAILY_LOSS` | float | `5000` | Maximum allowed daily loss in INR (realised + unrealised combined). When total P&L drops below `-MAX_DAILY_LOSS`, the risk manager halts all new orders and fires halt callbacks. The halt persists until manually cleared via `risk.resume()`. |

### Options Scalper Specific

| Variable | Type | Default | Description |
|---|---|---|---|
| `EXPIRY_DATE` | string | _(empty)_ | Target option expiry date in `YYYY-MM-DD` format. When blank, the instrument master automatically selects the nearest upcoming weekly expiry. Set this explicitly if you want to trade a specific expiry rather than the default nearest-weekly. |
| `NUM_LOTS` | integer | `1` | Number of NIFTY option lots to trade per entry. One lot is 75 units. Total quantity = `NUM_LOTS * 75`. The risk manager checks capital exposure before every entry regardless of this setting. |

### Server

| Variable | Type | Default | Description |
|---|---|---|---|
| `WEBHOOK_PORT` | integer | `8765` | TCP port for the aiohttp web server. Serves the React dashboard at `/`, the JSON API at `/api/*`, and receives DhanHQ order postback events at `POST /postback`. Bind to `0.0.0.0` — restrict with a firewall if running on a remote server. |

---

## Strategy Config Objects

Strategy behaviour is defined in dataclass objects in code. To change strategy parameters beyond what `.env` exposes, edit `main.py` directly.

### `RiskConfig` (`core/risk.py`)

Instantiated in `main.py` and passed to `RiskManager`.

| Field | Type | Default | Description |
|---|---|---|---|
| `max_daily_loss` | float | `5000.0` | Daily P&L floor in INR. Sourced from `MAX_DAILY_LOSS` env var. |
| `max_open_positions` | int | `10` | Hard cap on concurrent open positions. `check_order()` returns `False` if this is reached. Hardcoded to `5` in `main.py`. |
| `max_capital_utilisation_pct` | float | `80.0` | Reserved for future capital-based position sizing. Not enforced in current release. |
| `max_loss_per_trade` | float | `1000.0` | Maximum single-trade capital exposure in INR (quantity × price). Set to `2000` in `main.py`. Orders that exceed this are rejected pre-trade. |
| `auto_squareoff_minutes_before_close` | int | `15` | Reserved field. Squareoff timing is currently handled inside each strategy via `squareoff_time`. |
| `check_interval_seconds` | int | `30` | How often the risk monitor evaluates positions and P&L. |
| `kill_switch` | bool | `False` | When `True`, all new orders are blocked. Activated via `risk.activate_kill_switch()`. |

### `OptionsScalperConfig` (`strategies/options_scalper.py`)

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | `Options_Scalper` | Display name used in logs and dashboard. |
| `underlying_security_id` | str | `"13"` | DhanHQ security ID for the NIFTY 50 index. Used for fetching OHLC data to compute RSI. Do not change unless Dhan changes their instrument IDs. |
| `underlying_exchange` | str | `"IDX_I"` | Exchange segment for the underlying index. `IDX_I` = index feed. |
| `option_exchange` | str | `"NSE_FNO"` | Exchange segment for placing option orders. |
| `num_lots` | int | `1` | Sourced from `NUM_LOTS` env var. |
| `expiry_date` | str | `""` | Sourced from `EXPIRY_DATE` env var. Empty triggers auto-selection. |
| `strike_step` | int | `50` | NIFTY strikes are in increments of 50. ATM is the strike nearest to `round(underlying_price / 50) * 50`. |
| `rsi_period` | int | `14` | Wilder's RSI look-back period. Requires `rsi_period + 1` price ticks before producing a value. |
| `rsi_oversold` | float | `30.0` | RSI crossover threshold for buying a call. Signal fires when RSI was above this on the previous tick and is at or below it on the current tick. |
| `rsi_overbought` | float | `70.0` | RSI crossover threshold for buying a put. Signal fires when RSI was below this and is now at or above it. |
| `target_buffer` | float | `5.0` | Premium above breakeven for the OCO target leg. OCO target price = `breakeven_premium + target_buffer`. |
| `stop_buffer` | float | `5.0` | Premium below entry for the OCO stop leg. OCO stop price = `entry_premium - stop_buffer`. |
| `min_premium` | float | `10.0` | Minimum option premium filter. Entry is skipped if the ATM premium is below this. Avoids extremely cheap/illiquid contracts. |
| `max_premium` | float | `200.0` | Maximum option premium filter. Entry is skipped if premium exceeds this. Avoids entries near large gap-ups where premium is already elevated. |
| `trade_start` | str | `"09:20"` | IST time (24h) at which the strategy begins evaluating entries. Intentionally after the opening auction noise settles. |
| `trade_end` | str | `"15:00"` | IST time at which no new entries are taken. Existing positions are still managed by the OCO. |
| `squareoff_time` | str | `"15:15"` | IST time at which any remaining open position is forcibly closed with a market sell, and any pending OCO order is cancelled. |
| `poll_interval` | float | `10.0` | Seconds between strategy ticks. Each tick fetches underlying OHLC and runs RSI logic. |
| `paper_trading` | bool | `True` | Sourced from `PAPER_TRADING` env var. |
| `max_orders` | int | `10` | Maximum entries per session. Prevents runaway trading if RSI oscillates repeatedly. |
| `quantity` | int | `75` | Base lot size. Actual order quantity = `quantity * num_lots`. Set to 75 (one NIFTY lot). |

### `SMAConfig` (`strategies/strategy_base.py`)

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | `SMA_9_21_Reliance` | Display name. |
| `security_id` | str | `"2885"` | DhanHQ security ID for Reliance Industries on NSE. Change to any NSE equity security ID. |
| `exchange_segment` | str | `"NSE_EQ"` | Exchange segment. Use `NSE_EQ` for NSE equities, `BSE_EQ` for BSE. |
| `product_type` | str | `"INTRADAY"` | Order product type. `INTRADAY` for MIS, `CNC` for delivery. |
| `quantity` | int | `1` | Number of shares per order. |
| `fast_period` | int | `9` | Number of ticks for the fast (short-period) SMA. |
| `slow_period` | int | `21` | Number of ticks for the slow (long-period) SMA. Requires this many ticks before generating any signal. |
| `poll_interval` | float | `10.0` | Seconds between ticks. |
| `paper_trading` | bool | `True` | Sourced from `PAPER_TRADING` env var. |
| `max_orders` | int | `10` | Maximum orders per session. |

---

## Charge Rates (`core/charges.py`)

These constants reflect NSE F&O statutory charges as of 2025. Update them if SEBI or NSE revises the rates.

| Constant | Value | Applied To |
|---|---|---|
| `BROKERAGE_PER_LEG` | ₹20.00 | Each executed order (buy and sell separately) |
| `STT_SELL_PCT` | 0.1% | Sell-side turnover (options only) |
| `EXCHANGE_FEE_PCT` | 0.053% | Total turnover (both sides) |
| `SEBI_FEE_PER_CR` | ₹10.00 | Per crore of total turnover |
| `GST_PCT` | 18% | On brokerage + exchange fee + SEBI fee |
| `STAMP_DUTY_PCT` | 0.003% | Buy-side turnover only |

Breakeven premium formula:

```
total_charges = brokerage + stt + exchange_fee + sebi_fee + gst + stamp_duty
breakeven_premium = entry_premium + (total_charges / total_quantity)
```

---

## Instrument Master (`core/instruments.py`)

| Constant | Value | Description |
|---|---|---|
| `SCRIP_MASTER_URL` | `https://images.dhan.co/api-data/api-scrip-master.csv` | Source URL for the instrument master CSV |
| `CACHE_DIR` | `.cache/` | Local directory for cached CSV |
| `CACHE_FILE` | `.cache/scrip_master.csv` | Cached file path |
| `CACHE_TTL_HOURS` | `6` | Hours before re-downloading. A fresh download takes ~2–5 seconds. |

The parser filters rows to:
- `SEM_INSTRUMENT_NAME == "OPTIDX"` (index options)
- `SEM_TRADING_SYMBOL` starts with `"NIFTY-"` (pure NIFTY, excludes BANKNIFTY, NIFTYMID50, etc.)
- `SEM_OPTION_TYPE` in `("CE", "PE")`
- `SEM_EXPIRY_DATE >= today` (no expired contracts)

---

## DhanHQ API Rate Limits

The `DhanClient` respects these per-second limits automatically via the token-bucket `RateLimiter`:

| Category | Calls/sec | Calls/min | Calls/day |
|---|---|---|---|
| `orders` | 10 | 250 | 7,000 |
| `data` | 5 | — | 100,000 |
| `quote` (OHLC/LTP) | 1 | — | — |
| `non_trading` | 20 | — | — |

The quote limit (1/sec) is the binding constraint for the strategy's `poll_interval`. Setting `poll_interval` below 1.0 will cause the rate limiter to introduce delays automatically.
