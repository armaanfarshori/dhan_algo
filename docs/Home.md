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
| Dashboard process | `dhan-api` (aiohttp + React) — reads DB + heartbeat only, port 8765 |
| Strategy | ORB on a dynamic ATR%-screened watchlist (price/volume floors, scrip-master validated) |
| AI gate | Kronos-small zero-shot (24.7M params), shadow mode, fail-open |
| Storage | TimescaleDB — bars, ticks, orders, fills, trades, positions, equity curve, gate verdicts |
| Backtester | Event-driven replay of the same strategy class, next-bar fills, full cost model |
| Alerts | Plain Telegram bot API (no LLM) — halts, fills, EOD summary, watchdogs |
| Infra | Terraform: VPC, 2× EC2 (ARM Graviton), S3, SSM; ~$56/month |
| Tests | 71 pytest cases, GitHub Actions on every push |

## Roadmap state

**Done:** infrastructure, schema (5 hypertables), two-process engine with Paper/Live executors, DB-persisted portfolio with boot/broker reconciliation, live WebSocket → 1-min bars pipeline, shadow gate + calibration loop, event-driven backtester, dashboard, ops automation.

**In progress:** full NSE_EQ historical backfill (~9,470 instruments, 5 years of 1-minute bars); gate calibration data collection (needs ≥30 fresh-data outcomes before any re-arm decision).

**Next:** clean training replica (corporate-action-adjusted, liquid-only) → 2-year three-way backtest (ORB alone vs +Kronos zero-shot vs +fine-tuned) → go/no-go on tiny live capital.

**Post-backtest research queue:** scheduled-event calendar filter (RBI/budget/expiry/earnings days — defensive), Kronos small-vs-base shadow A/B. News/NLP awareness is deliberately deprioritized: Kronos consumes only candles, and historical news cannot be backtested honestly at retail data budgets.

## Documentation map

| Page | Read it for |
|---|---|
| [Architecture](Architecture.md) | Process model, engine internals, design rationale |
| [Setup-Guide](Setup-Guide.md) | Local dev and AWS deployment |
| [Configuration](Configuration.md) | Every config field, defaults, safety notes |
| [Strategies](Strategies.md) | ORB rules, Kronos gate, risk model, calibration |
| [Backtesting](Backtesting.md) | Backtester design, cost model, CLI |
| [API-Reference](API-Reference.md) | REST endpoints and response shapes |
| [Operations-Runbook](Operations-Runbook.md) | Deploy, monitor, recover |
