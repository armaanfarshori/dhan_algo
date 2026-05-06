# DhanHQ Algo Platform — Wiki Home

Welcome to the internal documentation for the DhanHQ Algo Platform. This wiki covers everything from first-run setup to deep-dive strategy mechanics.

---

## What This Platform Does

DhanHQ Algo Platform is a private, self-hosted algorithmic trading system for NSE equity and NIFTY F&O markets. It connects to DhanHQ's v2 REST API, executes rule-based strategies, enforces real-time risk controls, and exposes a React dashboard for monitoring.

The entire runtime is a single Python process (`main.py`) built on `asyncio`. Three concurrent tasks run in parallel:

- **Strategy engine** — polls market data, generates signals, places orders
- **Risk monitor** — evaluates P&L and position counts every 30 seconds
- **Web server** — serves the React dashboard and JSON API on port 8765

Paper trading is on by default. No real orders are placed until `PAPER_TRADING=false` is explicitly set.

---

## Feature Highlights

### Options Scalper (default strategy)
- Tracks RSI-14 on the NIFTY 50 index in real time
- Buys the ATM call when RSI crosses below 30; buys the ATM put when RSI crosses above 70
- Security IDs are never hardcoded — they are discovered at entry time by querying the in-memory instrument master built from Dhan's scrip CSV
- After every fill, a Forever OCO order is placed immediately: target leg at `breakeven + 5`, stop leg at `entry - 5`
- All statutory charges (brokerage, STT, exchange fee, SEBI fee, GST, stamp duty) are calculated before setting OCO prices so the target is always above breakeven
- Forced squareoff at 15:15 IST. No overnight positions.

### SMA 9/21 Crossover
- Tracks a 9-period fast SMA and a 21-period slow SMA on any NSE equity
- Buys on golden cross (fast crosses above slow), sells/exits on death cross
- Requires 21 ticks to warm up before generating signals — warmup progress is visible in `/api/status`

### Risk Manager
- Checks total P&L (realised + unrealised) every 30 seconds
- Halts all new orders if daily loss exceeds `MAX_DAILY_LOSS`
- Hard position limit: no new orders if open positions exceed `max_open_positions`
- Per-trade capital exposure check before each order
- Kill switch available at runtime via `risk.activate_kill_switch()`
- All halts fire registered async callbacks (for alerting, logging, or UI updates)

### Instrument Master
- Downloads `https://images.dhan.co/api-data/api-scrip-master.csv` on startup
- Caches the file locally under `.cache/` for 6 hours — mid-session refresh if stale
- Filters to NIFTY index options only (`OPTIDX` instrument type, `NIFTY-` prefix)
- Indexes ~4,120 active contracts by `(expiry, strike, option_type)` for O(1) ATM lookup
- Exposes weekly/monthly expiry lists and a nearest-expiry selector

### Backtesting Engine
- Replay any `BaseStrategy` subclass against a list of OHLCV bars
- MockClient and MockRiskManager stub out all API calls
- Computes: Sharpe ratio (annualised), max drawdown %, win rate %, total P&L, equity curve
- No live API calls required — safe to run offline

### React Dashboard
- Built with React 18 and Vite, served from `dashboard/dist/` by the aiohttp server
- Live-polls all `/api/*` endpoints
- Shows: strategy state, current RSI, breakeven premium, active expiry, signal feed, risk violations, funds, positions

---

## Wiki Navigation

| Page | What's Inside |
|---|---|
| [Setup Guide](Setup-Guide.md) | Ubuntu install, venv, `.env`, running for the first time |
| [Configuration](Configuration.md) | Every environment variable, every strategy config field |
| [Strategies](Strategies.md) | Signal logic, state machines, breakeven math derivation |
| [API Reference](API-Reference.md) | All 8 endpoints with full request/response JSON |

---

## Quick Links

- **DhanHQ API docs**: https://dhanhq.co/docs/v2/
- **Scrip master CSV**: https://images.dhan.co/api-data/api-scrip-master.csv
- **DhanHQ developer portal** (token generation): https://developer.dhan.co/
- **Dashboard** (when running): http://localhost:8765

---

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.14 |
| Async runtime | asyncio |
| HTTP client | aiohttp 3.9+ |
| Web framework | aiohttp.web |
| Frontend | React 18 + Vite |
| Broker API | DhanHQ v2 REST |
| Data format | CSV (scrip master), JSON (API) |
| Config | python-dotenv |
| Testing | pytest + pytest-asyncio + aioresponses |
