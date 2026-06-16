# Architecture

## Why two processes

The platform's worst historical failure was self-inflicted: a single process served the dashboard *and* traded, so a slow analytics query could delay an order, and a watchdog that mistook a slow boot for a hang kill-9'd the trader in a loop. The 2026-06 rewrite split it permanently:

- **`dhan-trader`** (`apps/trader.py`) — owns the order flow. WebSocket feed, strategy runners, risk, execution, portfolio. Exports state to `run/trader_heartbeat.json` every 5 s (atomic tmp+rename).
- **`dhan-api`** (`apps/api.py`) — owns the dashboard. Serves the React build and ~30 REST endpoints on `:8765`. Reads the database and the heartbeat file. **It can never touch an order.**

The two share nothing but the DB and that file. Either can restart without the other noticing.

```
                    Dhan WebSocket             Dhan REST
                         │                         │
 ┌───────────────────────▼─────────────────────────▼───────────────┐
 │ dhan-trader                                                     │
 │                                                                 │
 │  LiveFeed ──► BarBuilder ──► bars hypertable      (1m bars)     │
 │     │              │                                            │
 │     │              └─► LiveFeed.get_ohlc_tick() reads same bar  │
 │     └──► StrategyRunner (one per security, staggered polling)   │
 │              │ on_tick(now, price, high, low) → Decision        │
 │              ▼                                                  │
 │          ORB (pure, synchronous, IO-free)                       │
 │              │ ENTER/EXIT                                       │
 │              ├── KronosGate ──► signals table (every verdict)   │
 │              ├── RiskEngine — sizing, daily-loss halt,          │
 │              │                kill-switch (single owner)        │
 │              └── Executor (Paper | Live) ──► Fill               │
 │                       │                                         │
 │                  Portfolio ──► engine_positions (DB)            │
 │                               orders / fills / trades           │
 │  RiskEngine ──► equity_curve (snapshot ~10s)                    │
 │  ApiUsageFlusher ──► api_usage table (periodic deltas)          │
 │  heartbeat ──► run/trader_heartbeat.json (5s)                   │
 └─────────────────────────────────────────────────────────────────┘
                         │ DB (read) + heartbeat (read)
 ┌───────────────────────▼─────────────────────────────────────────┐
 │ dhan-api — React dashboard + JSON API on :8765                  │
 │  build_app() registers all routes from apps/routes/             │
 │  ApiUsageFlusher ──► api_usage table (periodic deltas)          │
 └─────────────────────────────────────────────────────────────────┘
```

## The engine contract

**Strategies are pure.** `strategies/orb.py` is synchronous and IO-free: `on_tick(now, price, high, low) → Decision | None`. It holds no client, no DB handle, no notion of paper vs live. The same class is instantiated by the live runner and by the backtester — there is no "backtest version" of the strategy to drift out of sync.

**Executors absorb the mode.** One interface, three implementations:

| Executor | Fill source | Notes |
|---|---|---|
| `PaperExecutor` | Reference price ± configurable adverse slippage (bps) | Journals orders/fills exactly like live |
| `LiveExecutor` | Broker — polls `get_order_by_id` until terminal | TRADED → broker's avg price/qty; REJECTED/CANCELLED → no fill, CRITICAL log; unconfirmed after backoff → ref-price fill flagged CRITICAL for reconciliation |
| Backtest fill model | Next bar's open | No lookahead by construction |

Switching paper → live is `PAPER_TRADING=false` plus a restart. No other code path changes.

**Portfolio is durable.** Every fill upserts `engine_positions`. On boot, `reconcile_on_boot()` restores today's open positions (a restart never orphans a position); stale prior-session rows are cleared loudly. In LIVE mode, `reconcile_with_broker()` additionally treats the broker as the source of truth and adopts any mismatch with a CRITICAL log. Entry→exit linkage is maintained via an in-memory `_open_trade_id` map so exit fills close the same `trades` row that the entry opened (DATA-03).

**Risk owns the kill-switch.** `RiskEngine` sizes positions from stop distance (risk fraction of equity, capped by max notional), monitors the *portfolio* (so paper losses can trip the halt — the old design watched the empty real account), and is the only component allowed to halt. An external kill-switch file (`run/killswitch`, written by `POST /api/killswitch`) routes through the same halt path: flatten + alert within ~10 s. ADV is computed with a time-bounded 30-day window query so TimescaleDB can prune to relevant chunks rather than scanning the full `bars` hypertable (DATA-05).

## Mid-session restart safety

Two mechanisms make a restart during market hours safe:

1. **EOD square-off is unconditional** — evaluated before any strategy-state gates, so a position is always flattened before close even if the strategy lost its context.
2. **`seed_opening_ranges()`** — on a boot after 09:30 IST, the trader rebuilds each security's true opening range from REST intraday bars (paced ~1.2 s apart; the endpoint rate-limits bursts). Sides that already broke out while the process was down are marked as tried, so a breakout is never chased hours late.

## Market data path

`LiveFeed` (Dhan WebSocket, Quote packets) → `BarBuilder` (cumulative-volume deltas, 1-minute aggregation, 5 s flush) → `bars` hypertable. `BarBuilder` is the **single** tick-to-candle aggregator: `LiveFeed.get_ohlc_tick()` reads `BarBuilder.get_current()` so the strategy and the database always see identical intrabar OHLC values — there is no separate in-memory accumulator that could diverge (DATA-04). This is also what gives the Kronos gate *fresh* data — its `score_from_db()` reads the same table and reports `data_age_min` so staleness is always visible in the verdict log.

> **Hard-won detail:** the Dhan v2 subscribe message requires `SecurityId` as a **string**. An integer is accepted without error and simply never streams a packet.

## Database conventions

- TimescaleDB `2.17.2-pg16` (pinned image) on a separate EC2 (private subnet).
- **Hypertables:** `ticks`, `bars`, `positions`, `equity_curve` (4 hypertables; `ohlcv_1min` was dropped in migration 005). Compression and retention policies are applied on all four.
- **Schema head: 007.** 20 tables total. Key additions per revision: 006 migrated `signals.features_snapshot` from `json` to `jsonb` + added a GIN index; 007 added `api_usage` for cross-process API spend accounting.
- **Never run `COUNT(*)` or `ORDER BY time LIMIT 1` against `bars`** (hundreds of millions of rows — full scans / chunk decompression that hang for minutes under backfill load). Use `approximate_row_count()`, `hypertable_size()`, and chunk-catalog ranges for metadata.
- `signals.features_snapshot` is now `jsonb` (since migration 006). Legacy casts to `::jsonb` remain in the codebase but are now redundant.
- One SQLAlchemy engine per process — `db.py`'s `init_db()` is idempotent; `AsyncDBBackend` in `core/journal.py` reuses that shared pool (DATA-02).

## API layer — decomposed route modules (CODE-09)

`apps/api.py` (~320 lines) is the entry point. It owns: the `_db_query()` helper (replaces repeated `run_in_executor` boilerplate in every handler), CORS middleware, the shared-secret auth guard (`_check_auth`), the lazy read-only Dhan client, and the `build_app()` factory that registers all routes.

Handlers live in four sub-modules under `apps/routes/`:

| Module | Responsibility |
|---|---|
| `heartbeat.py` | Fast, file-only handlers: `/api/snapshot`, `/api/status`, `/api/risk`, `/api/feed`, `/api/paper/positions`, `/api/config`, `/api/mode`, `/api/killswitch`, `/kronos/live`, `/health` |
| `db.py` | DB-backed read endpoints: equity curve, signals, trades, Kronos gate/signals/screener, DB stats, **`/api/rate-limits`** |
| `market.py` | Live broker data via the read-only Dhan client: funds, positions, instrument price/search, market status, watchlist |
| `system.py` | Operational endpoints: logs, backfill status, system health, `/postback` (HMAC-verified webhook) |

## Cross-process API spend accounting (FEAT-02)

Every process (trader, backfill, api) instantiates an `ApiUsageFlusher` (`core/api_usage.py`) tagged with its process name. Periodically, the flusher reads the in-memory `RateLimiter` call counters and writes only the *delta* since the last flush into the `api_usage` table via an `ON CONFLICT DO UPDATE` upsert. Midnight roll-over is handled safely: if the counter resets below the last-flushed value, the new value is treated as the full delta for the new day rather than subtracting to a negative.

`GET /api/rate-limits` calls `query_today_totals()` which reads today's `api_usage` rows, aggregates across all processes, and returns a per-category breakdown (orders / data / quote / non_trading) with totals, Dhan daily caps, and a per-process split. No synchronous cross-process communication is needed.

## Dashboard data flow

The frontend polls `/api/snapshot` every 2 s — a file read, no DB. Slower pollers (30 s gate, 20 s equity, 30 s system health) hit cached DB endpoints. `dhan-api` installs a dedicated 16-thread executor because aiohttp's default executor serves static files too — one slow query in the shared pool used to freeze the entire page while JSON endpoints kept answering.

## Infrastructure hardening

- **Terraform remote state** in S3 + DynamoDB lock — no local `terraform.tfstate`.
- **Daily EBS snapshots** via AWS DLM (7-day retention).
- **systemd resilience:** both service units set `StartLimitBurst=5` and `OnFailure=dhan-alert@%n.service` — a crash loop fires a Telegram alert and stops rather than spinning.
- **`scripts/health_alert.py`** runs every 5 minutes via cron — monitors disk, log errors, and heartbeat staleness outside of systemd.
- **TimescaleDB image pinned** to `timescale/timescaledb:2.17.2-pg16`.

## Security

- Mutating POST endpoints (`/api/killswitch`, `/api/watchlist/refresh`, `/api/mode`) require an `X-Dashboard-Token` or `Authorization: Bearer` header matching `DASHBOARD_TOKEN`. Absent token = fail-open with a one-time WARNING log, so a misconfigured secret never locks the kill-switch.
- `/postback` verifies an HMAC-SHA256 signature when `DHAN_WEBHOOK_SECRET` is set.
- `client_id` is masked (`****<last 4>`) in the `/api/config` response.
- The `dhanhq` SDK is version-pinned in `requirements.txt`; order-placement code must not drift silently.
- CI runs with least-privilege `GITHUB_TOKEN` (contents: read only).
