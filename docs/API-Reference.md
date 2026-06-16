# API Reference

All endpoints are served by `dhan-api` (aiohttp) on `http://localhost:8765` (`WEBHOOK_PORT`). Responses are JSON. **No authentication exists yet (M6)** — the server binds for tunnel/localhost access; never expose it publicly.

The API process is read-only with respect to trading: it reads the database and the trader's heartbeat file. The only writes it can perform are the kill-switch flag and watchlist cache refresh.

## Auth — POST control endpoints

Two mutating POST endpoints (`/api/killswitch` and `/api/watchlist/refresh`) require a shared-secret header when `DASHBOARD_TOKEN` is set (SEC-04):

```
X-Dashboard-Token: <token>
```

Alternatively `Authorization: Bearer <token>`. When `DASHBOARD_TOKEN` is empty the endpoints are unprotected (fail-open — a misconfigured secret must never lock the operator out of the kill-switch); a one-time WARNING is logged. A mismatch returns HTTP 401 `{"ok": false, "error": "unauthorized"}`.

## Core / dashboard loop

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | React dashboard (`index.html`, served with `Cache-Control: no-cache`) |
| `/health` | GET | Liveness of the api process |
| `/api/snapshot` | GET | **The fast poll (2 s).** Trader heartbeat (mode, gate, strategies with OR/position/ticker, portfolio, risk, feed, bar-builder stats) + config limits. File read only — no DB |
| `/api/status` | GET | Engine status summary (masked client ID, strategy state, heartbeat age, Kronos gate mode) |
| `/api/config` | GET | Non-secret runtime config: strategy name, Kronos gate mode, exchange segment, watchlist security IDs |
| `/api/risk` | GET | Live risk state from heartbeat: daily P&L, halt status, violations, position totals. Returns 503 if trader heartbeat has no risk block |

### `/api/status` response

```json
{
  "mode": "PAPER",
  "client_id": "****1234",
  "uptime_seconds": 3600,
  "trader_alive": true,
  "strategy_name": "ORB_13951",
  "strategy_running": true,
  "orders_placed": 2,
  "position": 0,
  "entry_price": 0.0,
  "warmup": { "ready": true },
  "kronos_gate": "shadow",
  "strategies": [ { "security_id": "13951", "running": true, "entries_today": 2 } ],
  "note": null
}
```

`client_id` is **always masked**: `****` + last 4 digits. `note` is non-null (with a stale heartbeat warning) when `trader_alive` is false.

### `/api/config` response

```json
{
  "strategy": "orb+kronos",
  "kronos_gate": "shadow",
  "segment": "NSE_EQ",
  "watchlist": ["13951", "1333"],
  "mode": "PAPER"
}
```

### `/api/risk` response

```json
{
  "halted": false,
  "violations": [],
  "total_pnl": -120.50,
  "daily_loss_budget": 5000,
  "open_value": 0,
  "trader_alive": true
}
```

Returns `{"ok": false, "trader_alive": false}` with HTTP 503 when the heartbeat contains no risk block (trader not yet started or stale).

### `/api/snapshot` response sketch

```json
{
  "ok": true,
  "alive": true,
  "ts": "2026-06-16T04:30:00+00:00",
  "trader": {
    "ts": "…", "pid": 1234, "mode": "PAPER", "uptime_seconds": 3600,
    "kronos_gate": "shadow",
    "watchlist": ["13951", "…"],
    "names": {"13951": "PARACABLES"},
    "strategies": [{ "security_id": "13951", "ticker": "PARACABLES",
                     "or_high": 64.1, "or_low": 62.5, "or_locked": true,
                     "position": 0, "entry_price": 0, "last_price": 63.2,
                     "entries_today": 0, "running": true }],
    "portfolio": { "mode": "PAPER", "open_positions": [],
                   "realized_pnl": 0, "unrealized_pnl": 0, "total_pnl": 0 },
    "risk": { "halted": false, "violations": [], "total_pnl": 0 },
    "feed": { "connected": true, "subscribed": 5 },
    "bars": { "tracking": 5, "bars_written": 120, "last_flush": "…" }
  },
  "limits": {
    "max_daily_loss": 10000,
    "paper_balance": 500000,
    "max_orders_per_session": 4,
    "max_open_positions": 10
  }
}
```

`limits.max_daily_loss` is computed as `equity × max_daily_loss_pct` (× `live_risk_scale` in live mode). All four keys are always present.

## Trading data

| Endpoint | Method | Description |
|---|---|---|
| `/api/trades` | GET | Trade log with instrument tickers + today's summary (`closed_today`, `pnl_today`, `wins_today`). `?limit=N` (1–500, default 200) |
| `/api/signals` | GET | **Today's** executions only (entries + exits, newest first; max 100) |
| `/api/equity` | GET | Intraday P&L curve — 1-min buckets from the `equity_curve` hypertable (risk engine snapshots ~10 s). Cached 15 s |
| `/api/positions` | GET | Broker positions (live) via read-only Dhan client. Returns `{"ok": true, "data": [...]}` or 503 on Dhan API error |
| `/api/paper/positions` | GET | Paper engine open positions from heartbeat. In LIVE mode returns empty with a note pointing to `/api/positions` |
| `/api/funds` | GET | Broker fund limits from live Dhan API (`get_funds`). Returns `{"ok": true, "data": {...}}` or 503 on Dhan API error |
| `/api/feed` | GET | WebSocket feed state and bar-builder stats from heartbeat |

### `/api/trades` response

```json
{
  "ok": true,
  "count": 2,
  "summary": { "closed_today": 2, "pnl_today": 42.50, "wins_today": 1 },
  "trades": [
    {
      "symbol": "PARACABLES", "security_id": "13951", "action": "BUY", "qty": 10,
      "entry_ts": "2026-06-16 09:30:00", "entry_price": 63.50,
      "exit_ts": "2026-06-16 10:00:00", "exit_price": 64.00,
      "pnl": 42.50, "strategy": "orb", "status": "CLOSED"
    }
  ]
}
```

### `/api/signals` response

Array of signal objects, newest first (max 100):

```json
[
  { "action": "BUY", "price": 63.50, "reason": "orb entry x10",
    "timestamp": "2026-06-16 09:30:00", "source": "orb PARACABLES" },
  { "action": "EXIT", "price": 64.00, "reason": "orb exit  PnL ₹+42.50",
    "timestamp": "2026-06-16 10:00:00", "source": "orb PARACABLES" }
]
```

### `/api/funds` response

```json
{
  "ok": true,
  "data": {
    "availabelBalance": 100000.00,
    "sodLimit": 100000.00,
    "collateralAmount": 0,
    "receiveableAmount": 0,
    "utilizedAmount": 0,
    "blockedPayoutAmount": 0,
    "withdrawableBalance": 100000.00
  }
}
```

Field names come directly from the Dhan API response; shape may vary by account type.

### `/api/feed` response

```json
{
  "ok": true,
  "connected": true,
  "subscribed": 5,
  "bars": {
    "tracking": 5,
    "bars_written": 120,
    "last_flush": "2026-06-16T09:30:05+00:00"
  }
}
```

`ok` mirrors `trader_alive`. `subscribed` is the count of security IDs currently streaming. `bars` is the bar-builder state from the heartbeat.

## Kronos

| Endpoint | Method | Description |
|---|---|---|
| `/api/kronos/gate` | GET | Today's gate verdicts (direction, verdict, confidence, `data_age_min`, shadow flag) + cached calibration summary. Cached 20 s |
| `/api/kronos/signals` | GET | Today's Kronos scores from the `signals` table, newest first. `?limit=N` (1–500, default 50) |
| `/api/kronos/screener` | GET | Screener-ranked securities (ATR% + price/volume floors). `?n=N` (1–100, default 20). Cached 300 s |
| `/api/kronos/live` | GET | Live scanner state from heartbeat (`kronos_scanner` key). Returns `{"ok": false, "error": "scanner not running"}` when no state is present |

### `/api/kronos/gate` response

```json
{
  "ok": true,
  "decisions": [
    {
      "security_id": "13951", "ticker": "PARACABLES",
      "model_side": "BUY", "confidence": 0.72,
      "ts": "2026-06-16 09:30:05",
      "requested_direction": "BUY", "verdict": "ALLOW",
      "shadow": true, "data_age_min": 2.1, "stale": false
    }
  ],
  "calibration": {
    "recommendation": "keep shadow",
    "fresh_n": 12,
    "fresh_accuracy": 0.50,
    "recommended_min_confidence": 0.4,
    "gate_value": null
  }
}
```

### `/api/kronos/signals` response

```json
{
  "ok": true,
  "signals": [
    {
      "security_id": "13951", "side": "BUY", "score": 0.003,
      "confidence": 0.72, "strategy": "orb_gate",
      "ts": "2026-06-16 09:30:05",
      "features": { "…": "…" },
      "ticker": "PARACABLES", "name": "Para Cables Ltd"
    }
  ]
}
```

## Rate limits

| Endpoint | Method | Description |
|---|---|---|
| `/api/rate-limits` | GET | Account-wide per-category daily API spend across all processes vs. per-day caps |

### `/api/rate-limits` response

```json
{
  "ok": true,
  "date": "2026-06-16",
  "categories": {
    "orders":      { "total": 6,   "per_day": 290,  "by_process": { "trader": 6 } },
    "data":        { "total": 840, "per_day": null,  "by_process": { "backfill": 840 } },
    "quote":       { "total": 120, "per_day": 1000,  "by_process": { "trader": 120 } },
    "non_trading": { "total": 5,   "per_day": null,  "by_process": { "api": 5 } }
  }
}
```

All four categories (`orders`, `data`, `quote`, `non_trading`) are always present even when zero. `per_day` is `null` for uncapped categories. `by_process` shows each process (`trader`, `backfill`, `api`) that made calls today; absent = zero.

## Control (deliberately limited)

| Endpoint | Method | Description |
|---|---|---|
| `/api/mode` | GET | Current mode (paper/live) from heartbeat |
| `/api/mode` | POST | **Returns 409.** Mode changes require editing `.env` + restarting `dhan-trader` — live must never be one unauthenticated request away |
| `/api/killswitch` | POST | Writes the kill-switch flag file; the trader risk loop halts + flattens within ~10 s. **Requires auth header when `DASHBOARD_TOKEN` is set** |
| `/api/watchlist/refresh` | POST | Forces an immediate watchlist refresh from the screener. **Requires auth header when `DASHBOARD_TOKEN` is set** |

### `/api/mode` GET response

```json
{ "ok": true, "paper": true, "mode": "PAPER" }
```

### `/api/killswitch` POST response

```json
{ "ok": true, "halted": true, "message": "Kill switch flag set — trader halts within ~10s" }
```

## System / ops

| Endpoint | Method | Description |
|---|---|---|
| `/api/system/health` | GET | Cron schedule, Telegram alert status, trader error count for today. Cached 30 s |
| `/api/db/stats` | GET | DB size, approximate row counts, bar time-span, alembic version, hypertable compression stats — **catalog-only queries** (never `COUNT(*)` on hypertables). Cached 60 s |
| `/api/backfill/status` | GET | Backfill checkpoint progress + process running flag + log tail |
| `/api/logs` | GET | Recent `trader.log` lines with ISO timestamps. `?limit=N` (1–500, default 50) |
| `/api/market` | GET | Market open/closed status (NSE equity, NSE F&O, MCX) derived from IST clock |
| `/api/watchlist` | GET | Cached watchlist view (securities + screener metadata) |
| `/api/instruments/search` | GET | `?q=<query>&segment=NSE_EQ` — ticker/name search against the instrument master (min 2 chars) |
| `/api/instruments/price` | GET | LTP for one security: `?security_id=<id>&segment=NSE_EQ` |
| `/postback` | POST | Dhan order postback webhook receiver |

### `/api/system/health` response

```json
{
  "ok": true,
  "telegram_configured": true,
  "crons": [
    { "schedule": "*/15 * * * *", "job": "backfill watchdog" },
    { "schedule": "15 11 * * 1-5", "job": "calibration" },
    { "schedule": "30 11 * * 1-5", "job": "EOD summary" }
  ],
  "trader_errors_today": 0,
  "hermes": "retired 2026-06-11 — plain Telegram alerts via core/notify.py"
}
```

### `/api/db/stats` response

```json
{
  "ok": true, "up": true, "ping_ms": 1.2,
  "db_size": "18 GB", "alembic": "005",
  "hypertables": [
    { "name": "bars", "size": "16 GB", "approx_rows": 300000000,
      "chunks_compressed": 240, "chunks_total": 250 }
  ],
  "bars_span": { "earliest": "2024-01-02", "latest": "2026-06-16" },
  "instruments": { "NSE_EQ": 1850 },
  "signals": 42, "trades": 6
}
```

### `/api/backfill/status` response

```json
{
  "ok": true, "running": true,
  "checkpoint": { "index": 427, "total": 1850, "pct": 23.1, "symbol": "HDFC" },
  "log_tail": [ "2026-06-16 09:15:00 — [427/1850] HDFC …" ]
}
```

`checkpoint` is empty `{}` if the checkpoint file does not exist. `running` is true when `backfill.py` is found in the process table.

### `/postback` — Dhan webhook

Receives Dhan order postback events. When `DHAN_WEBHOOK_SECRET` is set (SEC-09), the handler reads the raw request body, computes `HMAC-SHA256(secret, body)` and constant-time-compares against the `X-Dhan-Signature` header. A mismatch returns HTTP 401. When the secret is unset, verification is skipped (back-compat).

On success returns `{"ack": "ok"}`.

## Conventions

- Times are stored UTC; the dashboard renders IST.
- Heavy queries are cached server-side (15–300 s) and run in a dedicated 16-thread pool so file serving never starves behind DB queries.
- Instrument names: DB-backed endpoints join the `instruments` table so consumers get `ticker` alongside `security_id`; the heartbeat carries a name map resolved at trader boot.
- CORS headers (`Access-Control-Allow-Origin: *`) are set on every response by middleware.
