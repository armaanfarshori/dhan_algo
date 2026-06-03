# API Reference

All endpoints are served by the aiohttp web server on `http://localhost:8765` (configurable via `WEBHOOK_PORT`). All responses are `application/json`. CORS is open (`*`). No authentication is required — restrict with a firewall or reverse proxy if exposing beyond localhost.

---

## Endpoints at a Glance

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe |
| `GET` | `/api/status` | Strategy state, uptime, mode, warmup progress |
| `GET` | `/api/risk` | P&L snapshot, halt state, violations |
| `GET` | `/api/signals` | Last 100 signals from both scanners, newest first |
| `GET` | `/api/funds` | Available funds from DhanHQ |
| `GET` | `/api/positions` | Open positions from DhanHQ (live mode) |
| `GET` | `/api/paper/positions` | Simulated paper positions from both scanners |
| `GET` | `/api/scalper` | Scalper-specific state (RSI, OCO, ATM) |
| `GET` | `/api/instruments` | NIFTY expiry list from instrument master |
| `GET` | `/api/instruments/search` | Search scrip master by symbol or name |
| `GET` | `/api/instruments/price` | Live LTP for a single instrument |
| `GET` | `/api/auth` | Token refresh mode and status |
| `GET` | `/api/config` | Current runtime strategy configuration |
| `GET` | `/api/market` | NSE equity / NSE F&O / MCX open/close status |
| `GET` | `/api/watchlist` | Current watchlist summary |
| `POST` | `/api/watchlist/refresh` | Re-fetch top NSE movers and update watchlist |
| `GET` | `/api/scanner` | F&O scanner summary (alias for `/api/scanner/fno`) |
| `GET` | `/api/scanner/fno` | IndexOptionsScanner per-index state |
| `GET` | `/api/scanner/equity` | MultiStockScanner per-stock state |
| `POST` | `/api/scanner/config` | Update scanner strategy, segments, or capital allocation |
| `GET` | `/api/logs` | Rolling platform log buffer |
| `GET` | `/api/feed` | WebSocket LiveFeed connection state and sample LTPs |
| `GET` | `/api/trades` | Trade journal for the current session |
| `GET` | `/api/payoff` | P&L payoff diagram for the active scalper position |
| `GET` | `/api/mode` | Current paper/live mode |
| `POST` | `/api/mode` | Toggle paper/live mode across all engines |
| `POST` | `/api/killswitch` | Activate kill-switch — halt all orders and cancel OCO |
| `POST` | `/api/strategy/switch` | Switch the primary strategy at runtime |
| `POST` | `/api/backtest/run` | Run a strategy backtest on historical data |
| `POST` | `/postback` | DhanHQ order postback webhook receiver |
| `GET` | `/api/db/stats` | TimescaleDB row counts and date ranges |
| `GET` | `/api/kronos/signals` | Latest Kronos signals from the `signals` table |
| `GET` | `/api/kronos/screener` | Top N volatile NSE equities from ATR screener |
| `GET` | `/api/backfill/status` | Live backfill progress from EC2 log |
| `GET` | `/api/hermes/status` | Hermes gateway status |

---

## GET /health

Liveness probe. Returns immediately without touching any strategy state.

**Response:**

```json
{
  "status": "ok",
  "paper": true
}
```

---

## GET /api/status

Returns current strategy state, uptime, trading mode, and warmup progress.

**Response:**

```json
{
  "mode": "PAPER",
  "client_id": "1234567890",
  "uptime_seconds": 3742,
  "strategy_name": "Options_Scalper",
  "strategy_running": true,
  "orders_placed": 2,
  "position": 75,
  "entry_price": 47.5,
  "warmup": {
    "ready": true
  }
}
```

For the SMA strategy during warmup, `warmup` includes progress fields:

```json
{
  "warmup": {
    "fast_current": 9,
    "fast_required": 9,
    "slow_current": 14,
    "slow_required": 21,
    "ready": false
  }
}
```

| Field | Type | Description |
|---|---|---|
| `mode` | string | `"PAPER"` or `"LIVE"` |
| `client_id` | string | DhanHQ client ID in use |
| `uptime_seconds` | int | Seconds since process started |
| `strategy_name` | string | Active strategy config `name` field |
| `strategy_running` | bool | Whether the strategy loop is active |
| `orders_placed` | int | Orders placed this session |
| `position` | int | Net open quantity (0 = flat) |
| `entry_price` | float | Entry price of the current position (0 if flat) |
| `warmup.ready` | bool | Whether the strategy has enough data to trade |

---

## GET /api/risk

Real-time risk snapshot. Updated every 30 seconds by the background risk monitor.

**Response — normal state:**

```json
{
  "realised_pnl": 320.50,
  "unrealised_pnl": -45.00,
  "total_pnl": 275.50,
  "open_positions": 1,
  "halted": false,
  "halt_reason": "",
  "violations": [],
  "last_checked": "2026-06-03T10:32:15.421083"
}
```

**Response — halted:**

```json
{
  "realised_pnl": -4200.00,
  "unrealised_pnl": -900.00,
  "total_pnl": -5100.00,
  "open_positions": 1,
  "halted": true,
  "halt_reason": "Daily loss ₹5,100 exceeds limit ₹5,000",
  "violations": ["Daily loss ₹5,100 exceeds limit ₹5,000"],
  "last_checked": "2026-06-03T13:12:45.001234"
}
```

| Field | Type | Description |
|---|---|---|
| `realised_pnl` | float | Realised P&L in INR from DhanHQ positions API |
| `unrealised_pnl` | float | Unrealised P&L in INR from DhanHQ positions API |
| `total_pnl` | float | `realised_pnl + unrealised_pnl` |
| `open_positions` | int | Count of positions with non-zero net quantity |
| `halted` | bool | Whether all new orders are blocked |
| `halt_reason` | string | Human-readable reason for the halt |
| `violations` | array | Active rule violations (may be multiple) |
| `last_checked` | string | ISO 8601 timestamp of last risk evaluation |

---

## GET /api/signals

Returns the last 100 signals from both the F&O scanner and the equity scanner, sorted newest-first.

**Response:**

```json
[
  {
    "action": "BUY",
    "price": 47.50,
    "reason": "CALL | RSI 28.4 | BEP ₹48.23 | T ₹53.23 S ₹42.50",
    "timestamp": "2026-06-03T10:35:02.114523",
    "source": "F&O"
  },
  {
    "action": "EXIT",
    "price": 0,
    "reason": "OCO TRADED",
    "timestamp": "2026-06-03T10:52:18.008741",
    "source": "F&O"
  }
]
```

| Field | Type | Description |
|---|---|---|
| `action` | string | `"BUY"`, `"SELL"`, `"EXIT"`, or `"HOLD"` |
| `price` | float | Price at signal time. `0` for automated OCO exits. |
| `reason` | string | Human-readable trigger description |
| `timestamp` | string | ISO 8601 timestamp |
| `source` | string | `"F&O"` (IndexOptionsScanner) or `"EQ"` (MultiStockScanner) |

---

## GET /api/funds

Proxies DhanHQ `/fundlimit`. Returns live available balance.

**Response — success:**

```json
{
  "ok": true,
  "data": {
    "dhanClientId": "1234567890",
    "availabelBalance": 48250.00,
    "sodLimit": 50000.00,
    "collateralAmount": 0.0,
    "utilizedAmount": 1750.00,
    "withdrawableBalance": 48250.00
  }
}
```

**Response — error:**

```json
{
  "ok": false,
  "error": "[DHAN-1002] Invalid access token"
}
```

HTTP 503 on error.

---

## GET /api/positions

Proxies DhanHQ `/positions`. Returns all open intraday and overnight positions. In paper mode, this will reflect the broker's view (usually empty). Use `/api/paper/positions` for simulated paper positions.

**Response — success:**

```json
{
  "ok": true,
  "data": [
    {
      "dhanClientId": "1234567890",
      "tradingSymbol": "NIFTY-24500CE-08May2026",
      "securityId": "98765",
      "positionType": "LONG",
      "exchangeSegment": "NSE_FNO",
      "buyAvg": 47.50,
      "buyQty": 75,
      "netQty": 75,
      "unrealisedProfit": -112.50,
      "realisedProfit": 0.0
    }
  ]
}
```

HTTP 503 on error.

---

## GET /api/paper/positions

Returns simulated paper positions aggregated from both scanners. Only meaningful in paper mode.

**Response — paper mode:**

```json
{
  "ok": true,
  "count": 2,
  "data": [
    {
      "engine": "F&O",
      "symbol": "NIFTY 24500 CE",
      "index": "NIFTY",
      "option_type": "CE",
      "strike": 24500.0,
      "entry_premium": 47.50,
      "lot_size": 75,
      "expiry": "2026-06-05",
      "bep": 48.23
    },
    {
      "engine": "EQ",
      "symbol": "RELIANCE",
      "name": "Reliance Industries",
      "segment": "NSE_EQ",
      "entry_price": 2850.00,
      "current_price": 2865.00,
      "qty": 1,
      "unrealized_pnl": 15.00,
      "change_pct": 0.53
    }
  ]
}
```

**Response — live mode:**

```json
{
  "ok": true,
  "count": 0,
  "data": [],
  "note": "Live mode — see /api/positions for real positions"
}
```

---

## GET /api/scalper

Returns the internal state of the Options Scalper strategy. Returns 404 if the active strategy is not the scalper.

**Response — in position:**

```json
{
  "state": "IN_POSITION",
  "oco_state": "IN_POSITION",
  "entry_premium": 47.50,
  "breakeven_premium": 48.23,
  "last_rsi": 28.4,
  "oco_order_id": "112345678901234",
  "option_sid": "98765",
  "active_expiry": "2026-06-05",
  "current_atm": {
    "strike": 24500.0,
    "expiry": "2026-06-05",
    "CE_sid": "98765",
    "PE_sid": "98766"
  },
  "orders_placed": 1,
  "master_loaded": true
}
```

**Response — flat:**

```json
{
  "state": "FLAT",
  "oco_state": "FLAT",
  "entry_premium": 0.0,
  "breakeven_premium": 0.0,
  "last_rsi": 52.1,
  "oco_order_id": null,
  "option_sid": null,
  "active_expiry": "2026-06-05",
  "current_atm": null,
  "orders_placed": 0,
  "master_loaded": true
}
```

| Field | Type | Description |
|---|---|---|
| `state` | string | `"FLAT"`, `"ENTERING"`, or `"IN_POSITION"` |
| `entry_premium` | float | Option premium at fill (0 if flat) |
| `breakeven_premium` | float | Minimum exit premium after all charges (0 if flat) |
| `last_rsi` | float | Most recent RSI-14 on NIFTY index |
| `oco_order_id` | string or null | DhanHQ order ID of the active Forever OCO |
| `option_sid` | string or null | DhanHQ security ID of the held option |
| `active_expiry` | string | Targeted expiry date (`YYYY-MM-DD`) |
| `current_atm` | object or null | Strike and CE/PE security IDs resolved at last entry |
| `master_loaded` | bool | Whether the instrument master has finished loading |

---

## GET /api/instruments

Returns NIFTY expiry lists from the in-memory instrument master.

**Response — loaded:**

```json
{
  "loaded": true,
  "expiries": ["2026-06-05", "2026-06-12", "2026-06-26", "2026-07-31"],
  "weekly": ["2026-06-05", "2026-06-12"],
  "monthly": ["2026-06-26", "2026-07-31"],
  "nearest": "2026-06-05",
  "active": "2026-06-05"
}
```

Returns 404 when a non-scalper strategy is active.

---

## GET /api/instruments/search

Searches the Dhan scrip master for instruments matching a query string.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `q` | string | required | Search term (min 2 characters). Matches symbol and name. |
| `segment` | string | `NSE_EQ` | Exchange segment filter. |

**Response:**

```json
{
  "ok": true,
  "results": [
    {
      "security_id": "2885",
      "trading_symbol": "RELIANCE",
      "name": "Reliance Industries Ltd",
      "segment": "NSE_EQ"
    }
  ]
}
```

HTTP 400 if `q` is less than 2 characters.

---

## GET /api/instruments/price

Returns the live LTP (last traded price) for a single instrument.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `security_id` | string | required | DhanHQ security ID |
| `segment` | string | `NSE_EQ` | Exchange segment (`NSE_EQ`, `NSE_FNO`, `MCX`) |

**Response — success:**

```json
{
  "ok": true,
  "security_id": "2885",
  "price": 2865.50,
  "segment": "NSE_EQ"
}
```

HTTP 400 if `security_id` is missing. HTTP 503 on API error.

---

## GET /api/auth

Returns the token management mode and status.

**Response — auto refresh enabled (DHAN_PIN + DHAN_TOTP_SECRET set):**

```json
{
  "mode": "auto",
  "token_expires_at": "2026-06-04T08:30:00",
  "minutes_remaining": 720,
  "last_refresh": "2026-06-03T08:30:00"
}
```

**Response — manual mode:**

```json
{
  "mode": "manual",
  "note": "Set DHAN_PIN + DHAN_TOTP_SECRET to enable auto-refresh"
}
```

---

## GET /api/config

Returns the current runtime strategy configuration as last set by startup or `POST /api/strategy/switch`.

**Response:**

```json
{
  "strategy": "scalper",
  "segment": "NSE_FNO",
  "security_id": "13",
  "quantity": 75,
  "num_lots": 1
}
```

---

## GET /api/market

Returns open/close status for NSE equity, NSE F&O, and MCX commodity markets. Computed from IST time — no API call.

**Response:**

```json
{
  "nse_equity": "OPEN",
  "nse_fno": "OPEN",
  "mcx": "CLOSED",
  "ist_time": "10:45:23",
  "weekday": "Tuesday",
  "is_weekend": false
}
```

`nse_equity` and `nse_fno` may return `"PRE"` between 09:00–09:14 IST on weekdays.

---

## GET /api/watchlist

Returns the current watchlist used by the equity scanner.

**Response:**

```json
{
  "ok": true,
  "count": 15,
  "stocks": ["RELIANCE", "HDFCBANK", "TCS", "INFY", "..."]
}
```

---

## POST /api/watchlist/refresh

Re-fetches the top NSE movers from DhanHQ and updates the watchlist in memory.

**Request body:** none required.

**Response:**

```json
{
  "ok": true,
  "count": 15,
  "stocks": ["RELIANCE", "HDFCBANK", "TCS", "..."]
}
```

HTTP 503 if the watchlist is not initialised.

---

## GET /api/scanner

Alias for `/api/scanner/fno`. Returns the F&O scanner summary.

---

## GET /api/scanner/fno

Returns the state of `IndexOptionsScanner` for each monitored index.

**Response:**

```json
{
  "ok": true,
  "indices": {
    "NIFTY": {
      "in_position": true,
      "option_type": "CE",
      "strike": 24500,
      "entry_premium": 47.50,
      "rsi": 28.4,
      "expiry": "2026-06-05"
    },
    "BANKNIFTY": {
      "in_position": false,
      "rsi": 54.2
    }
  }
}
```

---

## GET /api/scanner/equity

Returns the state of `MultiStockScanner` for each tracked stock.

**Response:**

```json
{
  "ok": true,
  "count": 15,
  "positions": 1,
  "stocks": {
    "RELIANCE": {
      "in_position": true,
      "entry_price": 2850.00,
      "current_price": 2865.00,
      "strategy": "momentum_breakout"
    }
  }
}
```

---

## POST /api/scanner/config

Updates scanner configuration at runtime without restart.

**Request body:**

```json
{
  "strategy_key": "momentum_breakout",
  "segments": ["NSE_FNO", "NSE_EQ"],
  "capital_pct": 0.35,
  "max_positions": 10,
  "hedge_fno": false
}
```

All fields are optional. Unset fields are not changed.

| Field | Type | Description |
|---|---|---|
| `strategy_key` | string | Equity scanner strategy. Valid: `sma_crossover`, `rsi_scalper`, `momentum_breakout`, `mean_reversion`, `bollinger`, `vwap_reversion` |
| `segments` | array | Active segments. `NSE_FNO` and/or `BSE_FNO` for F&O; `NSE_EQ` for equity. Removing a segment pauses that scanner. |
| `capital_pct` | float | Fraction of paper balance per position |
| `max_positions` | int | Maximum concurrent positions for equity scanner |
| `hedge_fno` | bool | Whether to hedge equity positions with F&O |

**Response:**

```json
{
  "ok": true,
  "strategy_key": "momentum_breakout",
  "segments": ["NSE_FNO", "NSE_EQ"],
  "capital_pct": 0.35
}
```

---

## GET /api/logs

Returns recent platform log lines from the rolling in-memory buffer.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | integer | `50` | Number of recent log lines to return |

**Response:**

```json
{
  "ok": true,
  "logs": [
    {"level": "INFO", "message": "F&O Scanner: NIFTY · BANKNIFTY ...", "ts": "10:00:01"},
    {"level": "WARNING", "message": "Mode→LIVE: clearing paper position NIFTY", "ts": "10:00:02"}
  ]
}
```

---

## GET /api/feed

Returns the WebSocket LiveFeed connection state and sample LTPs.

**Response:**

```json
{
  "ok": true,
  "connected": true,
  "subscribed": 21,
  "sample_ltps": {
    "13": 24532.50,
    "25": 54120.00,
    "2885": 2865.50
  }
}
```

| Field | Type | Description |
|---|---|---|
| `connected` | bool | Whether the WebSocket is currently connected to DhanHQ |
| `subscribed` | int | Total instruments subscribed |
| `sample_ltps` | object | LTP for up to 6 subscribed security IDs |

---

## GET /api/trades

Returns the trade journal for the current session from the in-memory trade logger.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | integer | `200` | Maximum trades to return |
| `engine` | string | _(all)_ | Filter by engine: `F&O` or `EQ` |

**Response:**

```json
{
  "ok": true,
  "count": 3,
  "summary": {
    "total_trades": 3,
    "total_pnl": 320.50,
    "win_rate": 0.67
  },
  "trades": [
    {
      "engine": "F&O",
      "symbol": "NIFTY 24500 CE",
      "action": "BUY",
      "price": 47.50,
      "qty": 75,
      "mode": "PAPER",
      "strategy": "index_options",
      "ts": "2026-06-03T10:35:02"
    }
  ]
}
```

---

## GET /api/payoff

Returns a P&L payoff diagram for the active scalper position (or a what-if scenario if flat).

**Response — in position:**

```json
{
  "ok": true,
  "mode": "live",
  "entry": 47.50,
  "breakeven": 48.23,
  "target": 53.23,
  "stop": 42.50,
  "points": [
    {"premium": 35.0, "pnl": -937.50},
    {"premium": 50.0, "pnl": 187.50},
    {"premium": 65.0, "pnl": 1312.50}
  ]
}
```

`mode` is `"live"` when in position, `"whatif"` when flat (uses `max_premium / 2` as a hypothetical entry).

Returns 404 if the active strategy is not the scalper.

---

## GET /api/mode

Returns the current paper/live trading mode.

**Response:**

```json
{
  "ok": true,
  "paper": true,
  "mode": "PAPER"
}
```

---

## POST /api/mode

Toggles paper/live mode across all running engines (F&O scanner, equity scanner, and primary strategy).

**Request body:**

```json
{
  "paper": true
}
```

When switching to live (`paper: false`), all in-memory paper positions are cleared and a warning is logged for each cleared position. This prevents ghost positions from appearing in live mode.

**Response:**

```json
{
  "ok": true,
  "paper": true,
  "mode": "PAPER"
}
```

---

## POST /api/killswitch

Activates the kill-switch immediately. Halts all new orders, stops the primary strategy task, and attempts to cancel any active OCO order (in live mode).

**Request body:** none required.

**Response:**

```json
{
  "ok": true,
  "halted": true,
  "oco_cancelled": 1,
  "message": "Kill switch activated"
}
```

`oco_cancelled` is 0 in paper mode or when no OCO was active. This action is irreversible without a process restart — the risk manager remains halted.

---

## POST /api/strategy/switch

Stops the current primary strategy and starts a new one. Both scanners continue running independently.

**Request body:**

```json
{
  "strategy": "sma_crossover",
  "segment": "NSE_EQ",
  "security_id": "2885",
  "quantity": 1,
  "num_lots": 1
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `strategy` | string | `"scalper"` | Strategy key. Valid: `scalper`, `sma_crossover`, `rsi_scalper`, `momentum_breakout`, `mean_reversion`, `bollinger`, `vwap_reversion`, `short_straddle` |
| `segment` | string | `"NSE_FNO"` | Exchange segment |
| `security_id` | string | `"13"` | Target security ID |
| `quantity` | int | `75` | Shares or lot size per order |
| `num_lots` | int | `1` | Number of lots (scalper only) |

**Response — success:**

```json
{
  "ok": true,
  "strategy": "sma_crossover",
  "message": "Switched to SMA_9_21_2885"
}
```

**Response — unknown strategy:**

```json
{
  "ok": false,
  "error": "Unknown strategy: unknown_strat"
}
```

HTTP 400 on unknown strategy.

---

## POST /api/backtest/run

Runs a strategy backtest on historical data fetched from DhanHQ.

**Request body:**

```json
{
  "strategy": "sma_crossover",
  "security_id": "2885",
  "segment": "NSE_EQ",
  "from_date": "2026-01-01",
  "to_date": "2026-05-01",
  "quantity": 1,
  "fast_period": 9,
  "slow_period": 21,
  "interval": "D"
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `strategy` | string | `"sma_crossover"` | Strategy to backtest. Valid: `sma_crossover`, `rsi_scalper`, `momentum_breakout`, `mean_reversion`, `bollinger`, `vwap_reversion` |
| `security_id` | string | `"2885"` | DhanHQ security ID |
| `segment` | string | `"NSE_EQ"` | Exchange segment |
| `from_date` | string | `"2026-01-01"` | Start date `YYYY-MM-DD` |
| `to_date` | string | `"2026-05-01"` | End date `YYYY-MM-DD` |
| `quantity` | int | `1` | Shares per trade |
| `fast_period` | int | `9` | Fast SMA period (SMA only) |
| `slow_period` | int | `21` | Slow SMA period (SMA only) |
| `interval` | string | `"D"` | `"D"` for daily, `"1"` for 1-minute intraday |

**Response — success:**

```json
{
  "ok": true,
  "bars": 86,
  "strategy": "sma_crossover",
  "symbol": "2885",
  "summary": {
    "total_pnl": 4250.00,
    "total_trades": 8,
    "win_rate": 0.625,
    "sharpe_ratio": 1.42,
    "max_drawdown_pct": 3.2
  },
  "equity_curve": [0, 250, 1200, 4250],
  "trades": [
    {
      "entry_date": "2026-01-15",
      "exit_date": "2026-01-22",
      "action": "BUY",
      "entry_price": 2780.0,
      "exit_price": 2865.0,
      "pnl": 85.0
    }
  ]
}
```

**Response — no data:**

```json
{
  "ok": false,
  "error": "No historical data returned — check security_id, segment and date range."
}
```

HTTP 400 when no data is returned. HTTP 500 on unexpected errors.

---

## POST /postback

Receives DhanHQ order update postback events. Configure this URL in the DhanHQ developer portal as your webhook endpoint.

**Request body (sent by DhanHQ):**

```json
{
  "dhanClientId": "1234567890",
  "orderId": "112345678901234",
  "orderStatus": "TRADED",
  "transactionType": "SELL",
  "tradingSymbol": "NIFTY-24500CE-05Jun2026",
  "securityId": "98765",
  "quantity": 75,
  "averagePrice": 53.23
}
```

**Response:**

```json
{
  "ack": "ok"
}
```

HTTP 400 on parse error. Currently logs the event. Future: will update position state without polling.

---

## GET /api/db/stats

Returns row counts and date ranges from TimescaleDB. Requires `DB_HOST` to be reachable.

**Response:**

```json
{
  "ok": true,
  "bars": [
    {"timeframe": "1d", "rows": 4949, "earliest": "2021-06-01", "latest": "2026-06-01"},
    {"timeframe": "1m", "rows": 4800000, "earliest": "2024-06-01", "latest": "2026-06-03"}
  ],
  "instruments": {
    "NSE_EQ": 22646,
    "NSE_FNO": 180000,
    "BSE_EQ": 5000
  },
  "signals": 0,
  "trades": 0
}
```

**Response — DB unreachable:**

```json
{
  "ok": false,
  "error": "could not connect to server: Connection refused",
  "bars": [],
  "instruments": {}
}
```

---

## GET /api/kronos/signals

Returns the most recent Kronos signals recorded in the `signals` table.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | integer | `50` | Number of signals to return |

**Response:**

```json
{
  "ok": true,
  "signals": [
    {
      "security_id": "2885",
      "side": "BUY",
      "score": 0.002341,
      "confidence": 0.76,
      "strategy": "ORB",
      "ts": "2026-06-03 09:35:00+05:30",
      "features": {"or_high": 2870, "or_low": 2840, "or_range": 30}
    }
  ]
}
```

Returns `{"ok": false, "signals": []}` if the DB is unreachable.

---

## GET /api/kronos/screener

Returns the top N NSE equities by ATR (most volatile) from the `bars` table. Used for watchlist construction.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `n` | integer | `20` | Number of candidates to return |

**Response:**

```json
{
  "ok": true,
  "candidates": [
    {"security_id": "2885", "symbol": "RELIANCE", "atr": 45.2, "atr_pct": 1.58},
    {"security_id": "1333", "symbol": "HDFCBANK", "atr": 38.7, "atr_pct": 2.21}
  ],
  "count": 20
}
```

Returns `{"ok": false, "candidates": []}` if the DB is unreachable.

---

## GET /api/backfill/status

Returns the live backfill progress by reading the last 15 lines of `/tmp/backfill.log` on the agent EC2.

**Response:**

```json
{
  "ok": true,
  "running": true,
  "log_tail": [
    "10:15:23  INFO  dhan.backfill — ═══ security_id=2885 ═══",
    "10:15:24  INFO  dhan.backfill —   [1m] 2885  2021-06-01 → 2021-08-29",
    "10:15:25  INFO  dhan.backfill —   Received 25600 candles"
  ]
}
```

`running` is `true` if a `backfill.py` process is detected via `pgrep`. `log_tail` is empty if the log file does not exist.

---

## GET /api/hermes/status

Returns the Hermes gateway status by shelling out to the `hermes` CLI.

**Response — online:**

```json
{
  "ok": true,
  "running": true,
  "raw": "hermes gateway: active (running) since ...",
  "model": "meta-llama/llama-3.3-70b-instruct",
  "provider": "openrouter"
}
```

**Response — offline or CLI not found:**

```json
{
  "ok": false,
  "running": false,
  "error": "hermes: command not found"
}
```
