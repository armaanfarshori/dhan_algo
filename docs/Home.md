# DhanAIBot — System Overview

DhanAIBot is a self-hosted algorithmic trading platform for NSE equities and index options. It runs on two AWS EC2 instances in ap-south-1 (Mumbai) and is operated exclusively via SSH and a Telegram bot — the Mac is an editor only.

---

## What It Does

The platform runs a single Python process (`main.py`) backed by asyncio. At startup it launches six concurrent tasks:

| Task | Purpose |
|---|---|
| `IndexOptionsScanner` | RSI-14 on 6 NSE/BSE indices (NIFTY, BANKNIFTY, SENSEX, FINNIFTY, NIFTYNXT50, MIDCPNIFTY); buys ATM options, places OCO immediately |
| `MultiStockScanner` | SMA crossover + momentum on top 15 NSE equity movers from watchlist |
| `LiveFeed` | WebSocket connection to DhanHQ; streams ticks for all subscribed instruments |
| `RiskManager` | Evaluates P&L and position counts every 30 seconds; activates kill-switch if daily loss cap is breached |
| `auth_manager` | Background loop; refreshes Dhan access token via PIN + TOTP 30 minutes before expiry |
| `aiohttp` web server | Serves React dashboard on port 8765; exposes 30+ `/api/*` JSON endpoints |

Paper trading is on by default (`PAPER_TRADING=true`). No real orders are placed until explicitly changed.

---

## Current Live State (2026-06-03)

| Component | State |
|---|---|
| AWS infrastructure | Running — 2x t4g EC2 in ap-south-1 |
| Agent EC2 | t4g.micro, Elastic IP 13.206.66.237 (whitelisted for orders) |
| DB EC2 | t4g.medium, private IP 10.0.1.155 |
| TimescaleDB | 19 tables, 5 hypertables, ~4.8M 1-min bars across 16 securities |
| Instruments | 204K records loaded; 22,646 NSE equity backfill running (~5 days) |
| Hermes gateway | Online — @farshoribot (Telegram), meta-llama/llama-3.3-70b-instruct via OpenRouter |
| Schema version | `003_auth_tables` (head) |
| Trading mode | PAPER — never live until backtesting passes |

---

## Component Overview

### IndexOptionsScanner (`strategies/index_options.py`)

Monitors all tradeable NSE/BSE index underlyings simultaneously via a single bulk LTP call. For each index it maintains an independent RSI-14 state machine. On oversold crossover it buys the ATM call; on overbought crossover it buys the ATM put. The ATM security ID is resolved at entry time from the in-memory instrument master (downloaded from Dhan's scrip CSV). After every fill, a DhanHQ Forever OCO order is placed: target at breakeven + buffer, stop at entry − buffer. All positions are force-closed at 15:15 IST. Max one position per index; up to six concurrent.

### MultiStockScanner (`strategies/scanner.py`)

Polls the top 15 NSE equity movers from the watchlist on a 60-second interval. Each stock runs an independent instance of the configured strategy (default: momentum breakout on paper). The 60-second candle is intentional — it produces cleaner signals than 10-second REST polling.

### ORBStrategy (`strategies/strategy_orb.py`)

Opening Range Breakout with a Kronos pre-filter. During the first `orb_minutes` (default 15) of the session it builds the OR_HIGH and OR_LOW. After the range locks, a price close above OR_HIGH is a long candidate; below OR_LOW is a short candidate. If Kronos is available, the model must agree with the direction AND return confidence >= 0.4 before the trade is placed. On model error, the strategy is fail-open (trade proceeds). One trade per direction per security per session.

### Kronos Signal Engine (`core/kronos_signal.py`)

Lazy-loads `NeoQuasar/Kronos-small` from HuggingFace on first call. Takes the last `KRONOS_LOOKBACK` 1-minute bars from TimescaleDB (`ohlcv_1min` table), runs the foundation model with sampling, and returns `{side, score, confidence, forecasted_return}`. Batch scoring is available via `score_batch()` for screening multiple securities concurrently.

### Hermes + Groq

Hermes (NousResearch agent framework) runs as a separate process on the agent EC2 and connects to Telegram via @farshoribot. It uses `meta-llama/llama-3.3-70b-instruct` via OpenRouter (configured as `groq/llama-3.3-70b-versatile` in `.env` — the OpenRouter endpoint). Seven trading skills live in `hermes_skills/dhan/`: backfill check, daily pre-market, execution loop, health report, kill switch, Kronos forecast, and trade reflection.

### TimescaleDB

19 tables on the DB EC2. Five are TimescaleDB hypertables (partitioned by time): `bars`, `ticks`, `positions`, `equity_curve`, and `ohlcv_1min`. The `bars` table is the primary OHLCV store; `ohlcv_1min` is a legacy mirror written by `backfill.py` for backward compatibility with `kronos_signal.py`.

### RiskManager (`core/risk.py`)

Runs as a background asyncio task every 30 seconds. Fetches live positions from DhanHQ, sums realised + unrealised P&L, and compares against `MAX_DAILY_LOSS`. On breach it sets `kill_switch=True` and fires registered halt callbacks. `check_order()` is called before every order placement; it rejects orders when halted, when open positions exceed the cap, or when the per-trade capital exposure exceeds `max_loss_per_trade`.

### DhanClient (`core/client.py`)

Async aiohttp client for the DhanHQ v2 REST API. Implements a token-bucket rate limiter (5 req/s for data, 10 req/s for orders). All requests include `client-id` and `access-token` headers. Auto-refreshes the token from `DhanAuthManager` when the manager is wired.

---

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 (Graviton ARM64 on EC2) |
| Async runtime | asyncio |
| HTTP client | aiohttp 3.9+ |
| Web framework | aiohttp.web |
| Frontend | React 18 + Vite |
| Broker API | DhanHQ v2 REST (no sandbox) |
| Database | TimescaleDB on PostgreSQL 16 |
| ORM / migrations | SQLAlchemy + Alembic |
| AI model | NeoQuasar/Kronos-small (HuggingFace) |
| LLM for Hermes | Groq llama-3.3-70b-versatile via OpenRouter |
| Agent framework | NousResearch Hermes |
| Secrets | AWS SSM Parameter Store |
| Infra | Terraform, ap-south-1, t4g Graviton |
| Config | python-dotenv + typed `Config` dataclass |

---

## Wiki Navigation

| Page | Contents |
|---|---|
| [Setup Guide](Setup-Guide.md) | Local dev, AWS deploy, Hermes setup, troubleshooting |
| [Configuration](Configuration.md) | Every env var, strategy config field, DB settings |
| [Strategies](Strategies.md) | ORB, Options Scalper, SMA Crossover — signal logic and state machines |
| [API Reference](API-Reference.md) | All REST endpoints with request/response JSON |

---

## External Resources

- **DhanHQ API docs**: https://dhanhq.co/docs/v2/
- **DhanHQ developer portal** (token generation): https://developer.dhan.co/
- **Dhan scrip master CSV**: https://images.dhan.co/api-data/api-scrip-master.csv
- **Kronos model**: https://huggingface.co/NeoQuasar/Kronos-small
- **Dashboard** (when running): http://localhost:8765 (or via SSH tunnel)
- **Telegram bot**: @farshoribot
