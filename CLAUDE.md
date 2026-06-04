# DhanAIBot — Hermes-Kronos Trading Platform
**Repo:** `github.com/armaanfarshori/dhan_algo`  
**Last updated:** 2026-06-04  
**Current phase:** PHASE 2 — BACKFILL RUNNING / KRONOS LIVE

---

## ⚡ TL;DR — Current state

```
Backfill running on agent EC2 — ~22,646 NSE equities, completes ~June 8–9.
Platform is live in PAPER mode with ORB + Kronos (zero-shot).
Hermes gateway online at @farshoribot on Telegram.
Nothing needs provisioning — AWS is fully live.
```

**Dev workflow (Mac = editor only):**
```
Mac (VS Code + Claude Code)  →  git push  →  GitHub  →  git pull on agent EC2
```

**SSH access:**
```bash
ssh -i ~/.ssh/dhan_trading_key ubuntu@13.206.66.237          # agent
ssh -J ubuntu@13.206.66.237 -i ~/.ssh/dhan_trading_key ubuntu@10.0.1.155  # DB
```

---

## AWS infrastructure — live

| Resource | Value |
|---|---|
| Agent EC2 (t4g.micro) | `13.206.66.237` — Elastic IP, whitelisted in Dhan DevPortal |
| DB EC2 (t4g.medium) | `10.0.1.155` — private subnet only |
| TimescaleDB | Running in Docker on DB EC2 — 19 tables, 5 hypertables, migration 003 |
| S3 bucket | `dhan-trading-data-155304839154` |
| SSM secrets | `/dhan-trading/{dhan_client_id, dhan_access_token, dhan_totp_secret, dhan_pin, groq_api_key, openrouter_api_key, telegram_bot_token}` |
| Monthly cost | ~$56/mo (DB $25 + agent $6 + 200GB EBS $18 + misc $7) |

**Emergency teardown:** `cd infra && ./teardown.sh`

---

## Milestone status

| Milestone | Status | Notes |
|---|---|---|
| M0 — AWS infrastructure | ✅ **Done** | VPC, EC2×2, EIP, S3, SSM, IAM, Terraform committed |
| M1 — Database schema | ✅ **Done** | 19 tables, 5 hypertables, compression + retention, migration 003 |
| M2 — Data pipeline | ⏳ **60%** | Backfill running (~June 8–9). WebSocket→DB not yet wired. |
| M3 — Backtester on real bars | ❌ Not started | Blocked on backfill completion |
| M4 — Execution engine DB writes | ✅ **Done** | orders/fills/positions/equity_curve write to TimescaleDB |
| M5 — Deployment + ops | ⚠️ Partial | systemd + NSE cron done, no CloudWatch/alerts |
| M6 — Operator auth layer | ❌ Schema only | users/sessions/auth_events tables exist, no FastAPI auth |
| M7 — Readonly validation | ❌ Not started | Needs M3 first |
| M8 — Tiny live | ❌ Not started | Needs M7 + Elastic IP whitelisted (already done) |
| Kronos zero-shot | ✅ **Done** | Integrated, ORB gate wired, lazy-loads on first use |
| Kronos fine-tuned on NSE | ❌ Next after backfill | See fine-tuning plan below |

---

## Component status

| Component | Status | Notes |
|---|---|---|
| TimescaleDB (AWS) | ✅ Running | 19 tables, ~11M bars (Jun 4), migration 003 head |
| Backfill | ⏳ Running | `screen -ls` on agent, ~22,646 NSE equities, ~75% hit rate |
| `core/live_feed.py` | ⚠️ Partial | WebSocket works, not writing to `ticks` table |
| `core/backtest.py` | ⚠️ Partial | Reads mock data, not wired to `bars` hypertable |
| Kronos (`core/kronos_signal.py`) | ✅ Integrated | Zero-shot, lazy-loaded (OOM safe on t4g.micro) |
| Hermes + OpenRouter | ✅ Running | @farshoribot, 19 skills, 14 cron jobs |
| `main.py` orchestrator | ✅ Running | ORB+Kronos on screener-dynamic watchlist |
| React dashboard | ✅ Running | 3 tabs: Signals, Portfolio, System — port 8765 |
| Auth layer | ❌ Schema only | After M6 |

---

## Backfill — current state

- **Started:** June 3, 2026
- **Expected completion:** ~June 8–9 (22,646 NSE equities × 21 API calls each)
- **Rate:** 100K Dhan data API calls/day limit, 5 req/s enforced
- **Progress monitor:** `ssh ubuntu@13.206.66.237 "tail -f /tmp/backfill.log"`
- **DB progress:** `curl -s http://localhost:8765/api/db/stats | python3 -m json.tool`
- **Hit rate:** ~75% — suspended/illiquid instruments return 0 candles (expected, not an error)
- **Auto-recovery:** `backfill_resume` Hermes skill runs every 15 min, auto-restarts screen if dead
- **Token refresh:** `backfill.py` uses `DhanAuthManager` — token auto-refreshes via TOTP

---

## Active strategy — ORB + Kronos

```
Market open → NSE screener (ATR%) → top-N volatile equities
  → ORBStrategy polls live OHLC (9:00–15:35 IST only)
  → Breakout detected → _kronos_allows(direction)
      → KronosSignalEngine.score_from_db() → 400 bars → 30-bar forecast
      → confidence ≥ KRONOS_MIN_CONFIDENCE (0.4) → allow trade
      → confidence < 0.4 or wrong direction → skip (fail-open on error)
  → BaseStrategy.buy()/sell() → RiskManager gate → DhanClient.place_order()
  → DB: orders + fills + trades + positions + equity_curve written
```

**Key configs:**
- `WATCHLIST_N=5` — screener picks top-5 ATR% securities each startup
- `POLL_INTERVAL=20` — 20s between polls per strategy (staggered start: +5s per strategy)
- `KRONOS_MIN_CONFIDENCE=0.4` — gate threshold
- `ORB_RANGE_MINUTES=15` — opening range window
- `PAPER_TRADING=true` — **never change without explicit intent**

---

## Hermes orchestration

- **Version:** v0.15.1
- **LLM:** OpenRouter → `meta-llama/llama-3.3-70b-instruct` (NOT Groq direct — Groq free tier is 12K TPM, Hermes system prompt is 18K tokens, it breaks)
- **Telegram bot:** `@farshoribot` (chat_id: `7229051134`, owner: `@lolsisi`)
- **Skills:** 19 skills in `~/.hermes/skills/dhan/` + `hermes_skills/dhan/` in repo
- **Cron jobs:** 14 scheduled (pre-market 8:45, drawdown every 5min market hours, position reconcile every 30min, backfill watchdog every 15min, EOD review 15:45, data quality 2AM, gap scan 2:30AM, strategy performance Sunday, signal calibration Sunday)

**Common Telegram commands:**
```
check data freshness    → backfill_check skill
run Kronos forecast     → kronos_forecast skill  
system status           → execution_loop skill
HALT                    → kill_switch skill (emergency)
```

**Why OpenRouter not Groq:** Groq free tier has 12K TPM limit. Hermes system prompt alone is ~18K tokens. Every request would fail. OpenRouter has credit-based billing, no TPM cap.

---

## Kronos — current state

### What's wired
- **File:** `core/kronos_signal.py` → `KronosSignalEngine`
- **Model:** `NeoQuasar/Kronos-small` (24.7M params) — lazy-loaded from HuggingFace
- **`score_from_db(security_id)`** — pulls 400 1-min bars from `bars`, forecasts 30 bars → `{side, score, confidence, forecasted_return}`
- **`score_batch()`** — concurrent scoring of full watchlist
- **ORB gate:** `_kronos_allows()` called before every entry in `strategy_orb.py`
- **Confidence gate:** ≥0.4 required; skip trade if below
- **Fail-open:** Kronos errors never block trades
- **OOM protection:** lazy-loads on first signal, not at startup (t4g.micro = 1GB RAM)

### What zero-shot means
Pre-trained on 45 global exchanges. Has never seen NSE auction mechanics, SGX Nifty gap-ups, monthly F&O expiry volatility, or Indian corporate action patterns. Works as a generalist. Fine-tuning makes it NSE-native.

### KRONOS_CHECKPOINT env var
Already wired. Currently empty → loads from HuggingFace zero-shot. After fine-tuning, set to S3 path → loads NSE-specific checkpoint automatically.

---

## Kronos fine-tuning plan (after backfill ~June 8–9)

### When to run
After backfill completes AND M3 backtester is wired. Run three-way comparison first. Fine-tune only if zero-shot doesn't already show meaningful improvement over baseline ORB.

### Step 0 — Clean training data (critical)

Write `scripts/prepare_kronos_dataset.py`. NSE-specific problems to detect:
- Corporate action gaps (price jumps >20% overnight — bonus/splits/rights issues)
- Circuit breaker sessions (zero/near-zero volume days)
- Pre-market auction rows (9:00–9:15 IST candles)
- Exchange holiday gaps (expected, handle with session-aware windowing)
- Newly listed stocks (<6 months data)

**Safe training universe:** NSE_EQ securities with ≥2 years continuous 1-min bars + avg_volume >50K + no corporate action gaps >15% overnight.

Output: clean Parquet files to `s3://dhan-trading-data-155304839154/kronos/training-data/`

### Step 1 — Spin up spot GPU

```bash
aws ec2 run-instances \
  --instance-type g4dn.xlarge \
  --instance-market-options '{"MarketType":"spot","SpotOptions":{"MaxPrice":"0.30"}}' \
  --key-name dhan_trading_key \
  --iam-instance-profile Name=dhan-trading-prod-ec2-profile \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=kronos-finetune}]'
# Cost: ~$0.16–0.30/hr spot × 2–6 hrs = ~$1–3 total
# TERMINATE IMMEDIATELY after checkpoint uploads to S3
```

### Step 2 — Data split (STRICT — never random)

```
Train:    2021-06-01 → 2024-12-31
Validate: 2025-01-01 → 2025-12-31
Test:     2026-01-01 → today  ← touch ONCE for final eval only
```

**Never use random train/test split on time-series. Always split by date.**

### Step 3 — Fine-tune Kronos-base

Use `Kronos-base` (102.3M params) not `Kronos-small` — needs more capacity for NSE patterns.

**Tokenizer runs FIRST, predictor SECOND.** Kronos quantizes candles into discrete tokens before transformer trains. Skip tokenizer fitting → bad vocabulary → bad model.

```bash
python finetune.py \
  --model NeoQuasar/Kronos-base \
  --data_path ~/nse_training_data/ \
  --context_length 512 \
  --prediction_length 30  # matches score_from_db() call
```

### Step 4 — Validate + upload + switch

```bash
# Upload checkpoint
aws s3 sync ~/kronos-nse-v1/ s3://dhan-trading-data-155304839154/kronos/checkpoints/nse-v1/

# TERMINATE GPU INSTANCE

# On agent EC2 — update .env
KRONOS_CHECKPOINT=s3://dhan-trading-data-155304839154/kronos/checkpoints/nse-v1/
# Restart platform — KronosSignalEngine lazy-loads from S3 on first call
```

### Three-way backtest comparison

Run all three over the same 2-year window with identical realistic costs:

| Run | Config | Purpose |
|---|---|---|
| 1 | ORB standalone | Baseline — pure rule-based |
| 2 | ORB + Kronos zero-shot | Does pre-trained model add value on NSE? |
| 3 | ORB + Kronos fine-tuned | Does NSE-specific training improve further? |

**Costs to include:** STT (0.025% delivery, 0.01% intraday), brokerage (₹20/order), exchange charges, GST.

**Decision rule:** promote fine-tuned model only if Run 3 shows meaningfully better Sharpe than Run 2.

---

## Key architecture decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Database | TimescaleDB self-hosted on EC2 | Free query scans for backtesting (no per-query billing like Timestream) |
| Hermes LLM | OpenRouter → `meta-llama/llama-3.3-70b-instruct` | Groq direct breaks on free tier (12K TPM < 18K system prompt) |
| Kronos model | `NeoQuasar/Kronos-small` (zero-shot) → `Kronos-base` (fine-tuned) | OHLCV foundation model, AAAI 2026 |
| Broker | DhanHQ v2 (`api.dhan.co/v2`) — **no sandbox** | All API calls hit production |
| Cloud | AWS `ap-south-1` (Mumbai) | NSE latency + data residency |
| Compute | 2× EC2 t4g Graviton ARM64 | Cost-efficient: DB t4g.medium ($25/mo), agent t4g.micro ($6/mo) |
| Watchlist | Dynamic from ATR screener | `WATCHLIST_N=5` — no static IDs in `.env` |
| Trading mode | `PAPER_TRADING=true` default | Never flip to live without explicit intent |
| ORB polling | 20s interval, staggered +5s per strategy | Prevents 429 bursts on Dhan 1 req/s quote limit |

---

## Key constraints — never forget

- **No Dhan sandbox.** Every API call hits `api.dhan.co/v2` production.
- **IP whitelist = order placement only.** Data/historical/WebSocket APIs work from any IP. Elastic IP `13.206.66.237` is whitelisted.
- **`intraday_minute_data` SDK = last 5 days only.** Backfill uses REST `/v2/charts/intraday` directly.
- **Dhan 100K API calls/day hard limit.** Rate limiter enforced in `core/client.py`.
- **t4g.micro has 1GB RAM.** Kronos must lazy-load — no eager startup load.
- **Groq direct breaks Hermes.** Use OpenRouter. Groq free tier 12K TPM < 18K Hermes system prompt.
- **ORB strategies have market-hours gate.** `_is_market_hours()` check in `BaseStrategy.run()` — zero API calls outside 9:00–15:35 IST weekdays.
- **No static watchlist.** `WATCHLIST_SECURITY_IDS` removed from `.env`. Screener-only. `WATCHLIST_N=5`.
- **`TRADING_MODE=paper` is default.** Never change to live without explicit intent.
- **RiskManager owns kill-switch.** Never bypass. All orders route through `core/risk.py`.
- **Orphan protection:** Action watchlist in UI shows positions separately from screener list. If a security exits the screener mid-session, its open position is still visible and tracked.

---

## File structure

```
dhan_algo/
├── core/
│   ├── auth.py             DhanAuthManager (TOTP auto-refresh)
│   ├── backtest.py         Event-driven backtester (not yet wired to bars)
│   ├── charges.py          F&O brokerage calculator
│   ├── client.py           Async DhanHQ v2 client (rate limiter, retry)
│   ├── instrument_sync.py  Scrip master downloader (224K instruments)
│   ├── instruments.py      InstrumentMaster (ATM option lookup)
│   ├── journal.py          TradeLogger + AsyncDBBackend + LogBuffer (unified)
│   ├── kronos_signal.py    KronosSignalEngine (score_from_db, score_batch)
│   ├── live_feed.py        WebSocket tick feed (not yet → DB)
│   ├── nse_screener.py     ATR volatility screener (get_top_volatile)
│   ├── risk.py             RiskManager (kill-switch, position limits, equity snapshots)
│   └── watchlist.py        WatchlistManager
├── strategies/
│   ├── strategy_base.py    BaseStrategy ABC + market-hours gate + DB logging
│   ├── strategy_orb.py     ORB — Kronos-gated, staggered polling, EOD square-off
│   ├── options_scalper.py  RSI ATM scalper (inactive — not started by main.py)
│   ├── scanner.py          MultiStockScanner (inactive — not started)
│   ├── index_options.py    IndexOptionsScanner (inactive — not started)
│   └── backtest_strategies.py  RSI/Momentum/MR/Bollinger/VWAP (for M3)
├── kronos/                 Vendored Kronos model (MIT, AAAI 2026, shiyu-coder/Kronos)
├── hermes_skills/dhan/     19 Hermes trading skills + 14 cron schedules
├── dashboard/              React 18 + Vite — 3 tabs: Signals, Portfolio, System
├── infra/                  Terraform — VPC, EC2×2, EBS, S3, SSM, IAM
├── alembic/versions/       001 → 002 → 003 (auth tables)
├── main.py                 Async orchestrator (ORB+Kronos only, legacy scanners disabled)
├── backfill.py             Historical OHLCV CLI — --instruments, --nse-eq, --all
├── config.py               Typed Config (watchlist_n, no static IDs)
├── db.py                   SQLAlchemy engine + session helpers
└── docker-compose.yml      LOCAL DEV ONLY — not used in AWS
```

---

## What to build next (in order)

1. **`scripts/prepare_kronos_dataset.py`** — clean NSE bars, export Parquet to S3
2. **M3: wire `core/backtest.py` to `bars` hypertable** — replace mock data with real TimescaleDB reads
3. **Run three-way backtest** — ORB alone vs zero-shot vs (later) fine-tuned
4. **Fine-tune Kronos-base** — spot GPU g4dn.xlarge, NSE bars, S3 checkpoint
5. **M2 completion: wire `core/live_feed.py` → `ticks` table** — real-time bar building
6. **M7 readonly validation** — shadow trading, log orders without placing

---

## Running the platform (all on AWS — Mac = editor only)

```bash
# Check platform status
ssh ubuntu@13.206.66.237 "ss -tlnp | grep 8765"

# View dashboard (SSH tunnel)
ssh -i ~/.ssh/dhan_trading_key -N -L 8765:localhost:8765 ubuntu@13.206.66.237
# Open http://localhost:8765

# Monitor backfill
ssh ubuntu@13.206.66.237 "tail -f /tmp/backfill.log"

# Check platform logs
curl -s http://localhost:8765/api/logs?limit=20

# Restart platform
ssh ubuntu@13.206.66.237 "screen -S platform -X quit; sleep 2; screen -dmS platform bash -c 'cd /opt/dhan-trading && set -a && source .env && set +a && .venv/bin/python3 main.py >> /tmp/platform.log 2>&1'"

# Deploy update (from agent EC2)
bash ~/.hermes/skills/dhan/deploy_update/scripts/deploy.sh
```

---

## Safety rules (never override)

1. **`PAPER_TRADING=true` is default.** Only flip to `live` with an explicit, deliberate change.
2. **IP whitelist only applies to order placement APIs.** Data APIs work from any IP. Elastic IP `13.206.66.237` whitelisted. Once whitelisted, IP cannot be changed for 7 days.
3. **RiskManager owns the kill-switch.** Never bypass it. All orders route through `core/risk.py`.
4. **No live trading until backtesting passes** on 2+ years of real data with realistic costs.
5. **Kronos is fail-open.** Model errors must never block trades — `_kronos_allows()` returns `True` on exception.
6. **Hermes may only propose changes.** Human approval required before deploying any skill that affects position sizing, kill-switch threshold, or live order flow.
7. **Dhan has no sandbox.** Every API call that isn't paper-mode simulation hits real infrastructure.
8. **No static watchlist.** Screener picks securities dynamically. Never hardcode `WATCHLIST_SECURITY_IDS` in `.env`.
