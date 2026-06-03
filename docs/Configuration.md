# Configuration Reference

All runtime behaviour is controlled through environment variables loaded from `.env` by `python-dotenv` at process startup. The typed `Config` dataclass in `config.py` is the single source of truth — all modules call `get_config()` rather than `os.getenv()` directly. Strategy internals that are not exposed as env vars are configured through dataclass fields in code.

---

## Environment Variables

### Dhan Credentials

| Variable | Type | Default | Description |
|---|---|---|---|
| `DHAN_CLIENT_ID` | string | `mock` | DhanHQ client ID. Sent as the `client-id` header in every API request and included in order payloads as `dhanClientId`. Required for any real API call. |
| `DHAN_ACCESS_TOKEN` | string | `mock` | JWT access token from the DhanHQ developer portal. Passed as the `access-token` header. Expires approximately 24 hours after generation. |
| `DHAN_PIN` | string | _(empty)_ | 4-digit trading PIN. When set alongside `DHAN_TOTP_SECRET`, enables automatic token refresh via `core/auth.py`. Without it, the static `DHAN_ACCESS_TOKEN` is used and backfill runs will fail after token expiry. |
| `DHAN_TOTP_SECRET` | string | _(empty)_ | Base32 TOTP secret from DhanHQ 2FA setup. Used with `DHAN_PIN` for TOTP-based login. The auth manager refreshes the token 30 minutes before expiry and writes the new token back to `.env` for restart continuity. |

### Trading Mode

| Variable | Type | Default | Description |
|---|---|---|---|
| `PAPER_TRADING` | boolean string | `true` | When `true`, all order placements are simulated in memory. No API order endpoints are called. Set to `false` only after backtesting passes and the agent Elastic IP is whitelisted in Dhan DevPortal. |
| `STRATEGY` | string | `scalper` | Selects the startup strategy for the single-strategy path. Valid: `scalper`, `sma`. In scanner mode (default `main.py` behaviour), both `IndexOptionsScanner` and `MultiStockScanner` run regardless of this setting. |

### Risk Controls

| Variable | Type | Default | Description |
|---|---|---|---|
| `MAX_DAILY_LOSS` | float | `5000` | Maximum allowed daily loss in INR (realised + unrealised). When total P&L drops below `-MAX_DAILY_LOSS`, the risk manager halts all new orders and fires halt callbacks. The halt persists until manually cleared. |
| `CAPITAL` | float | `100000` | Total capital allocated in INR. Used to compute `max_loss_per_trade` in paper mode: `CAPITAL * 0.20`. |
| `RISK_PER_TRADE` | float | `0.01` | Fraction of capital risked per trade (1% default). Currently informational — `max_loss_per_trade` in `RiskConfig` is computed from `CAPITAL` not this field. |

### Options Scalper

| Variable | Type | Default | Description |
|---|---|---|---|
| `EXPIRY_DATE` | string | _(empty)_ | Target option expiry in `YYYY-MM-DD` format. When blank, the instrument master auto-selects the nearest upcoming weekly expiry. |
| `NUM_LOTS` | integer | `1` | Number of NIFTY option lots per entry. One NIFTY lot = 75 units. Actual order quantity = `NUM_LOTS * 75`. |

### ORB Strategy

| Variable | Type | Default | Description |
|---|---|---|---|
| `ORB_RANGE_MINUTES` | integer | `15` | Opening range duration in minutes. 15 (9:15–9:29) and 30 (9:15–9:44) are standard. |

### Watchlist

| Variable | Type | Default | Description |
|---|---|---|---|
| `WATCHLIST_SECURITY_IDS` | comma-separated string | `2885,1333,1594,11536` | DhanHQ security IDs for the default watchlist. Defaults to RELIANCE, HDFCBANK, INFY, TCS. Used by `backfill.py` when no `--ids` flag is given, and by `WatchlistManager`. |
| `WATCHLIST_EXCHANGE_SEGMENT` | string | `NSE_EQ` | Exchange segment for watchlist securities. |

### Database (TimescaleDB)

| Variable | Type | Default | Description |
|---|---|---|---|
| `DB_HOST` | string | `localhost` | TimescaleDB host. On agent EC2 this is the DB server's private IP (`10.0.1.155`). Locally it is `localhost` via docker-compose. |
| `DB_PORT` | integer | `5432` | PostgreSQL port. |
| `DB_NAME` | string | `dhan_trading` | Database name. |
| `DB_USER` | string | `trader` | Database user. |
| `DB_PASSWORD` | string | `trader123` | Database password. On EC2 this is pulled from SSM (`/dhan/db_password`). |

The `db_url` property on `Config` assembles the SQLAlchemy connection string with URL-encoded credentials.

### Hermes / LLM

| Variable | Type | Default | Description |
|---|---|---|---|
| `GROQ_API_KEY` | string | _(empty)_ | Groq API key from console.groq.com. Used by Hermes for LLM inference. NOT used by the trading platform itself. |
| `GROQ_MODEL` | string | `groq/llama-3.3-70b-versatile` | Groq model identifier. In practice the agent uses `meta-llama/llama-3.3-70b-instruct` via OpenRouter. |

### Kronos Foundation Model

| Variable | Type | Default | Description |
|---|---|---|---|
| `KRONOS_TOKENIZER` | string | `NeoQuasar/Kronos-Tokenizer-base` | HuggingFace model ID for the Kronos tokenizer. |
| `KRONOS_MODEL` | string | `NeoQuasar/Kronos-small` | HuggingFace model ID. Use `Kronos-mini` for CPU-only machines without enough RAM for `Kronos-small`. |
| `KRONOS_LOOKBACK` | integer | `400` | Number of 1-minute candles fed to the model. Must be ≤ 512 (model max context). |
| `KRONOS_PRED_LEN` | integer | `30` | Number of candles to forecast ahead. |
| `KRONOS_SAMPLES` | integer | `5` | Sampling paths per prediction. Higher values give a better confidence estimate at the cost of compute time. |
| `KRONOS_THRESH` | float | `0.001` | Minimum absolute forecasted return to emit a BUY or SELL signal. Forecasts below this threshold produce HOLD. Default 0.1%. |

### Web Server

| Variable | Type | Default | Description |
|---|---|---|---|
| `WEBHOOK_PORT` | integer | `8765` | TCP port for the aiohttp web server. Serves the React dashboard, all `/api/*` endpoints, and the DhanHQ postback webhook. Binds to `0.0.0.0`. On EC2 the security group restricts external access. |
| `SCANNER_MODE` | boolean string | `false` | When `true`, activates multi-scanner mode (both F&O and equity scanners). This is already the default in `main.py` regardless of this flag — the flag is kept for backward compatibility. |
| `PAPER_BALANCE` | float | `500000` | Simulated balance for paper mode capital sizing. `max_loss_per_trade = PAPER_BALANCE * 0.20`. |

---

## Strategy Config Objects

Strategy parameters not exposed as env vars are set in dataclass instances in code. To change them, edit the relevant config instantiation in `main.py` or the strategy file.

### `RiskConfig` (`core/risk.py`)

| Field | Type | Default in main.py | Description |
|---|---|---|---|
| `max_daily_loss` | float | From `MAX_DAILY_LOSS` env | INR daily loss floor. |
| `max_open_positions` | int | `10` | Hard cap on concurrent open positions. `check_order()` blocks new orders above this. |
| `max_loss_per_trade` | float | `PAPER_BALANCE * 0.20` (paper) / `25000` (live) | Maximum capital exposure per trade (quantity × price). Orders exceeding this are rejected before submission. |
| `check_interval_seconds` | int | `30` | How often the background risk monitor evaluates P&L and positions. |
| `kill_switch` | bool | `False` | When `True`, all new orders are blocked. Set via `risk.activate_kill_switch()` or `POST /api/killswitch`. |

### `OptionsScalperConfig` (`strategies/options_scalper.py`)

| Field | Type | Default | Description |
|---|---|---|---|
| `underlying_security_id` | str | `"13"` | Dhan security ID for NIFTY 50 index. Used for OHLC polling. |
| `underlying_exchange` | str | `"IDX_I"` | Exchange segment for the index feed. |
| `option_exchange` | str | `"NSE_FNO"` | Exchange segment for placing option orders. |
| `num_lots` | int | From `NUM_LOTS` env | Lots per entry (1 lot = 75 units). |
| `expiry_date` | str | From `EXPIRY_DATE` env | Empty = auto nearest weekly. |
| `strike_step` | int | `50` | NIFTY strike increment. ATM = `round(price / 50) * 50`. |
| `rsi_period` | int | `14` | RSI look-back period. Requires `rsi_period + 1` ticks before first value. |
| `rsi_oversold` | float | `30.0` | RSI threshold for call entry (crossover below). |
| `rsi_overbought` | float | `70.0` | RSI threshold for put entry (crossover above). |
| `target_buffer` | float | `5.0` | Premium above breakeven for OCO target leg (₹/unit). |
| `stop_buffer` | float | `5.0` | Premium below entry for OCO stop leg (₹/unit). |
| `min_premium` | float | `10.0` | Skip entry if ATM premium is below this. |
| `max_premium` | float | `200.0` | Skip entry if ATM premium is above this. |
| `trade_start` | str | `"09:20"` | IST time — no entries before this. |
| `trade_end` | str | `"15:00"` | IST time — no new entries after this. |
| `squareoff_time` | str | `"15:15"` | IST time — force-close any open position, cancel OCO. |
| `poll_interval` | float | `10.0` | Seconds between ticks. |
| `max_orders` | int | `10` | Maximum entries per session. |
| `quantity` | int | `75` | Base lot size. Actual qty = `quantity * num_lots`. |

### `SMAConfig` (`strategies/strategy_base.py`)

| Field | Type | Default | Description |
|---|---|---|---|
| `security_id` | str | `"2885"` | Dhan security ID (default: Reliance Industries NSE). |
| `exchange_segment` | str | `"NSE_EQ"` | Exchange segment. |
| `product_type` | str | `"INTRADAY"` | `INTRADAY` for MIS, `CNC` for delivery. |
| `quantity` | int | `1` | Shares per order. |
| `fast_period` | int | `9` | Fast SMA tick count. |
| `slow_period` | int | `21` | Slow SMA tick count. Strategy produces no signal until `slow_period` ticks are received. |
| `poll_interval` | float | `10.0` | Seconds between ticks. |
| `max_orders` | int | `10` | Max orders per session. |

### `ORBConfig` (`strategies/strategy_orb.py`)

| Field | Type | Default | Description |
|---|---|---|---|
| `orb_minutes` | int | `15` | Opening range duration (9:15 to 9:30 for 15-min). |
| `sl_buffer_pct` | float | `0.002` | Stop-loss buffer as a fraction of OR range (0.2%). Added above OR_HIGH for shorts, subtracted below OR_LOW for longs. |
| `target_multiplier` | float | `1.5` | Target = entry ± (1.5 × OR range). |
| `squareoff_before_close_min` | int | `15` | Exit open position N minutes before 15:30 IST close. |
| `min_range_pct` | float | `0.003` | Skip trades when OR range is less than 0.3% of price (too narrow). |
| `use_kronos` | bool | `True` | Require Kronos model agreement before entering. |
| `kronos_min_confidence` | float | `0.4` | Minimum Kronos confidence score to allow entry. Below this, the trade is skipped (but Kronos errors are fail-open). |

---

## NSE F&O Charge Rates (`core/charges.py`)

These constants reflect NSE F&O statutory charges as of 2025. Update if SEBI or NSE revises rates.

| Constant | Value | Applied To |
|---|---|---|
| `BROKERAGE_PER_LEG` | ₹20.00 | Each executed order leg |
| `STT_SELL_PCT` | 0.1% | Sell-side turnover (options) |
| `EXCHANGE_FEE_PCT` | 0.053% | Total turnover both sides |
| `SEBI_FEE_PER_CR` | ₹10.00 | Per crore of total turnover |
| `GST_PCT` | 18% | On brokerage + exchange fee + SEBI fee |
| `STAMP_DUTY_PCT` | 0.003% | Buy-side turnover only |

Breakeven formula:

```
total_charges = brokerage(2×₹20) + stt(sell×0.001) + exchange_fee(total×0.00053)
              + sebi_fee(total/1e7×10) + gst(sub_total×0.18) + stamp(buy×0.00003)
breakeven_premium = entry_premium + (total_charges / total_quantity)
```

---

## Instrument Master Constants (`core/instruments.py`)

| Constant | Value | Description |
|---|---|---|
| `SCRIP_MASTER_URL` | `https://images.dhan.co/api-data/api-scrip-master.csv` | Source for the scrip master CSV |
| `CACHE_DIR` | `.cache/` | Local cache directory |
| `CACHE_TTL_HOURS` | `6` | Hours before re-downloading |

The live instrument master (used by IndexOptionsScanner and OptionsScalperStrategy) filters to NIFTY index options only. The `instrument_sync.py` module writes the full ~224K scrip set into the `instruments` table.

---

## DhanHQ API Rate Limits

Enforced by the token-bucket `RateLimiter` in `core/client.py`. These are per-second limits.

| Category | Calls/sec | Calls/min | Notes |
|---|---|---|---|
| `data` (historical) | 5 | — | Binding constraint for backfill speed |
| `orders` | 10 | 250 | For live/paper order placement |
| `quote` (OHLC/LTP) | 1 | — | Limits strategy `poll_interval` minimum |
| `non_trading` | 20 | — | Instrument master, fund queries |

At 5 req/s, Nifty 50 backfill (~1,050 calls) takes approximately 3.5 minutes. All NSE equities (~84,000 calls) takes approximately 4.7 hours.
