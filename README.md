# DhanAIBot — Algorithmic Trading Platform for NSE

A self-hosted algorithmic trading platform for NSE (National Stock Exchange of India) equities and index options. It combines rule-based strategies with an OHLCV foundation model (Kronos) for AI signal filtering, a Hermes agent running on Groq LLM for orchestration, and TimescaleDB for all market data and trade history.

**All execution happens on AWS EC2 (ap-south-1, Mumbai). The Mac is an editor only.**

---

## Architecture

```
  Mac (VS Code + Claude Code)
         │ git push
         ▼
     GitHub (main)
         │ git pull
         ▼
  AWS ap-south-1
  ┌──────────────────────────────────────────────────────────────────┐
  │  agent EC2  t4g.micro  (13.206.66.237 Elastic IP)               │
  │                                                                  │
  │  main.py (asyncio)                                               │
  │  ┌──────────────┐  ┌─────────────────┐  ┌────────────────────┐  │
  │  │  DhanClient  │  │  RiskManager    │  │  Strategy Engine   │  │
  │  │  aiohttp     │  │  30s monitor    │  │                    │  │
  │  │  5 req/s     │  │  kill-switch    │  │  IndexOptionsScanner│ │
  │  │  TOTP refresh│  │  daily loss cap │  │  (6 indices, RSI)  │  │
  │  └──────┬───────┘  └─────────────────┘  │                    │  │
  │         │                               │  MultiStockScanner │  │
  │         │          ┌─────────────────┐  │  (top 15 movers)   │  │
  │         │          │  LiveFeed       │  │                    │  │
  │         │          │  WebSocket ticks│  │  ORBStrategy       │  │
  │         │          │  6 indices +    │  │  (Kronos-gated)    │  │
  │         │          │  top 15 equities│  └────────────────────┘  │
  │         │          └─────────────────┘                          │
  │         │                                                        │
  │  ┌──────▼─────────────────────────────────────────────────────┐ │
  │  │  aiohttp web server :8765  (React dashboard + /api/*)      │ │
  │  └────────────────────────────────────────────────────────────┘ │
  │                                                                  │
  │  hermes agent  ──  @farshoribot (Telegram)                      │
  │  groq llama-3.3-70b-versatile via OpenRouter                    │
  └──────────────────────────────────────────────────────────────────┘
         │ private subnet (10.0.0.0/16)
         ▼
  ┌──────────────────────────────────┐
  │  db EC2  t4g.medium  (private)   │
  │  TimescaleDB 19 tables           │
  │  5 hypertables  ~4.8M bars       │
  └──────────────────────────────────┘
         │
         ▼
  DhanHQ v2 REST  api.dhan.co/v2  →  NSE / BSE
```

---

## Tech Stack

| Layer | Technology | Notes |
|---|---|---|
| Language | Python 3.11 | Graviton ARM64 on EC2 |
| Async runtime | asyncio + aiohttp | Single process, all concurrent |
| Broker API | DhanHQ v2 REST | No sandbox — all calls hit production |
| Database | TimescaleDB (PostgreSQL + Timescale) | Hypertables, compression, free scans |
| DB migrations | Alembic | 3 versions, current head: `003_auth_tables` |
| AI signal filter | Kronos-small (NeoQuasar/HuggingFace) | OHLCV foundation model, AAAI 2026 |
| Orchestration LLM | Groq `llama-3.3-70b-versatile` via OpenRouter | NOT Anthropic — too expensive for always-on |
| Hermes agent | NousResearch Hermes | Telegram gateway (@farshoribot) |
| Frontend | React 18 + Vite | Polls `/api/*`, served from `dashboard/dist/` |
| Secrets | AWS SSM Parameter Store | Pulled by `setup_agent.sh` on first boot |
| Infra | Terraform, ap-south-1, t4g Graviton | ~$56/month |

---

## Milestone Status

| Milestone | Status | Description |
|---|---|---|
| M0 — Infrastructure | Done | Terraform applied, TimescaleDB running, schema at head |
| M1 — Data backfill | Done | 22,646 NSE equities being backfilled; 4.8M 1-min bars loaded |
| M2 — Live WebSocket feed | In progress | LiveFeed connects; not yet writing to `ticks` table |
| M3 — Backtester wired | Not started | `core/backtest.py` reads mock data, not `bars` hypertable |
| M4 — Paper trading loop | In progress | `main.py` orchestrates both scanners; DB writes not fully wired |
| M5 — Hermes orchestration | In progress | Hermes gateway online at @farshoribot; Groq configured |
| M6 — Auth layer | Not started | Schema exists in `003_auth_tables`, no FastAPI routes |
| M7 — Shadow orders | Not started | Log real-intent orders without executing |
| M8 — Tiny live | Not started | 1-share real orders, reconcile fills |
| M9 — Scale | Not started | After M8 validated |

---

## Key Constraints

- **No Dhan sandbox.** Every API call hits `api.dhan.co/v2` production infrastructure.
- **Static IP required for live orders.** The agent EC2 Elastic IP (13.206.66.237) must be whitelisted in Dhan DevPortal under API settings. Once set, it cannot change for 7 days. Data APIs (historical bars, live feed, quotes) have no IP restriction.
- **PAPER_TRADING=true always.** Never flip to `false` until backtesting on 2+ years of real data passes with realistic slippage.
- **5 req/s rate limit.** Enforced in `core/client.py` with a token bucket.
- **Max 90 days per intraday API call.** `backfill.py` chunks automatically.
- **Token expires ~24h.** `core/auth.py` auto-refreshes via PIN + TOTP when `DHAN_PIN` and `DHAN_TOTP_SECRET` are set.
- **Kronos is fail-open.** Model errors never block trades — `_kronos_allows()` returns `True` on exception.

---

## Quick Start

### Local dev (docker-compose)

```bash
# Clone
git clone <repo-url> DhanAIBot && cd DhanAIBot

# Configure
cp .env.example .env
# Edit .env: set DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN at minimum

# Start TimescaleDB locally
docker compose up -d

# Apply schema
source venv/bin/activate      # or: python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
alembic upgrade head

# Load instrument master (~224K scrips)
python backfill.py --instruments

# Backfill watchlist (90 days, default .env watchlist)
python backfill.py

# Run platform in paper mode
python main.py
# Dashboard at http://localhost:8765
```

### AWS deploy (production path)

```bash
# 1. Provision infrastructure (run once from Mac terminal)
cd infra && terraform init && terraform apply
# Note the outputs: agent_elastic_ip, db_private_ip

# 2. Whitelist agent Elastic IP in Dhan DevPortal → API settings (order APIs only)

# 3. SSH to agent EC2
ssh -i ~/.ssh/dhan_trading_key ubuntu@<agent_elastic_ip>

# 4. On agent EC2 — repo is already cloned by setup_agent.sh
cd ~/dhan_algo && source .venv/bin/activate

# 5. Pull latest code
git pull origin main

# 6. Verify schema
alembic current   # expect: 003_auth_tables (head)

# 7. Load all instruments (run once)
python backfill.py --instruments

# 8. Backfill Nifty 50 — 5 years of 1-min + daily bars (~3.5 min)
python backfill.py --all --from 2021-06-01

# 9. Expand to all NSE equities (~5 days, running in background)
python backfill.py --nse-eq --all --from 2021-06-01

# 10. Run the platform
python main.py

# Dashboard access via SSH tunnel from Mac:
ssh -L 8765:localhost:8765 ubuntu@<agent_elastic_ip>
# Then open: http://localhost:8765
```

---

## Documentation

| Page | Contents |
|---|---|
| [docs/Home.md](docs/Home.md) | System overview, feature highlights, tech stack |
| [docs/Setup-Guide.md](docs/Setup-Guide.md) | Local dev, AWS deploy, Hermes setup |
| [docs/Configuration.md](docs/Configuration.md) | All env vars, strategy config fields, DB config |
| [docs/Strategies.md](docs/Strategies.md) | ORB, Options Scalper, SMA Crossover signal logic |
| [docs/API-Reference.md](docs/API-Reference.md) | All 30+ REST endpoints with request/response examples |

---

## Safety Rules

1. `PAPER_TRADING=true` is the default. Never flip without explicit intent and backtesting evidence.
2. IP whitelist applies only to order placement. Data APIs work from any IP.
3. `RiskManager` owns the kill-switch. All orders route through `core/risk.py`.
4. No live trading until backtesting passes on 2+ years of real NSE data with realistic costs.
5. Kronos failures must never block trades. `_kronos_allows()` returns `True` on any exception.
6. Hermes may only propose changes to strategy/risk parameters. Human approval required for anything touching position sizing, kill-switch threshold, or live order flow.

---

## Disclaimer

This software is for educational and research purposes. Algorithmic trading involves substantial financial risk. Always start in paper mode. The authors are not responsible for trading losses.
