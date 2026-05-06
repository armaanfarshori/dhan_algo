# DhanHQ Algo Platform

> Async algorithmic trading platform for the Indian equity and F&O markets, built on DhanHQ v2 REST API.

[![Python](https://img.shields.io/badge/Python-3.14-blue?logo=python&logoColor=white)](https://www.python.org/)
[![aiohttp](https://img.shields.io/badge/aiohttp-3.9%2B-teal)](https://docs.aiohttp.org/)
[![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![Vite](https://img.shields.io/badge/Vite-5-646CFF?logo=vite&logoColor=white)](https://vitejs.dev/)
[![DhanHQ](https://img.shields.io/badge/DhanHQ-v2%20API-orange)](https://dhanhq.co/docs/v2/)
[![License](https://img.shields.io/badge/License-Private-red)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Active-brightgreen)]()

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      main.py  (asyncio)                         │
│                                                                 │
│   ┌──────────────┐     ┌─────────────────┐     ┌────────────┐  │
│   │  DhanClient  │────▶│   RiskManager   │────▶│  Strategy  │  │
│   │  (aiohttp)   │     │  (background    │     │  Engine    │  │
│   │              │     │   loop, 30s)    │     │            │  │
│   │  Rate limiter│     │                 │     │ ┌────────┐ │  │
│   │  Retry logic │     │  Daily loss cap │     │ │Scalper │ │  │
│   │  Auth headers│     │  Position limit │     │ │RSI-14  │ │  │
│   └──────┬───────┘     │  Kill switch    │     │ │OCO exit│ │  │
│          │             │  Halt callbacks │     │ └────────┘ │  │
│          │             └─────────────────┘     │ ┌────────┐ │  │
│          │                                     │ │  SMA   │ │  │
│          │                                     │ │  9/21  │ │  │
│          │                                     │ └────────┘ │  │
│          │                                     └────────────┘  │
│          │                                                      │
│          ▼                                                       │
│   ┌──────────────┐     ┌──────────────────────┐                │
│   │  Instrument  │     │   aiohttp Web App     │                │
│   │  Master      │     │   (port 8765)         │                │
│   │              │     │                       │                │
│   │  CSV cache   │     │   React 18 + Vite     │                │
│   │  6h TTL      │     │   dashboard           │                │
│   │  4120 NIFTY  │     │                       │                │
│   │  contracts   │     │   /api/* REST JSON    │                │
│   └──────────────┘     └──────────────────────┘                │
└─────────────────────────────────────────────────────────────────┘
          │
          ▼
  DhanHQ v2 REST API  ──▶  NSE / BSE
  api.dhan.co/v2
```

---

## Features

**Two production-ready strategies**
- **Options Scalper** — RSI-14 on NIFTY index, buys ATM options, places Forever OCO immediately after fill. Auto-discovers security IDs from the Dhan scrip master CSV.
- **SMA 9/21 Crossover** — Golden/death cross on any NSE equity. Configurable fast and slow periods.

**Risk manager** runs as a parallel async task every 30 seconds:
- Daily loss cap with configurable INR limit
- Maximum open positions enforcement
- Per-trade capital exposure check
- Kill switch for immediate trading halt
- Async halt callbacks for alerting

**Breakeven-aware exit pricing** — all F&O charges calculated before placing OCO:
- Brokerage ₹20/leg, STT 0.1% sell, exchange fee 0.053%, SEBI ₹10/crore, GST 18%, stamp 0.003% buy

**Instrument master** — downloads `api-scrip-master.csv` from Dhan on startup, caches for 6 hours, indexes ~4,120 NIFTY option contracts. O(1) ATM lookup by price and expiry. Auto-selects nearest weekly expiry when `EXPIRY_DATE` is blank.

**Backtesting engine** — replay any `BaseStrategy` against historical OHLCV bars. Computes Sharpe ratio, max drawdown, win rate, and equity curve without touching the API.

**React dashboard** on `http://localhost:8765` — live P&L, risk state, signal feed, position detail, instrument browser.

**Paper trading mode** on by default — set `PAPER_TRADING=false` only when you are ready for live capital.

---

## Quick Start

### 1. Clone and create virtual environment

```bash
git clone <repo-url> dhan_algo
cd dhan_algo
python3.14 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env .env.local   # or edit .env directly
```

| Variable | Required | Description |
|---|---|---|
| `DHAN_CLIENT_ID` | Yes | Your DhanHQ client ID |
| `DHAN_ACCESS_TOKEN` | Yes | JWT from DhanHQ developer portal |
| `PAPER_TRADING` | No | `true` (default) — set `false` for live |
| `MAX_DAILY_LOSS` | No | INR daily loss cap (default `5000`) |
| `STRATEGY` | No | `scalper` (default) or `sma` |
| `EXPIRY_DATE` | No | `YYYY-MM-DD` — blank = nearest weekly |
| `NUM_LOTS` | No | Number of lots (default `1`) |
| `WEBHOOK_PORT` | No | Dashboard port (default `8765`) |

Full variable reference: [Configuration wiki](wiki/Configuration.md)

### 3. Run

```bash
source venv/bin/activate
python main.py
```

Open `http://localhost:8765` for the live dashboard.

To switch strategies:

```bash
STRATEGY=sma python main.py
STRATEGY=scalper EXPIRY_DATE=2026-05-15 python main.py
```

### 4. Build the React dashboard (optional)

```bash
cd dashboard
npm install
npm run build
cd ..
python main.py   # now serves the compiled React app
```

---

## Environment Variable Reference

| Variable | Default | Description |
|---|---|---|
| `DHAN_CLIENT_ID` | — | DhanHQ client ID (required) |
| `DHAN_ACCESS_TOKEN` | — | JWT access token (required) |
| `PAPER_TRADING` | `true` | Paper mode — no real orders placed |
| `MAX_DAILY_LOSS` | `5000` | INR loss cap before platform halts |
| `STRATEGY` | `scalper` | Active strategy: `scalper` or `sma` |
| `EXPIRY_DATE` | _(auto)_ | Option expiry `YYYY-MM-DD` |
| `NUM_LOTS` | `1` | Number of NIFTY option lots |
| `WEBHOOK_PORT` | `8765` | HTTP port for dashboard and postback |

---

## Project Layout

```
dhan_algo/
├── main.py                   # Orchestrator, web server, signal handlers
├── requirements.txt
├── .env                      # Credentials and runtime config
│
├── core/
│   ├── client.py             # DhanHQ v2 async HTTP client
│   ├── risk.py               # RiskManager + RiskConfig
│   ├── instruments.py        # InstrumentMaster — scrip CSV parser
│   ├── charges.py            # BreakevenCalculator
│   └── backtest.py           # Backtesting engine
│
├── strategies/
│   ├── strategy_base.py      # BaseStrategy, SMA 9/21, StraddleSeller
│   └── options_scalper.py    # OptionsScalperStrategy
│
├── dashboard/                # React 18 + Vite frontend
│   ├── src/
│   │   ├── App.jsx
│   │   └── utils.js
│   └── dist/                 # Built output (served by aiohttp)
│
└── .cache/
    └── scrip_master.csv      # Auto-downloaded, refreshed every 6h
```

---

## API Endpoints

All endpoints return JSON. No authentication required (bind to localhost).

| Endpoint | Description |
|---|---|
| `GET /api/status` | Strategy state, uptime, warmup progress |
| `GET /api/risk` | P&L snapshot, violations, halt state |
| `GET /api/signals` | Last 50 signals (newest first) |
| `GET /api/funds` | Available funds from DhanHQ |
| `GET /api/positions` | Open positions from DhanHQ |
| `GET /api/scalper` | Scalper-specific: RSI, OCO state, ATM |
| `GET /api/instruments` | NIFTY expiry list from instrument master |
| `POST /postback` | DhanHQ order postback webhook receiver |

Full request/response documentation: [API Reference wiki](wiki/API-Reference.md)

---

## Wiki

| Page | Contents |
|---|---|
| [Home](wiki/Home.md) | Overview, feature highlights, navigation |
| [Setup Guide](wiki/Setup-Guide.md) | Full step-by-step on Ubuntu |
| [Configuration](wiki/Configuration.md) | Every `.env` variable and strategy param explained |
| [Strategies](wiki/Strategies.md) | Signal logic, breakeven math, state machine diagrams |
| [API Reference](wiki/API-Reference.md) | All 8 endpoints with request/response examples |

---

## Disclaimer

This software is for educational and research purposes. Algorithmic trading involves substantial financial risk. The authors are not responsible for any trading losses. Always start in paper mode and understand the strategy logic before committing real capital.
