# DhanAIBot — Algorithmic Trading Platform for NSE

A self-hosted intraday trading platform for NSE (National Stock Exchange of India) equities, built on the [DhanHQ v2 API](https://dhanhq.co/docs/v2/). It runs a rule-based Opening Range Breakout strategy filtered through [Kronos](https://github.com/shiyu-coder/Kronos), an OHLCV foundation model (AAAI 2026), with TimescaleDB as the single source of truth for market data, orders, and positions.

The design priority is **evidence before exposure**: paper trading is the hard default, the AI gate runs in shadow mode until calibration proves it adds value, and live trading is gated on a 2-year backtest with realistic Indian cost modelling.

> ⚠️ **Disclaimer:** educational and research software. Algorithmic trading involves substantial financial risk. Always start in paper mode. The authors are not responsible for trading losses.

---

## Architecture

Two independent processes share nothing but the database and a heartbeat file — an analytics query can never delay an order.

```
                      Dhan WebSocket feed          Dhan REST API
                             │                          │
  ┌──────────────────────────▼──────────────────────────▼─────────────┐
  │  dhan-trader  (apps/trader.py — systemd)                          │
  │                                                                   │
  │  LiveFeed ──► BarBuilder ──► bars hypertable   (1-min bars → DB)  │
  │      │                                                            │
  │      └──► StrategyRunner ──► ORB (pure signal logic)              │
  │                 │                                                 │
  │                 ├── KronosGate (shadow) ──► signals table         │
  │                 ├── RiskEngine (sizing, daily-loss halt,          │
  │                 │               kill-switch — single owner)       │
  │                 └── Paper | Live executor ──► Portfolio (DB)      │
  │                                                                   │
  │  exports run/trader_heartbeat.json every 5 s                      │
  └───────────────────────────────────────────────────────────────────┘
                             │ heartbeat + DB (read-only)
  ┌──────────────────────────▼────────────────────────────────────────┐
  │  dhan-api  (apps/api.py — systemd)                                │
  │  React dashboard + ~30 REST endpoints on :8765                    │
  └───────────────────────────────────────────────────────────────────┘

  TimescaleDB (separate EC2, private subnet)
  bars · ticks · orders · fills · trades · positions · equity_curve
  signals · runs · instruments  (5 hypertables, compression policies)
```

**Key design decisions**

| Decision | Why |
|---|---|
| One engine, swappable executors (Paper / Live / Backtest) | Strategies are mode-blind; paper → live is a config flip, not a code path |
| Portfolio persisted to DB, reconciled on boot | A process restart never orphans a position; in live mode the broker is cross-checked as source of truth |
| Kronos gate in **shadow mode** | Every verdict is logged with features for calibration, but never blocks a trade until the data says it should |
| Kronos is fail-open | A model error must never stop the rule-based strategy from managing risk |
| Backtester replays the **same** strategy class | Next-bar-open fills (no lookahead), full Indian intraday cost stack, point-in-time universe |
| TimescaleDB self-hosted | Backtests scan millions of bars for free; no per-query billing |

---

## What's inside

```
apps/
  trader.py            Trading process — feed, strategies, risk, execution
  api.py               Dashboard process — REST API + static React build
engine/
  execution.py         PaperExecutor (slippage-modelled) / LiveExecutor (fill-confirmed)
  portfolio.py         DB-persisted positions, boot + broker reconciliation
  risk.py              Position sizing, daily-loss halt, kill-switch
  bar_builder.py       WebSocket ticks → 1-minute bars → DB
  runner.py            Polling loop per security: gate → size → risk → execute
strategies/
  orb.py               Opening Range Breakout — pure, synchronous, IO-free
ml/
  kronos_gate.py       Shadow/enforcing AI gate; persists every verdict
  calibration.py       Realized-outcome filling + gate-value report (the re-arm criterion)
research/backtest/     Event-driven backtester: engine, costs, universe, report
core/                  Dhan client, token manager, live feed, screener, Kronos engine,
                       instrument master, Telegram alerts, trade journal
dashboard/             React 18 + Vite — Signals / Portfolio / System tabs, mobile-friendly
infra/                 Terraform (VPC, 2× EC2, S3, SSM, IAM) + systemd units
alembic/               Schema migrations (001 → 005)
backfill.py            Historical OHLCV backfill CLI (checkpointed, rate-limited)
config.py              Single typed pydantic-settings object — no os.getenv anywhere else
tests/                 71 tests, run in CI on every push
```

---

## The strategy loop

```
Market open → ATR% screener picks top-N volatile NSE equities
  (₹50 min price · 50K min avg volume · validated against the scrip master)
  → ORB locks the 9:15–9:30 opening range per security
  → breakout/breakdown → Kronos gate scores a 30-bar forecast
       shadow mode: verdict logged, trade proceeds regardless
       enforcing mode: confidence < threshold blocks the entry
  → RiskEngine sizes the position from stop distance (1% equity risk,
    notional cap) and can halt the session on daily loss
  → executor fills (paper: ref price ± slippage · live: broker fill confirmed)
  → exits: target (1.5× range) · stop (range edge ± buffer) · 15:15 EOD square-off
  → every order, fill, trade, equity snapshot and gate verdict lands in the DB
```

---

## Quick start

### Local development

```bash
git clone https://github.com/armaanfarshori/dhan_algo && cd dhan_algo
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # set DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN
docker compose up -d            # throwaway local TimescaleDB
alembic upgrade head

python backfill.py --instruments        # scrip master (~224K instruments)
python backfill.py                      # bars for a starter watchlist

python -m apps.trader                   # paper mode by default
python -m apps.api                      # dashboard → http://localhost:8765
pytest -q                               # 71 tests
```

### Backtesting

```bash
python -m research.backtest --from 2026-05-01 --to 2026-06-01 --n 5
python -m research.backtest --from 2026-05-01 --to 2026-06-01 --gate kronos --json
```

### Production (AWS)

```bash
cd infra && terraform init && terraform apply
# outputs: agent Elastic IP (whitelist it in Dhan DevPortal — order APIs only),
# DB private IP. systemd units in infra/systemd/ run dhan-trader + dhan-api.
```

See [docs/Setup-Guide.md](docs/Setup-Guide.md) for the full walkthrough and [docs/Operations-Runbook.md](docs/Operations-Runbook.md) for day-2 operations.

---

## Documentation

| Page | Contents |
|---|---|
| [docs/Home.md](docs/Home.md) | Overview, current status, roadmap |
| [docs/Architecture.md](docs/Architecture.md) | Process model, engine internals, data flow, design rationale |
| [docs/Setup-Guide.md](docs/Setup-Guide.md) | Local dev and AWS deployment, step by step |
| [docs/Configuration.md](docs/Configuration.md) | Every config field with defaults and safety notes |
| [docs/Strategies.md](docs/Strategies.md) | ORB rules, Kronos gate, risk model, calibration loop |
| [docs/Backtesting.md](docs/Backtesting.md) | Backtester design, cost model, CLI usage |
| [docs/API-Reference.md](docs/API-Reference.md) | REST endpoints with response shapes |
| [docs/Operations-Runbook.md](docs/Operations-Runbook.md) | Deploy, monitor, recover — the ops playbook |

---

## Safety model

1. **`PAPER_TRADING=true` is the default.** Going live requires editing `.env` *and* `ALLOW_LIVE_TOGGLE=true` *and* a process restart — live is never one HTTP request away.
2. **The RiskEngine owns the kill-switch.** All orders route through it; an external kill-switch file halts and flattens within seconds. Nothing bypasses it.
3. **Kronos is fail-open.** Gate errors never block trades — the rule-based exits always run.
4. **No live trading until the backtest passes** on 2+ years of real NSE data with realistic costs (brokerage, STT, exchange charges, GST, slippage).
5. **Dhan has no sandbox.** Every non-paper API call hits production infrastructure. The IP whitelist applies to order placement only.
6. **EOD square-off is unconditional** — it does not depend on strategy state, so a mid-session restart can never leave a position unmanaged overnight.
