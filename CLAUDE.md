# DhanAIBot — Trading Platform (agent instructions)
**Repo:** `github.com/armaanfarshori/dhan_algo` (PRIVATE — never commit IPs, account IDs, tokens, chat IDs)
**Last updated:** 2026-08-16
**Current phase:** ⚠️ **HARDWARE HANDOFF IN PROGRESS (2026-08-16).** The operator is selling the
T1700 and moving to a refurbished tiny PC. Everything durable is off-box: repo fully pushed,
DB dump + research corpus + Claude memory archives in
`s3://dhan-trading-data-155304839154/{backups,handoff}/` — see
**docs/machine-handoff-2026-08.md** for the exact restore sequence, what was backed up, and the
agenda a fresh session should pick up. Any reference below to "this box" describing the T1700 is
about to be stale; the T1700 section stays accurate as the template for what the new box must
become (the migration is a re-run of `infra/scripts/setup_local.sh` on new hardware, not a new
design). Previous phase (still the substantive state): SINGLE-HOST DEPLOYMENT (T1700) — LIVE-PATH INFRASTRUCTURE READY. The
AWS→home-server migration (PRs #100–#105, 2026-08-14) is complete: trader + dashboard +
TimescaleDB all run on one owned box, the old EC2 pair is **terminated**, and the operator has
directed the project to pursue live trading — this **supersedes** the 2026-06-21 wind-down verdict
below as the *gating condition*, without overturning what that research actually found (see the
CONCLUSION block — it is not being re-litigated). `PAPER_TRADING` is still `true` by default and
nothing trades live until the operator deliberately flips it (see *Going live* under Safety rules).
Real work remains before that flip: `.env` needs live Dhan credentials + `DASHBOARD_TOKEN`, SEC-2
(Dhan credential rotation) is still pending, DIMM4 wants a `memtest86+` pass, and the AWS
S3/SSM/EBS-snapshot salvage is an open operator decision. Full detail in the next section.

> **RESEARCH CONCLUSION (2026-06-21) — the honest verdict after exhausting the research. Kept as
> history, not as the current policy** *(superseded 2026-08-14 as the live/no-live gate by
> operator directive — see the banner above — but nobody has re-run any of this on the new box,
> so every finding below should still be treated as true until re-validated):*
> **No strategy in this repo had a validated edge after realistic frictions.** The earlier "validated
> F&O edge" framing was an **artifact of the VIX-as-weekly-IV proxy** and was falsified on real IV.
> What each test actually showed:
> - **Equity intraday (10 strategies + ORB):** all LOSE (OOS Sharpe −3 to −29). Kronos gate doesn't
>   rescue them (zero-shot +0.61 OOS only marginal; fine-tune *hurt*: v2 −3.22, v1 −4.54). `s3://…/kronos/m3/`
>   (S3 bucket — salvage status pending, see banner above).
> - **F&O iron-condor / premium-selling:** the ~4% ROM "GO" was a **VIX-proxy over-credit**. On REAL
>   option IV (Dhan rollingoption pull) the clean 2×2 (gate×IV) flips GO→NO-GO (condor v1 +16.1%→−3.3%,
>   v2 +13.2%→−1.5%). Only a thin far-OTM wide-wing corner barely survived — not a business. See
>   memory `real-iv-condor-verdict`.
> - **Options scalper (NIFTY/BANKNIFTY):** DECISIVELY negative-EV on real minute data — −₹681/scalp base,
>   negative in ALL 18 cost cells + ALL 27 regime cells; the first-15-min open-drive variant also loses
>   (−₹365 to −₹679/scalp). Built + paper-ready behind `scalper_enabled` (DEFAULT OFF). See memory
>   `scalper-backtest-verdict`.
> - **MCX futures:** no edge (`mcx-futures-backtest-result`). **Dispersion / implied-correlation:** NO-GO —
>   structurally retail-infeasible (leg slippage, single-stock illiquidity, corr→1 tail, capital), and the
>   realized-correlation *gate* is moot because the condor it would sharpen is dead. The realized-corr
>   signal itself is real (persistent, mostly dispersed) but has no retail-accessible defined-risk vehicle.
>
> **What's built & where:** real option-IV pipeline (`core/dhan_option_history.py`, rollingoption),
> backtest harnesses (`research/backtest/`), the scalper (engine/screener/orchestrator/governor + UI tab,
> all dark/off), dashboard. Multi-index intraday is still data-blocked (NIFTY-only live ingestion). The
> dual-session split is RETIRED — single `main`. **Going live means going live on top of this — no new
> edge has been found since 2026-06-21; the operator's directive is to proceed anyway, deliberately.**
> No sleeve (scalper, F&O orchestrator) is armed by this directive — it was about infra readiness, not
> a strategy pick. Treat "which strategy goes live first" as undecided.

---

## 🖥️ This is the single-host T1700 deployment

There is no more Mac/AWS split. Everything — editor, tests, backtests, the trader, the dashboard,
TimescaleDB — runs on one box: a Dell Precision T1700 (Xeon E3-1270 v3, 4c/8t, 32GB DDR3, **256GB
SSD — disk, not RAM, is the binding constraint**), Ubuntu 24.04, `Asia/Kolkata` timezone, repo at
`/home/armaanfarshori/dhan_algo`. The old AWS EC2 pair (agent + DB) is **terminated**;
`~/Desktop/dhan_algo` (the Mac clone) and `~/Desktop/dhan_aws_access/` (the SSH-key/`connect.sh`
package) no longer exist — any instruction below or in older docs that references them describes
dead infrastructure.

**`/opt/dhan-trading` is now a symlink to this checkout**, not a second clone
(`infra/scripts/setup_local.sh` step 3). That's deliberate: the systemd units, `config.py` defaults
(`BACKFILL_CHECKPOINT_PATH`), the cron block, and a pile of docs all hardcode `/opt/dhan-trading` —
a symlink makes every one of those correct as written instead of forking into a permanent local
diff against a second clone. One consequence: **there is no second `git pull` on a different
machine.** Deploying a merged change is `git pull` (if you're working from a worktree or a stale
checkout) followed by a service restart, in this same environment — see *Dev workflow* below.

**Credentials come from `.env`, hand-authored — there is no SSM here.** `setup_local.sh` seeds it
from `.env.example`, `chmod 600`s it, and never invents, fetches, or rewrites a value. Full
walkthrough: `docs/local-setup.md`. The decision record for *why* each piece landed the way it did
(symlink over retarget, `.env` over an `SSM|env` toggle, the proxy design, `JOURNAL_DB_ENABLED`,
the TCP healthcheck, IST-native cron, enabled-not-started services) is `docs/migration-2026-08.md`.

**Order placement's IP whitelist is now an egress proxy, not an Elastic IP.** Dhan still whitelists
exactly one static public IP for order-category REST calls; this box's own IP isn't it (no stable
egress off a home line / CGNAT). `DHAN_PROXY_URL` routes REST calls in `DHAN_PROXY_CATEGORIES`
(default `orders`; `all` pins everything) through an HTTP proxy — a small always-on VM whose IP
*is* the whitelisted one, reached over Tailscale (tinyproxy). Market data, historicals, and the
WebSocket feed stay **direct** — only order placement needs the borrowed identity; routing the
heavier categories through a free-tier VM would burn its bandwidth for no benefit.
`scripts/egress_check.py` (cron 09:00 IST weekdays) verifies the proxy is actually egressing from
the whitelisted IP *before* the open and alerts via Telegram if not — a dead or re-IP'd proxy fails
**silently** otherwise (aiohttp does not fall back to direct; orders get rejected mid-session, not
a loud error at boot). **Never commit the real whitelisted IP, the proxy VM's Tailscale address, or
its URL** — placeholders only in every committed doc; real values live in `.env` alone.

**Dashboard security posture changed with the move.** On AWS a VPC boundary sat between `:8765` and
the internet; here `API_BIND_HOST` defaults to `0.0.0.0` on a box that's reachable from the home
LAN and the tailnet. An unset `DASHBOARD_TOKEN` still fails **open** by design (never lock the
operator out of the kill-switch by misconfiguration) — which now means anyone who can reach the LAN
or tailnet can hit `POST /api/killswitch` or `/watchlist/refresh`. **Set `DASHBOARD_TOKEN` before
this box does anything that matters.**

**Terraform (`infra/*.tf`) is dormant, not deleted.** It describes the terminated AWS layout (VPC,
2× EC2, EIP, S3, SSM, IAM) and its remote state may no longer agree with reality now that the
instances are gone. Do not `terraform apply` against it without first reading exactly what `plan`
proposes — the S3/SSM/EBS-snapshot salvage question is still an open operator decision, and until
that's resolved the safest assumption is that the state describes resources that no longer exist.
`infra/scripts/` and `infra/systemd/` are the live parts: `setup_local.sh` (current bootstrap);
`setup_agent.sh` / `setup_db.sh` (retired AWS user-data scripts, kept for reference only — do not
run them here).

**Outstanding before any live session** (operator task list, not yet done as of 2026-08-14):
1. Fill real Dhan credentials into `.env` (`DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` / `DHAN_PIN` /
   `DHAN_TOTP_SECRET`) — the old SSM-sourced values were likely lost with the EC2 termination;
   Dhan-side TOTP re-enrollment may be required.
2. Set `DASHBOARD_TOKEN` (see above).
3. `sudo systemctl start dhan-trader dhan-api` (installed enabled-not-started by `setup_local.sh` —
   see *why* in `docs/local-setup.md`).
4. **SEC-2: rotate the Dhan PIN/TOTP/token** — still pending; was pending before this migration too.
5. `memtest86+` on DIMM4 (a mismatched replacement stick) before trusting the DB long-term.
6. Decide the AWS S3/SSM/EBS-snapshot salvage question (data may or may not be recoverable).

---

## 🤝 Multi-Claude collaboration — lanes

This repo is **PRIVATE**. Other Claude surfaces can read it and contribute, each in its lane, with
NO secrets, via **authorized private-repo access** (the Claude Projects GitHub connector and the
Claude Code GitHub App must be granted scope on `armaanfarshori/dhan_algo`). The repo (this
`CLAUDE.md` included) is the shared, always-in-sync context — keep it current and they stay current.

**ADVISORY lane — Claude Projects (claude.ai).** Connect the repo as a Project knowledge source
(GitHub connector on `armaanfarshori/dhan_algo`); the Project reasons over the synced codebase —
plans, reviews diffs, drafts code + tests, answers architecture questions. It does NOT execute
code, run tests, or open PRs by itself. It hands off **PR-ready diffs on feature branches**.

**CODE lane — Claude Code (web/CLI via the Claude GitHub App).** Clones the repo and is fully
productive without secrets:
- Edit code, run `pytest -q` (1691 tests) + `ruff`, build the dashboard, run **local** backtests,
  open **branches + PRs**. CI (Py3.12, x86+ARM, coverage, ruff) gates every PR.
- It CANNOT (and should not) touch live infra: no `.env` secrets, no sudo, no Tailscale.

**LIVE / INFRA lane — is now this box.** The AWS-era split (a separate "trusted machine" holding
`~/Desktop/dhan_aws_access/`, an SSH key, an AWS profile, a Tailscale join to a *remote* agent) is
gone because there is no longer a remote agent to reach — the T1700 **is** the machine that holds
`.env`, deploys, and would place live orders. Claude Code running here has standing sudo
authorization from the operator (memory `t1700-sudo-access`) and can restart services, run
migrations, and drive Docker directly. Tailscale is still in play, but only for the **egress
proxy** (order-path IP identity) — not for reaching a second box. The Project/Code Claude still
proposes via PR; whichever session is running directly on this box reviews, merges, and deploys —
no separate hop.

Rules for any Claude here: `PAPER_TRADING` stays `true` unless the operator has explicitly walked
through the live-flip protocol (see Safety rules); never commit IPs/IDs/tokens (private repo — but
never rely on that; no secrets in git);
branch + PR for every change (never commit straight to `main`). **The agent merges PRs itself**
(memory `agent-handles-merges` — don't wait for a human) once gated by **green CI + outside
market hours (09:15–15:40 IST, post-CAS)**. The market-hours + CI gates are safety rails, not
human-gating; only the deploy (restart services on this box) stays a deliberate step.

---

## ⚡ 2026-06-17 SESSION STATE (SUPERSEDED — see the 2026-06-20 F&O PIVOT note at the top)

> The block below is historical (backfill/M2.5/Kronos era). It's kept for context but the project has
> since pivoted F&O-focused, then (2026-08-14) migrated off AWS entirely; M2.5/backfill/Kronos are all
> DONE and the equity/Kronos edge was ruled out. None of the AWS specifics below are still true —
> see "single-host T1700 deployment" above.

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

**Note (2026-08-14):** the bare-metal PostgreSQL box and the DB-on-a-dedicated-EC2 layout described
above no longer exist. TimescaleDB now runs as a Docker Compose container on this same box
(`docker-compose.yml`, `docs/local-db.md`), and the schema went through seven more migrations after
this snapshot (008 → 014; head is now **014**, not the 007 implied elsewhere in this historical block).

## ⚡ TL;DR — platform shape

```
TWO PROCESSES on this box (systemd):
  dhan-trader  (apps/trader.py) — order flow only; heartbeat → run/trader_heartbeat.json
  dhan-api     (apps/api.py)    — dashboard :8765; reads DB + heartbeat only
       (handlers decomposed into apps/routes/{heartbeat,db,market,system}.py + _db_query helper)
PAPER mode · Kronos gate SHADOW (logs verdicts, never blocks) · 1691 tests, CI green.
main.py is GONE (Phase 1). Hermes LLM gateway RETIRED (plain Telegram via core/notify.py).

2026-08-14 — SINGLE-HOST MIGRATION shipped (PRs #100–#105; full decision record in
docs/migration-2026-08.md). Highlights:
  • AWS EC2 pair TERMINATED; trader + dashboard + TimescaleDB all now run on this T1700 box.
    /opt/dhan-trading is a SYMLINK to the checkout — no second clone to keep in sync.
  • Order-path IP whitelist: DHAN_PROXY_URL routes order-category REST through a static-IP
    HTTP proxy over Tailscale (WS feed + data stay direct); scripts/egress_check.py (cron
    09:00 IST) verifies the identity pre-open — replaces the old Elastic-IP whitelist.
  • JOURNAL_DB_ENABLED (explicit flag) replaces the old "DB_HOST == localhost means dev,
    disable journalling" heuristic — that heuristic was fatal here, where localhost genuinely
    IS the production DB; it was silently zeroing out RiskEngine's realized-loss meters.
  • Local TimescaleDB via docker-compose.yml (pinned 2.17.2-pg16, loopback-only 127.0.0.1:5432,
    TCP healthcheck — a socket-based pg_isready falsely reports healthy against initdb's
    temporary bootstrap server). Alembic 001→014 validated clean on a virgin container on
    this box: alembic_version=014, 8 hypertables, 31 tables.
  • infra/scripts/setup_local.sh — idempotent bootstrap replacing EC2 user-data; seeds .env
    from .env.example, installs systemd units enabled-not-started, installs an IST-native
    cron block (gated on the box actually being on Asia/Kolkata). Executed + verified on this
    box: 14 changes on the first run, 0 mutations on a re-run (true idempotency).
  • scripts/backup_db.sh — nightly pg_dump -Fc, integrity-gated (pg_restore --list before
    publish), rotated, optional S3 push (never fails the backup if S3 is unreachable).
  • The 2026-06-21 "no validated edge" research verdict is UNCHANGED by any of this — see the
    CONCLUSION block at the top. This migration only rebuilt the ground the platform runs on;
    the operator decided to proceed to live anyway, on top of an unchanged research record.

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
  • Schema head 007 at the time (006 = features_snapshot json→jsonb + GIN; 007 = api_usage) —
    now head 014 after F&O/MCX/scalper migrations; see Milestones below.
  • Infra: remote TF state in S3 + DynamoDB lock (dormant now — AWS terminated, see the T1700
    section above); daily EBS snapshots (DLM, also dormant); systemd StartLimitBurst +
    OnFailure=dhan-alert@; scripts/health_alert.py monitor cron (5-min); logrotate;
    TimescaleDB image pinned 2.17.2-pg16 (carried forward unchanged into docker-compose.yml).
  • CI: Py3.12 + x86/ARM matrix + coverage gate + ruff + pre-commit; CodeQL + Dependabot CLEAN.
  • Skipped: DATA-07 (equity_curve PK — risky hypertable change, ~zero value). OPS-03 SSH lockdown
    SCOPED OUT (user decision).
```

**Dev workflow:** edit → branch + PR → CI → merge → restart services. There is no second box to
`git pull` on anymore — `/opt/dhan-trading` is a symlink to this checkout, so once `main` carries
the commit (merge directly here, or `git pull` in this checkout if you're resuming from a worktree
or a stale clone), `sudo systemctl restart dhan-trader` picks up engine code and
`sudo systemctl restart dhan-api` picks up backend API code. Frontend changes still need a rebuild
first: `cd dashboard && npm run build` — this box has system Node 22 from the NodeSource apt repo
(`/usr/bin/node`, `/usr/bin/npm`) already on the default `PATH`, so no `PATH` hack is needed here
(the old AWS agent needed `~/.local/bin:~/.hermes/node/bin`; `setup_local.sh` does not install
Node/npm, so a fresh box needs it added separately) — then `sudo systemctl restart dhan-api`.

---

## Milestone status

| Milestone | Status | Notes |
|---|---|---|
| M0 — AWS infrastructure | ⚠️ RETIRED 2026-08-14 | VPC, EC2×2 (agent + DB), EIP, S3, SSM, IAM, Terraform — the EC2 instances are TERMINATED; see **Single-host migration** row below and the T1700 section at the top |
| M1 — Database schema | ✅ Done | alembic head **014** (31 tables, 8 hypertables as of the 2026-08-14 local-bootstrap verification — was 19 tables / 5 hypertables / head 005 before the F&O + scalper migrations; see `docs/local-db.md`) |
| M2 — Data pipeline | ⏳ | Historical backfill ~67% (all NSE_EQ, on the now-terminated AWS DB — status of that data pending the S3/EBS salvage decision). **Live half DONE 2026-06-12**: LiveFeed→BarBuilder→bars works (string-SecurityId fix) |
| M2.5 — Clean data replica | ❌ Next | `scripts/build_clean_db.py` exists — review for survivorship, then run after backfill (scope depends on the AWS salvage decision) |
| M3 — Backtester on real bars | ⚠️ Built, study pending | `research/backtest/` complete; the 2-year three-way study runs after M2.5 |
| M4 — Execution engine DB writes | ✅ Done | Verified across a full live session (AWS-era); journalling behavior changed 2026-08-14, see `JOURNAL_DB_ENABLED` |
| M5 — Deployment + ops | ✅ Done | systemd ×2 on this box, IST-native crons (health/egress-check/calibration/EOD/backup), Telegram alerts — re-deployed single-host 2026-08-14 (`infra/scripts/setup_local.sh`) |
| M6 — Auth layer | ❌ Schema only | `/api/mode` POST is read-only + live toggle env-gated as mitigation; `DASHBOARD_TOKEN`'s threat model got materially worse with the move off a VPC — see the T1700 section above |
| M7 — Readonly validation | ❌ | Needs M3 |
| M8 — Tiny live | ❌ | Needs M7; order-path IP whitelist is now the egress proxy (`DHAN_PROXY_URL`, `scripts/egress_check.py`) — replaces the old Elastic IP; the same Dhan 7-day change-lock applies to re-whitelisting a new IP |
| Kronos zero-shot + shadow gate | ✅ Done | OOS +0.61 on ORB — marginal/fragile; the only positive equity config |
| Kronos fine-tune (base) | ✅ Done — NO EDGE | v2/v1 fine-tuned gate HURT (OOS −3.22/−4.54); equity-Kronos ruled out |
| M3 three-way + strategy sweep | ✅ Done — NO EQUITY EDGE | 10 strategies + ORB all lose; results in `s3://…/kronos/m3/` (salvage status pending) |
| **F&O orchestration engine** | ⏳ Building | regime router → vol-gated GO premium-sellers; specs in `research/backtest/orchestrator_specs/`. NO-GO on real IV as of 2026-06-21 — unre-validated on this box |
| **F&O scalper hardening** | ⏳ Planned | DECISIVELY negative-EV on real minute data as of 2026-06-21 (see CONCLUSION); ships dark, `scalper_enabled=False` |
| Multi-index expansion | ❌ Data-blocked | NIFTY-only today; needs per-index Dhan ingestion (likely forward-only) |
| **Single-host migration (T1700)** | ✅ Done 2026-08-14 | AWS→home-server port: egress proxy (#101), `JOURNAL_DB_ENABLED` (#102), local TimescaleDB via Docker Compose (#104), nightly pg_dump backups (#103), `setup_local.sh` bootstrap (#105) — decision record `docs/migration-2026-08.md` |

**Critical path:** unchanged in substance by the migration — the 2026-06-21 CONCLUSION (no validated
edge, F&O included) still stands, and none of it has been re-tested on this box's own data. The
2026-08-14 operator directive is about being *ready* to go live, not about which sleeve (equity ORB,
F&O orchestrator, scalper) actually gets armed — that choice is still open. Equity/Kronos path is
CONCLUDED (no edge); the backfill→M2.5→M3 equity pipeline's verdict is final regardless of hosting.

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
- **IP whitelist = order placement only.** Data/historical/WebSocket work from any IP. On this
  box the whitelisted identity is borrowed via `DHAN_PROXY_URL` (an egress proxy), not an
  Elastic IP — see the T1700 section above.
- **100K Dhan API calls/day.** Rate limiter in `core/client.py`. Backfill uses REST
  `/v2/charts/intraday` directly (SDK intraday = last 5 days only).
- **NEVER `COUNT(*)` or `ORDER BY time LIMIT 1` on `bars`** (300M+ rows on the old AWS DB;
  scans/decompresses chunks, hangs minutes — the habit matters even on a smaller local dataset).
  Use `approximate_row_count()`, `hypertable_size()`, chunk-catalog ranges.
- **`signals.features_snapshot` is `jsonb`** (migration 006, GIN-indexed) — the old constraint
  here about it being plain `json` requiring an explicit `::jsonb` cast is stale; existing casts
  in `ml/calibration.py` and the gate-panel handler are redundant-but-harmless leftovers, not
  a requirement for new queries.
- **`JOURNAL_DB_ENABLED` must be true (the default) for RiskEngine's loss meters to work.**
  `core/journal.py`'s `AsyncDBBackend` only writes `runs`/`signals`/`trades`/`orders`/`fills`/
  `equity_curve` when this flag is true *and* `db_host` is non-empty; `RiskEngine.refresh_pnl()`
  reads `trades`, so a disabled journal silently pins realized P&L at zero while the trader keeps
  running. Replaces a 2026-06-16-era heuristic that inferred "disable" from `DB_HOST=="localhost"`
  — fatal on this box, where localhost genuinely **is** the production DB. See
  `docs/migration-2026-08.md`.
- **32GB RAM, but a 256GB SSD — disk is the binding constraint, not memory.** Do not re-run the
  full NSE_EQ backfill locally (~300M rows / tens of GB won't fit alongside the OS); do not disable
  the compression/retention policies (alembic 002) that keep `ticks`/`bars` bounded; do not enable
  WAL archiving. Kronos still lazy-loads on first use rather than eager-loading at startup — worth
  keeping even with headroom, since it avoids paying model-load cost on every restart.
- **Screener:** dynamic only (no static watchlist env var) — ₹200 price floor, 50K volume
  floor, candidates validated as EQUITY in segment against `instruments`; open positions
  exempt from validation (must always be manageable to exit).
- **`PAPER_TRADING=true` is default.** Never flip without explicit user intent.
- **RiskEngine owns the kill-switch.** Never bypass.
- **The old `platform_watchdog.sh` cron was REMOVED — do not re-add it** (it caused the
  June crash loop by kill -9ing a slow-booting process).
- **Repo is PRIVATE** — no secrets in committed files regardless; placeholders in docs, real
  values only in `.env` on this box (gitignored, mode 600) — there is no separate secrets
  machine anymore.

---

## Alerts & automation (Hermes retired 2026-06-11)

The Hermes LLM gateway burned its $10 OpenRouter budget in one day (the 18K-token system
prompt billed per tool step per 5-min cron). Stopped + disabled — `~/.hermes/` lived on the old
AWS agent and is gone with it. Replacement is ₹0/month: `core/notify.py` (plain Telegram bot API;
creds in `.env`).

Crontab on this box (`infra/scripts/setup_local.sh` step 9, IST-native, weekdays unless noted):

| Schedule (IST) | Job | Log |
|---|---|---|
| `*/5 * * * *` | `scripts/health_alert.py` (24/7; market-hours gating is inside the script) | `health_alert.log` |
| `0 9 * * 1-5` | `scripts/egress_check.py` — pre-open proxy identity check (new 2026-08-14) | `egress_check.log` (+ Telegram on failure) |
| `45 16 * * 1-5` | `ml.calibration fill` + `report` | `calibration.log` |
| `0 17 * * 1-5` | `scripts/eod_summary.py` | `eod_summary.log` |
| `30 2 * * *` | `scripts/backup_db.sh` — nightly pg_dump (new 2026-08-14, replaces EBS/DLM snapshots) | `backup.log` |

This replaces the old AWS crontab entirely (UTC times, a 15-min backfill watchdog that outlived its
purpose once backfill completed). The cron install is gated on this box actually being on
`Asia/Kolkata` — see `docs/local-setup.md` for the full rationale, including what happens if the
timezone doesn't match.

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
│   └── api.py               Dashboard process (REST + static, :8765)
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
├── core/                   client (Dhan REST + proxy routing), token_manager, live_feed,
│                           nse_screener, kronos_signal, instruments, journal, notify, watchlist
├── kronos/                 Vendored Kronos model (MIT)
├── dashboard/               React 18 + Vite (Signals/Portfolio/System, mobile-friendly)
├── scripts/                 health_alert.py, eod_summary.py, egress_check.py (proxy identity,
│                            new 2026-08-14), backup_db.sh (nightly pg_dump, new 2026-08-14),
│                            build_clean_db.py, finetune.py, prepare_kronos_dataset.py
├── infra/
│   ├── *.tf                 Terraform — DORMANT (describes the terminated AWS layout)
│   ├── scripts/              setup_local.sh (current bootstrap), setup_agent.sh/setup_db.sh
│   │                         (retired AWS user-data, reference only)
│   └── systemd/              dhan-{trader,api,alert@}.service
├── docker-compose.yml        Local TimescaleDB (Docker Compose; replaces the AWS bare-metal DB box)
├── alembic/versions/         001 → 014 (006 = features_snapshot jsonb+GIN; 009–013 = F&O/MCX;
│                             014 = scalper tables)
├── backfill.py               Historical OHLCV CLI (checkpointed)
├── config.py                 pydantic-settings Config — the only env reader
├── db.py                     SQLAlchemy engine/session
├── tests/                    pytest -q (1691 tests) + GitHub Actions CI (Py3.12, x86+ARM, cov, ruff)
└── apps/routes/               decomposed dashboard handlers (heartbeat, db, market, system)
```

---

## Running / operating

```bash
# status
systemctl status dhan-trader dhan-api
# logs (this box)
tail -50 /var/log/dhan/trader.log                 # also: api.log, calibration.log,
                                                    # health_alert.log, backup.log
# restart
sudo systemctl restart dhan-trader                # engine
sudo systemctl restart dhan-api                   # dashboard
# database (Docker Compose)
docker compose ps                                  # STATUS should read "Up (healthy)"
.venv/bin/alembic current                          # expect the head revision (014)
# egress identity (pre-open; also runs via cron 09:00 IST)
.venv/bin/python scripts/egress_check.py
# heartbeat sanity (mid-boot reads show the OLD process for ~45s)
cat /opt/dhan-trading/run/trader_heartbeat.json | python3 -m json.tool
```

---

## Safety rules (never override)

1. `PAPER_TRADING=true` default; live needs `.env` edit + `ALLOW_LIVE_TOGGLE=true` + restart.
2. RiskEngine owns the kill-switch; all orders route through it.
3. **Live enablement is an explicit operator decision (2026-08-14 directive), not a backtest
   gate.** The research record (2026-06-21 CONCLUSION, top of this file) found no validated edge
   on any tested strategy after realistic costs — that finding stands and is not being
   re-litigated. The operator has chosen to proceed to live anyway, deliberately, executed only
   via the `PAPER_TRADING` flip protocol (rule 1) — never as a side effect of any other change.
4. Kronos is fail-open — model errors never block trades.
5. Dhan has no sandbox; treat every non-paper call as production.
6. EOD square-off is unconditional — never reintroduce a dependency on strategy state.
7. No static watchlists; screener + validation only.
8. Repo is private — never commit real IPs/IDs/tokens (never rely on repo privacy for secrets).
9. **Egress identity must verify before any live session.** Run `scripts/egress_check.py` (or
   confirm its 09:00 IST cron ran clean) before flipping to live on any day — a dead or re-IP'd
   proxy fails order placement silently, not loudly, and the whitelisted IP itself must never be
   committed anywhere in this repo.
