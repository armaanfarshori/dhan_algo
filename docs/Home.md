# DhanAIBot — Overview

DhanAIBot is a self-hosted intraday trading platform for NSE equities, built on the DhanHQ v2 API. It pairs a rule-based Opening Range Breakout (ORB) strategy with Kronos, an OHLCV foundation model used as an AI signal gate, on top of TimescaleDB.

The operating principle is **evidence before exposure**:

- Paper trading is the hard default; live requires a deliberate config change *and* an explicit enable flag *and* a restart.
- The AI gate runs in **shadow mode** — it scores and records every decision but blocks nothing — until a calibration loop proves on fresh data that its ALLOW/BLOCK verdicts add value.
- Live trading is gated on a 2-year backtest with a realistic Indian intraday cost stack (brokerage, STT, exchange charges, stamp duty, GST, slippage).

## System at a glance

| Layer | Implementation |
|---|---|
| Broker | DhanHQ v2 (REST + WebSocket) — production only, no sandbox exists |
| Trading process | `dhan-trader` (asyncio, systemd) — feed, strategies, risk, execution |
| Dashboard process | `dhan-api` (aiohttp + React) — decomposed route modules, reads DB + heartbeat only, port 8765 |
| Strategy | ORB on a dynamic ATR%-screened watchlist (₹50 price floor, 50K volume floor, scrip-master validated) |
| AI gate | Kronos-small zero-shot (24.7M params), shadow mode, fail-open |
| Storage | TimescaleDB 2.17.2-pg16 — 20 tables, 4 hypertables (bars, ticks, positions, equity_curve), schema head 007 |
| API spend | `core/api_usage.py` + `api_usage` table — cross-process per-category accounting; `GET /api/rate-limits` shows today's totals |
| Backtester | Event-driven replay of the same strategy class, next-bar fills, full cost model |
| Alerts | Plain Telegram bot API (no LLM) — halts, fills, EOD summary, watchdogs |
| Infra | Terraform (remote S3+DynamoDB state): VPC, 2× EC2 (ARM Graviton), EBS snapshots (DLM), S3, SSM; ~$56/month |
| Tests | 195 pytest cases, CI on Py3.11 × x86 + ARM, ruff lint, coverage gate |

## Current status (2026-06-16)

The platform has completed a full SDLC-remediation pass this sprint. It is running in paper mode on AWS with the Kronos gate in shadow. Key hardening shipped:

- **API layer decomposed (CODE-09):** route handlers moved to `apps/routes/{heartbeat,db,market,system}.py`, registered by `build_app()` in `apps/api.py`. A shared `_db_query()` helper replaced repeated `run_in_executor` boilerplate.
- **Cross-process API spend (FEAT-02):** `core/api_usage.py` + migration 007 (`api_usage` table) — trader, backfill, and api each flush per-category call deltas; `GET /api/rate-limits` shows account-wide totals and per-process breakdown vs Dhan caps.
- **Schema head 007:** migration 006 converted `signals.features_snapshot` to `jsonb` + GIN index; migration 007 added the `api_usage` table (20 tables total).
- **Data integrity fixes:** one SQLAlchemy engine per process (DATA-02); entry→exit linked via `_open_trade_id` (DATA-03); `BarBuilder` is the single tick→candle aggregator — `LiveFeed.get_ohlc_tick()` reads it so strategy and DB see identical intrabar OHLC (DATA-04); ADV query uses a 30-day time-bounded `WHERE` clause (DATA-05).
- **Infrastructure:** Terraform remote state (S3 + DynamoDB lock), daily EBS snapshots (DLM), `StartLimitBurst` + `OnFailure` systemd alert unit, `scripts/health_alert.py` cron, pinned TimescaleDB image.
- **Security:** `DASHBOARD_TOKEN` shared-secret auth on mutating POSTs, masked `client_id` in `/api/config`, HMAC-verified `/postback`, pinned `dhanhq` SDK, least-privilege CI `GITHUB_TOKEN`.
- **Test suite:** 195 tests; CI matrix covers x86 + ARM64, Py3.11, ruff lint, and a coverage floor.

Backfill is approximately 67% complete (NSE_EQ, checkpointed). First full paper session completed 2026-06-12: 6 trades, clean exits, approximately flat after costs.

## Roadmap state

**Done:** infrastructure, schema (4 hypertables, head 007), two-process engine with Paper/Live executors, DB-persisted portfolio with boot/broker reconciliation, live WebSocket → 1-min bars pipeline, shadow gate + calibration loop, event-driven backtester, dashboard (decomposed API layer), SDLC hardening (infra, security, data integrity, test coverage).

**In progress:** full NSE_EQ historical backfill (~9,470 instruments, 5 years of 1-minute bars, ~67% complete); gate calibration data collection (needs ≥30 fresh-data outcomes before any re-arm decision).

**Next:** clean training replica (corporate-action-adjusted, liquid-only, `scripts/build_clean_db.py`) → 2-year three-way backtest (ORB alone vs +Kronos zero-shot vs +fine-tuned) → go/no-go on tiny live capital (M8).

**Post-backtest research queue:** scheduled-event calendar filter (RBI/budget/expiry/earnings days — defensive), Kronos small-vs-base shadow A/B, Kronos fine-tune on clean Parquet (spot g4dn.xlarge, date-split train/val/test, checkpoint to S3). News/NLP awareness is deliberately deprioritized: Kronos consumes only candles, and historical news cannot be backtested honestly at retail data budgets.

## Documentation map

| Page | Read it for |
|---|---|
| [Architecture](Architecture.md) | Process model, engine internals, API layer decomposition, FEAT-02 spend accounting, design rationale |
| [Setup-Guide](Setup-Guide.md) | Local dev and AWS deployment |
| [Configuration](Configuration.md) | Every config field, defaults, safety notes |
| [Strategies](Strategies.md) | ORB rules, Kronos gate, risk model, calibration |
| [Backtesting](Backtesting.md) | Backtester design, cost model, CLI |
| [API-Reference](API-Reference.md) | REST endpoints and response shapes |
| [Operations-Runbook](Operations-Runbook.md) | Deploy, monitor, recover |
