# DhanAIBot — Trading Platform (agent instructions)
**Repo:** `github.com/armaanfarshori/dhan_algo` (PRIVATE — never commit IPs, account IDs, tokens, chat IDs)
**Last updated:** 2026-06-20
**Current phase:** PIVOTED → **F&O-FOCUSED.** Equity/Kronos research CONCLUDED (no edge). Building the
F&O **strategy-orchestration engine** + hardened scalper on the validated options edge. PAPER throughout.

> **PIVOT (2026-06-20) — read this first.** The project is now **F&O-focused**. The 2026-06-20 backtest
> sweep settled the equity question: **10 intraday strategies + ORB all LOSE** (OOS Sharpe −3 to −29,
> none beats ORB; ORB itself loses), and the **Kronos gate does not rescue them** (zero-shot +0.61 OOS
> is the only marginal positive; fine-tuning *hurt*: v2 −3.22, v1 −4.54). Results in
> `s3://…/kronos/m3/`. The **validated edge is defined-risk options premium-selling, vol-gated**:
> iron_condor (3.91% ROM GO), bull_put_spread (7.19% GO), credit_put_spread (2.70%), broken_wing_condor
> (2.67%) — and the `ml/fno_vol_gate.py` (k≈0.9) gate **ADDS** edge on options (opposite of equity).
>
> **NEXT BUILD:** a regime-aware **Strategy Orchestration Engine** (`research/backtest/fno_orchestrator.py`)
> that picks which GO strategy to deploy per cycle/index, vol-gated, defined-risk only — plus a hardened
> options scalper. Full spec set on `main`: `research/backtest/orchestrator_specs/` (10) +
> `strategies/scalper_specs/` (10). Multi-index (BANKNIFTY/FINNIFTY/MIDCPNIFTY/SENSEX/BANKEX) is
> **data-blocked** — platform is NIFTY-only; expansion needs per-index Dhan ingestion (likely
> forward-only). The scalper is **un-backtestable** (no intraday option data) → forward-paper validation.
>
> Edge is **PRELIMINARY** (VIX-as-weekly-IV proxy, close-not-FSP, expiry-only/tail-blind) → real-IV
> forward paper-log is the truth test before any live talk. The dual-session (fno+finetune) split is
> **RETIRED** — single session, single `main`. The live equity engine stays deployed (PAPER) but is no
> longer the focus.

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

**Terraform — SINGLE working folder = `infra/` in THIS repo** (consolidated 2026-06-17; the
old `~/Documents/codecode/DhanAIBot/infra` clone is RETIRED). State is in S3
(`s3://dhan-trading-tfstate-<acct>/dhan-trading/terraform.tfstate`, versioned) + DynamoDB lock
`dhan-trading-tflock`; partial backend (bucket name in gitignored `backend.hcl`, see
`infra/backend.hcl.example`). The gitignored secrets (`terraform.tfvars`, `backend.hcl`,
`aws_outputs.local`) are backed up to `s3://<tfstate-bucket>/secrets/` (AES256, versioned) —
sync with `infra/secrets-sync.sh push|pull` (on a fresh clone, `pull` to restore). Run from
`infra/`: `terraform init -backend-config=backend.hcl`.
ALWAYS `terraform plan` and verify NO `aws_instance`/`aws_eip` replace/destroy before `apply`
(an `associate_public_ip_address` drift once tried to destroy the live agent — now in
`ignore_changes`; the agent EIP also has `prevent_destroy`). `infra/apply.sh` is guarded to
NEVER auto-destroy when state already has live resources. See memory `terraform-state-and-apply`.

---

## 🤝 Multi-Claude collaboration — lanes

This repo is **PRIVATE**. Other Claude surfaces can read it and contribute, each in its lane, with
NO secrets or AWS access, via **authorized private-repo access** (the Claude Projects GitHub
connector and the Claude Code GitHub App must be granted scope on `armaanfarshori/dhan_algo`). The
repo (this `CLAUDE.md` included) is the shared, always-in-sync context — keep it current and they
stay current.

**ADVISORY lane — Claude Projects (claude.ai).** Connect the repo as a Project knowledge source
(GitHub connector on `armaanfarshori/dhan_algo`); the Project reasons over the synced codebase —
plans, reviews diffs, drafts code + tests, answers architecture questions. It does NOT execute
code, run tests, or open PRs by itself. It hands off **PR-ready diffs on feature branches** for
Claude Code or the trusted machine to test + deploy. Set the Project's custom instructions to
point at this `CLAUDE.md` as source-of-truth + the rules below.

**CODE lane — Claude Code (web/CLI via the Claude GitHub App).** Clones the repo and is fully
productive without secrets:
- Edit code, run `pytest -q` (235 tests) + `ruff`, build the dashboard, run **local** backtests,
  open **branches + PRs**. CI (Py3.12, x86+ARM, coverage, ruff) gates every PR.
- It CANNOT (and should not) touch live infra: no SSH key, no AWS creds, no Tailscale.

The **LIVE / INFRA lane** stays on the trusted machine that holds `~/Desktop/dhan_aws_access/`
(SSH key, AWS profile `dhan-terraform`, Tailscale) — deploys (`sudo git pull` + restart on the
agent), terraform applies, DB work. Order placement is locked to the agent's whitelisted EIP, so
live ops MUST run from there. The Project/Code Claude proposes via PR; the trusted machine
reviews, merges, and deploys.
- If a contributor genuinely needs AWS/DB (e.g. heavy backtests on `dhan_clean`), share the access
  package **out of band** (never in the repo) + Tailscale-join their box; pull TF secrets with
  `infra/secrets-sync.sh pull`. Default to NOT doing this — the PR lane covers most work.

Rules for any Claude here: PAPER_TRADING stays `true`; never commit IPs/IDs/tokens (private repo —
but never rely on that; no secrets in git);
branch + PR for every change (never commit straight to `main`). **The agent merges PRs itself**
(memory `agent-handles-merges` — don't wait for a human) once gated by **green CI + outside
market hours (09:15–15:30 IST)**. The market-hours + CI gates are safety rails, not
human-gating; only the deploy (`sudo git pull` + restart on the agent) stays a deliberate
trusted-machine step.

---

## ⚡ 2026-06-17 SESSION STATE (SUPERSEDED — see the 2026-06-20 F&O PIVOT note at the top)

> The block below is historical (backfill/M2.5/Kronos era). It's kept for context but the project has
> since pivoted F&O-focused; M2.5/backfill/Kronos are all DONE and the equity/Kronos edge was ruled out.

```
BACKFILL COMPLETE (NSE_EQ 9470/9470). The */15 backfill watchdog cron is RETIRED;
  a single post-close run (12:00 UTC / 17:30 IST weekdays) remains as a safety net.
DB MIGRATED off Docker → bare-metal PostgreSQL 16 + TimescaleDB 2.27.2 (systemd), data
  dir /data/timescaledb/pgdata. DB box was temporarily r7g.2xlarge (64GB) for M2.5; to be
  downgraded → t4g.medium + gp3 IOPS 16000/1000 → 3000/125 (via `terraform apply`, AFTER M2.5).
AGENT t4g.small (2GB) → to be upgraded to t4g.large (8GB) permanently (Kronos-base headroom),
  same `terraform apply`. NOTE: apply STOPS the agent (kills the M2.5 build) — so apply only
  after M2.5 finishes. EIP survives stop/start; Dhan whitelist unaffected.
M2.5 CLEAN-DB BUILD RUNNING on the agent: scripts/build_clean_db.py --transform --export
  (6 parallel workers) → dhan_clean.bars (1,707 liquid NSE_EQ names) → exports per-security
  Parquet to s3://<bucket>/kronos/training-data/. Log: /tmp/m25_build.log.
S3 PIPELINE WIRED end-to-end (branch feat/s3-pipeline-wiring): clean→S3→prepare_kronos_dataset
  (--timeframe 1min|5min A/B)→finetune.py (--upload-s3)→KRONOS_CHECKPOINT s3:// loads in the
  live gate (KronosSignalEngine syncs s3→local, fail-open). kronos_model standardized → base.
  M3 backtester now reads dhan_clean (config.backtest_db_url). tests 235 + new s3-wiring tests.
OPEN BRANCHES pending merge after M2.5 verifies: feat/m25-transform-streaming,
  feat/s3-pipeline-wiring, chore/infra-single-source, docs/status-refresh-2026-06-17.
TRADING WAS HALTED for the day by the user (safe to do infra resizes). PAPER mode unchanged.
```

## ⚡ TL;DR — platform shape

```
TWO PROCESSES on the agent (systemd):
  dhan-trader  (apps/trader.py) — order flow only; heartbeat → run/trader_heartbeat.json
  dhan-api     (apps/api.py)    — dashboard :8765; reads DB + heartbeat only
       (handlers decomposed into apps/routes/{heartbeat,db,market,system}.py + _db_query helper)
PAPER mode · Kronos gate SHADOW (logs verdicts, never blocks) · 235 tests, CI green.
main.py is GONE (Phase 1). Hermes LLM gateway RETIRED (plain Telegram via core/notify.py).

2026-06-16 — FULL SDLC REMEDIATION shipped (64/65 checklist items, 11 PRs, backfill never
disturbed). Highlights (see memory `terraform-state-and-apply`, `credential-scrub-2026-06-16`):
  • Security: REAL Dhan creds were leaked in old git history → history REWRITTEN + force-pushed
    (agent clone was hard-reset). Shared-secret auth on POST /api/killswitch + /watchlist/refresh
    (DASHBOARD_TOKEN, X-Dashboard-Token/Bearer, fail-open if unset). /postback HMAC. /api/status
    masks client_id. Deps pinned. *** SEC-2: rotate Dhan PIN/TOTP/token — STILL PENDING (yours). ***
  • FEAT-01/02: dashboard System-tab API rate-limit spend panel, backed by cross-process accounting
    (core/api_usage.py + api_usage table; trader/api/backfill flush deltas; GET /api/rate-limits).
  • DATA: one DB engine/process; open_trade_id threaded entry→exit; BarBuilder is the single
    tick→candle aggregator; get_adv uses a 30-day time bound (no ORDER BY on bars).
  • Schema head 007 (006 = features_snapshot json→jsonb + GIN; 007 = api_usage).
  • Infra: remote TF state in S3 + DynamoDB lock; daily EBS snapshots (DLM); systemd
    StartLimitBurst + OnFailure=dhan-alert@; scripts/health_alert.py monitor cron (5-min);
    logrotate; TimescaleDB image pinned 2.17.2-pg16.
  • CI: Py3.12 + x86/ARM matrix + coverage gate + ruff + pre-commit; CodeQL + Dependabot CLEAN.
  • Skipped: DATA-07 (equity_curve PK — risky hypertable change, ~zero value). OPS-03 SSH lockdown
    SCOPED OUT (user decision).
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
| M2 — Data pipeline | ⏳ | Historical backfill ~67% (all NSE_EQ). **Live half DONE 2026-06-12**: LiveFeed→BarBuilder→bars works (string-SecurityId fix) |
| M2.5 — Clean data replica | ❌ Next | `scripts/build_clean_db.py` exists — review for survivorship, then run after backfill |
| M3 — Backtester on real bars | ⚠️ Built, study pending | `research/backtest/` complete; the 2-year three-way study runs after M2.5 |
| M4 — Execution engine DB writes | ✅ Done | Verified across a full live session |
| M5 — Deployment + ops | ✅ Done | systemd ×2, weekday crons (watchdog/calibration/EOD), Telegram alerts |
| M6 — Auth layer | ❌ Schema only | `/api/mode` POST is read-only + live toggle env-gated as mitigation |
| M7 — Readonly validation | ❌ | Needs M3 |
| M8 — Tiny live | ❌ | Needs M7; Elastic IP whitelisted at Dhan 2026-06-12 (7-day change lock) |
| Kronos zero-shot + shadow gate | ✅ Done | OOS +0.61 on ORB — marginal/fragile; the only positive equity config |
| Kronos fine-tune (base) | ✅ Done — NO EDGE | v2/v1 fine-tuned gate HURT (OOS −3.22/−4.54); equity-Kronos ruled out |
| M3 three-way + strategy sweep | ✅ Done — NO EQUITY EDGE | 10 strategies + ORB all lose; results in `s3://…/kronos/m3/` |
| **F&O orchestration engine (NEW FOCUS)** | ⏳ Building | regime router → vol-gated GO premium-sellers; specs in `research/backtest/orchestrator_specs/` |
| **F&O scalper hardening** | ⏳ Planned | honest positive-EV + daily governor; forward-paper validation (un-backtestable) |
| Multi-index expansion | ❌ Data-blocked | NIFTY-only today; needs per-index Dhan ingestion (likely forward-only) |

**Critical path (NEW): F&O orchestrator MVP (NIFTY) → backtest vs single-strategy → real-IV forward
paper-log → (user-gated) defined-risk small-live.** Equity/Kronos path is CONCLUDED (no edge).
The backfill→M2.5→M3 equity pipeline is complete and its verdict (no equity intraday edge) is final.

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
- **Repo is PRIVATE** — no secrets in committed files regardless; placeholders in docs, real values
  only in `~/Desktop/dhan_aws_access/`.

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
├── tests/                  pytest -q + GitHub Actions CI (Py3.12, x86+ARM, cov, ruff)
└── apps/routes/            decomposed dashboard handlers (heartbeat, db, market, system)
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
8. Repo is private — never commit real IPs/IDs/tokens (never rely on repo privacy for secrets).
