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
 │     │                                                           │
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
 │                                orders / fills / trades          │
 │  RiskEngine ──► equity_curve (snapshot ~10s)                    │
 │  heartbeat ──► run/trader_heartbeat.json (5s)                   │
 └─────────────────────────────────────────────────────────────────┘
                         │ DB (read) + heartbeat (read)
 ┌───────────────────────▼─────────────────────────────────────────┐
 │ dhan-api — React dashboard + JSON API on :8765                  │
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

**Portfolio is durable.** Every fill upserts `engine_positions`. On boot, `reconcile_on_boot()` restores today's open positions (a restart never orphans a position); stale prior-session rows are cleared loudly. In LIVE mode, `reconcile_with_broker()` additionally treats the broker as the source of truth and adopts any mismatch with a CRITICAL log.

**Risk owns the kill-switch.** `RiskEngine` sizes positions from stop distance (risk fraction of equity, capped by max notional), monitors the *portfolio* (so paper losses can trip the halt — the old design watched the empty real account), and is the only component allowed to halt. An external kill-switch file (`run/killswitch`, written by `POST /api/killswitch`) routes through the same halt path: flatten + alert within ~10 s.

## Mid-session restart safety

Two mechanisms make a restart during market hours safe:

1. **EOD square-off is unconditional** — evaluated before any strategy-state gates, so a position is always flattened before close even if the strategy lost its context.
2. **`seed_opening_ranges()`** — on a boot after 09:30 IST, the trader rebuilds each security's true opening range from REST intraday bars (paced ~1.2 s apart; the endpoint rate-limits bursts). Sides that already broke out while the process was down are marked as tried, so a breakout is never chased hours late.

## Market data path

`LiveFeed` (Dhan WebSocket, Quote packets) → `BarBuilder` (cumulative-volume deltas, 1-minute aggregation, 5 s flush) → `bars` hypertable. This is what gives the Kronos gate *fresh* data — its `score_from_db()` reads the same table and reports `data_age_min` so staleness is always visible in the verdict log.

> **Hard-won detail:** the Dhan v2 subscribe message requires `SecurityId` as a **string**. An integer is accepted without error and simply never streams a packet.

## Database conventions

- TimescaleDB on a separate EC2 (private subnet). Hypertables: `bars`, `ticks`, `orders`, `fills`, `equity_curve`; compression + retention policies applied.
- **Never run `COUNT(*)` or `ORDER BY time LIMIT 1` against `bars`** (hundreds of millions of rows — full scans / chunk decompression that hang for minutes under backfill load). Use `approximate_row_count()`, `hypertable_size()`, and chunk-catalog ranges for metadata.
- `signals.features_snapshot` is `json` (not `jsonb`) — cast `::jsonb` before using `?` or `->>`.

## Dashboard data flow

The frontend polls `/api/snapshot` every 2 s — a file read, no DB. Slower pollers (30 s gate, 20 s equity, 30 s system health) hit cached DB endpoints. `dhan-api` installs a dedicated 16-thread executor because aiohttp's default executor serves static files too — one slow query in the shared pool used to freeze the entire page while JSON endpoints kept answering.
