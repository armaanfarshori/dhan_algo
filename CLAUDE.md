# DhanAIBot — Trading Platform (agent instructions)
**Repo:** `github.com/armaanfarshori/dhan_algo` (PUBLIC — never commit IPs, account IDs, tokens, chat IDs)
**Last updated:** 2026-06-12
**Current phase:** ENGINE LIVE (paper) · BACKFILL RUNNING · GATE IN SHADOW

---

## 💻 This is the LOCAL MAC clone

You are reading this in `~/Desktop/dhan_algo` — the **Mac working copy** (editor + local
backtesting). The **live platform runs on AWS**, not here. The Mac does NOT run live
trading (one Dhan session per account; order placement is locked to the agent's
whitelisted Elastic IP). Use the Mac to edit code, run tests/backtests, and drive AWS
over SSH.

**AWS access package:** `~/Desktop/dhan_aws_access/` (OUTSIDE this repo — never committed).
Contains the SSH key, `connect.sh` helpers, `aws_inventory.md` (real IPs, instance IDs,
bucket name), and `secrets.md`. **All real infrastructure values live there, not here.**

```bash
~/Desktop/dhan_aws_access/connect.sh agent        # SSH to agent EC2
~/Desktop/dhan_aws_access/connect.sh dashboard    # tunnel → http://localhost:8765
~/Desktop/dhan_aws_access/connect.sh trader-log   # tail trader log
~/Desktop/dhan_aws_access/connect.sh help         # full list
```

AWS CLI on the Mac: profile `dhan-terraform` (set via AWS_PROFILE in ~/.zshrc).

---

## ⚡ TL;DR — Current state (2026-06-12 EOD)

```
TWO PROCESSES on the agent (systemd):
  dhan-trader  (apps/trader.py) — order flow only; heartbeat → run/trader_heartbeat.json
  dhan-api     (apps/api.py)    — dashboard :8765; reads DB + heartbeat only
PAPER mode · Kronos gate in SHADOW (logs verdicts, never blocks) · 71 tests, CI green.
Backfill RUNNING in screen `backfill` (~23%, ckpt backfill_ckpt_NSE_EQ.json, ETA ~Jun 16-17).
First full paper session completed 2026-06-12: 6 trades, clean exits, ~flat after costs.
Screener hardened: ₹50 price floor + 50K volume floor + scrip-master validation.
main.py is GONE (Phase 1). Hermes LLM gateway RETIRED (plain Telegram via core/notify.py).
```

**Dev workflow:** Mac edit → `git push` → on agent `sudo git pull` (repo at `/opt/dhan-trading`)
→ `sudo systemctl restart dhan-trader` (engine code) and/or rebuild dashboard:
`cd dashboard && npm run build` (PATH needs `~/.local/bin:~/.hermes/node/bin`) → `sudo systemctl restart dhan-api`.

---

## Milestone status

| Milestone | Status | Notes |
|---|---|---|
| M0 — AWS infrastructure | ✅ Done | VPC, EC2×2 (agent + DB), EIP, S3, SSM, IAM, Terraform |
| M1 — Database schema | ✅ Done | 19 tables, 5 hypertables, alembic head **005** |
| M2 — Data pipeline | ⏳ | Historical backfill ~23% (all NSE_EQ). **Live half DONE 2026-06-12**: LiveFeed→BarBuilder→bars works (string-SecurityId fix) |
| M2.5 — Clean data replica | ❌ Next | `scripts/build_clean_db.py` exists — review for survivorship, then run after backfill |
| M3 — Backtester on real bars | ⚠️ Built, study pending | `research/backtest/` complete; the 2-year three-way study runs after M2.5 |
| M4 — Execution engine DB writes | ✅ Done | Verified across a full live session |
| M5 — Deployment + ops | ✅ Done | systemd ×2, weekday crons (watchdog/calibration/EOD), Telegram alerts |
| M6 — Auth layer | ❌ Schema only | `/api/mode` POST is read-only + live toggle env-gated as mitigation |
| M7 — Readonly validation | ❌ | Needs M3 |
| M8 — Tiny live | ❌ | Needs M7; Elastic IP whitelisted at Dhan 2026-06-12 (7-day change lock) |
| Kronos zero-shot + shadow gate | ✅ Done | Every verdict persisted (signals, strategy='orb_gate') for calibration |
| Kronos fine-tune (base) | ❌ After M2.5 | Spot GPU, trains on clean Parquet, checkpoint → S3 |

**Critical path: backfill → M2.5 clean DB → M3 three-way backtest → live decision.**
Post-M3 research queue: scheduled-event calendar filter → Kronos small-vs-base shadow A/B.
News/NLP deprioritized (Kronos is OHLCV-only; honest backtests of news are impractical).

---

## Architecture (post-rewrite, 2026-06-11)

```
dhan-trader (apps/trader.py)                 dhan-api (apps/api.py)
  LiveFeed ─► BarBuilder ─► bars               serves React dashboard + /api/*
  LiveFeed ─► StrategyRunner ─► ORB (pure)     reads DB + heartbeat ONLY
      ├─ KronosGate (shadow) ─► signals        16-thread executor (file serving
      ├─ RiskEngine (kill-switch owner)         must never starve behind queries)
      └─ Paper/Live executor ─► Portfolio(DB)
  heartbeat → run/trader_heartbeat.json (5s)
```

- **Executors:** `PaperExecutor` (adverse slippage, journaled) / `LiveExecutor` (polls
  `get_order_by_id` to confirm fills; REJECTED→None CRITICAL; unconfirmed→ref-price flagged).
  Paper→live = `PAPER_TRADING=false` + restart. Everything else is mode-blind.
- **Portfolio:** DB-persisted (`engine_positions`), `reconcile_on_boot()` restores today's
  rows; LIVE mode additionally adopts broker truth via `reconcile_with_broker()`.
- **Mid-session restart:** EOD square-off is unconditional (above the OR-locked gate), and
  `seed_opening_ranges()` rebuilds today's OR from REST intraday bars (~1.2s spacing or
  DH-904), marking already-broken sides as tried.
- **Kill switch:** `run/killswitch` file (written by POST /api/killswitch) → risk loop
  halts + flattens within ~10s. RiskEngine watches the PORTFOLIO (paper losses trip it).
- **Mode change:** edit `.env` + restart dhan-trader. POST /api/mode is read-only by
  design until M6 auth exists.

---

## Key constraints — never forget

- **No Dhan sandbox.** Every API call hits `api.dhan.co/v2` production.
- **Dhan WS feed needs STRING SecurityId** in the subscribe JSON — ints are accepted
  silently and never stream a packet (cost the platform its entire pre-Jun-12 gate history).
- **`charts/intraday` tolerates ~1 req/s** — burst calls get DH-904; space + retry.
- **IP whitelist = order placement only.** Data/historical/WebSocket work from any IP.
- **100K Dhan API calls/day.** Rate limiter in `core/client.py`. Backfill uses REST
  `/v2/charts/intraday` directly (SDK intraday = last 5 days only).
- **NEVER `COUNT(*)` or `ORDER BY time LIMIT 1` on `bars`** (300M+ rows — scans/decompresses
  chunks, hangs minutes). Use `approximate_row_count()`, `hypertable_size()`, chunk-catalog ranges.
- **`features_snapshot` on signals is `json`, not `jsonb`** — cast `::jsonb` before `?`/`->>`.
- **t4g.small = 2 GB RAM.** Kronos lazy-loads on first use; never eager-load at startup.
- **Screener:** dynamic only (no static watchlist env var) — ₹50 price floor, 50K volume
  floor, candidates validated as EQUITY in segment against `instruments`; open positions
  exempt from validation (must always be manageable to exit).
- **`PAPER_TRADING=true` is default.** Never flip without explicit user intent.
- **RiskEngine owns the kill-switch.** Never bypass.
- **The old `platform_watchdog.sh` cron was REMOVED — do not re-add it** (it caused the
  June crash loop by kill -9ing a slow-booting process).
- **Repo is PUBLIC** — placeholders in docs; real values only in `~/Desktop/dhan_aws_access/`.

---

## Alerts & automation (Hermes retired 2026-06-11)

The Hermes LLM gateway burned its $10 OpenRouter budget in one day (the 18K-token system
prompt billed per tool step per 5-min cron). Stopped + disabled; `~/.hermes/` left intact.
Replacement is ₹0/month: `core/notify.py` (plain Telegram bot API; creds in agent `.env`).
Agent crontab (weekdays): backfill watchdog every 15 min · calibration fill+report 16:45 IST
→ `/var/log/dhan/calibration.log` · EOD summary 17:00 IST. The `hermes_skills/dhan/*/scripts/`
are plain Python, usable directly from cron.

---

## Kronos — gate + calibration

- `core/kronos_signal.py` → `KronosSignalEngine.score_from_db()` — 400 1-min bars → 30-bar
  forecast → `{side, score, confidence, forecasted_return, data_age_min}`.
- `ml/kronos_gate.py` — shadow mode: logs `[SHADOW] ... would ALLOW/BLOCK`, persists verdict
  + features to signals, always returns True. Enforcing mode honors confidence ≥ 0.4.
- `ml/calibration.py` — `fill` writes realized 30-min returns into features_snapshot;
  `report` computes ALLOW-vs-BLOCK gate value on FRESH rows only. **Re-arm criterion:
  n≥30 fresh, acc≥55%** → then set `KRONOS_SHADOW_MODE=false` + restart (manual, deliberate).
- `KRONOS_CHECKPOINT` env empty = HuggingFace zero-shot; set to S3 path after fine-tuning.
- Fine-tune plan: clean data (M2.5) → spot g4dn.xlarge → Kronos-base, context 512, pred 30,
  **date-split** train/val/test (never random on time series) → checkpoint to S3 → terminate GPU.
- Three-way comparison (identical costs): ORB alone vs +zero-shot vs +fine-tuned.
  Promote only on meaningfully better Sharpe.

---

## File structure

```
dhan_algo/
├── apps/
│   ├── trader.py           Trading process (feed, runners, risk, heartbeat)
│   └── api.py              Dashboard process (REST + static, :8765)
├── engine/
│   ├── execution.py        PaperExecutor / LiveExecutor (fill confirmation)
│   ├── portfolio.py        DB-persisted positions + reconciliation
│   ├── risk.py             Sizing, daily-loss halt, kill-switch owner
│   ├── bar_builder.py      WS ticks → 1m bars → DB (5s flush)
│   ├── runner.py           Per-security poll loop (feed-first, REST fallback)
│   └── types.py            OrderIntent / Fill / Decision dataclasses
├── strategies/orb.py       Pure sync ORB (on_tick → Decision; seed_opening_range)
├── ml/
│   ├── kronos_gate.py      Shadow/enforcing gate, persists verdicts
│   └── calibration.py      fill | report  (python -m ml.calibration ...)
├── research/backtest/      engine, costs (Indian intraday stack), universe, report
├── core/                   client, token_manager, live_feed, nse_screener,
│                           kronos_signal, instruments, journal, notify, watchlist
├── kronos/                 Vendored Kronos model (MIT)
├── dashboard/              React 18 + Vite (Signals/Portfolio/System, mobile-friendly)
├── infra/                  Terraform + systemd/dhan-{trader,api}.service
├── alembic/versions/       001 → 005 (005 dropped the ohlcv_1min mirror)
├── backfill.py             Historical OHLCV CLI (checkpointed)
├── config.py               pydantic-settings Config — the only env reader
├── db.py                   SQLAlchemy engine/session
└── tests/                  71 tests (pytest -q) + GitHub Actions CI
```

---

## Running / operating

```bash
# status
~/Desktop/dhan_aws_access/connect.sh status
# logs (on agent)
tail -50 /var/log/dhan/trader.log                 # also: api.log, calibration.log
# restart (on agent)
sudo systemctl restart dhan-trader                # engine
sudo systemctl restart dhan-api                   # dashboard
# backfill monitor (on agent)
tail -f /tmp/backfill.log                         # screen -r backfill
# heartbeat sanity (mid-boot reads show the OLD process for ~45s)
cat /opt/dhan-trading/run/trader_heartbeat.json | python3 -m json.tool
```

---

## Safety rules (never override)

1. `PAPER_TRADING=true` default; live needs `.env` edit + `ALLOW_LIVE_TOGGLE=true` + restart.
2. RiskEngine owns the kill-switch; all orders route through it.
3. No live trading until the 2-year backtest passes with realistic costs.
4. Kronos is fail-open — model errors never block trades.
5. Dhan has no sandbox; treat every non-paper call as production.
6. EOD square-off is unconditional — never reintroduce a dependency on strategy state.
7. No static watchlists; screener + validation only.
8. Repo is public: no real IPs/IDs/tokens in committed files.
