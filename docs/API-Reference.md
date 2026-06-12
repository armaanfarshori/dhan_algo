# API Reference

All endpoints are served by `dhan-api` (aiohttp) on `http://localhost:8765` (`WEBHOOK_PORT`). Responses are JSON. **No authentication exists yet (M6)** — the server binds for tunnel/localhost access; never expose it publicly.

The API process is read-only with respect to trading: it reads the database and the trader's heartbeat file. The only writes it can perform are the kill-switch flag and watchlist cache refresh.

## Core / dashboard loop

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | React dashboard (`index.html`, served with `Cache-Control: no-cache`) |
| `/health` | GET | Liveness of the api process |
| `/api/snapshot` | GET | **The fast poll (2 s).** Trader heartbeat (mode, gate, strategies with OR/position/ticker, portfolio, risk, feed, bar-builder stats) + config limits. File read only — no DB |
| `/api/status` | GET | Engine status summary |
| `/api/config` | GET | Non-secret config values |

### `/api/snapshot` response sketch

```json
{
  "alive": true,
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
  "limits": { "max_daily_loss": 5000, "max_orders_per_session": 4 }
}
```

## Trading data

| Endpoint | Method | Description |
|---|---|---|
| `/api/trades` | GET | Trade log with instrument tickers + today's summary (`closed_today`, `pnl_today`, `wins_today`). `?limit=` |
| `/api/signals` | GET | **Today's** executions only (kept consistent with Today P&L) |
| `/api/equity` | GET | Intraday P&L curve — 1-min buckets from the `equity_curve` hypertable (risk engine snapshots ~10 s). Cached 15 s |
| `/api/positions` | GET | Broker positions (live) |
| `/api/paper/positions` | GET | Simulated positions |
| `/api/funds` | GET | Broker fund limits |

## Kronos

| Endpoint | Method | Description |
|---|---|---|
| `/api/kronos/gate` | GET | Today's gate verdicts (direction, verdict, confidence, `data_age_min`, shadow flag) + cached calibration summary |
| `/api/kronos/signals` | GET | Recent Kronos scores |
| `/api/kronos/screener` | GET | Screener-ranked securities |
| `/api/kronos/live` | GET | Live scanner output (when enabled) |

## Control (deliberately limited)

| Endpoint | Method | Description |
|---|---|---|
| `/api/mode` | GET | Current mode (paper/live) |
| `/api/mode` | POST | **Returns 409.** Mode changes require editing `.env` + restarting `dhan-trader` — live must never be one unauthenticated request away |
| `/api/killswitch` | POST | Writes the kill-switch file; the trader's risk loop halts + flattens within ~10 s |

## System / ops

| Endpoint | Method | Description |
|---|---|---|
| `/api/system/health` | GET | Cron jobs, Telegram alert status, trader error count |
| `/api/db/stats` | GET | DB size, approximate row counts, bar time-span — **catalog-only queries** (never `COUNT(*)` on hypertables) |
| `/api/backfill/status` | GET | Backfill checkpoint progress |
| `/api/logs` | GET | Recent trader log lines, ISO timestamps. `?limit=` |
| `/api/feed` | GET | WebSocket feed state |
| `/api/market` | GET | Market open/closed |
| `/api/watchlist` | GET / `…/refresh` POST | Cached watchlist view / refresh |
| `/api/instruments/search` | GET | `?q=` ticker search |
| `/api/instruments/price` | GET | Spot quote for one security |
| `/postback` | POST | Dhan order postback receiver |

## Conventions

- Times are stored UTC; the dashboard renders IST.
- Heavy queries are cached server-side (15–300 s) and run in a dedicated thread pool so file serving never starves.
- Instrument names: endpoints join the `instruments` table so consumers get `ticker` alongside `security_id`; the heartbeat carries a name map resolved at trader boot.
