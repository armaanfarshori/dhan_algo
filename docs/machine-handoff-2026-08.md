# Machine handoff — T1700 → tiny PC (2026-08-16)

The operator is selling the T1700 and replacing it with a refurbished tiny PC.
This file records what was preserved, where it lives, how to rebuild, and what
the project was in the middle of — written so a fresh Claude session on the new
box (or anywhere) can resume without the old machine.

## What was preserved, and where

All in the private bucket `s3://dhan-trading-data-155304839154/` (AWS account
155304839154, ap-south-1; the `terraform-deployer` IAM user's keys work):

| Object | What it is |
|---|---|
| `backups/dhan_trading_<latest>.dump` | `pg_dump -Fc` of the full TimescaleDB (`dhan_trading`, ~8.9 GB live → ~3.7 GB dump). Contains everything: 302M 1-min bars (1,674 NSE names, 2yr), 772K daily bars, NIFTY 1-min 5yr + India VIX (`index_bars`), the 5yr ±10 option fan (`option_chain_snapshot`, correctly Tuesday-keyed post PR #114), `option_atm_iv`, `cas_surprise`, and all engine tables at alembic head **015**. |
| `handoff/research-2026-08-16.tar.zst` | The entire `~/dhan_data/research/` corpus (695 MB → 195 MB): every research report (N1/E1/classic-20/wide-wing verdicts), evidence packages, panels (`e1_panel.parquet`), CSVs. |
| `handoff/claude-memory-2026-08-16.tar.gz` | `~/.claude/projects/-home-armaanfarshori-dhan-algo/memory/` — the agent's persistent memory (research state, directives, infra facts). |
| `kronos/training-data/` (pre-existing) | 1,675 per-security 1-min parquet files, 2021-06 → 2026-06 (5yr) — the mirror of `~/dhan_data/s3-archive/`. Never deleted; no re-upload was needed. |

**NOT in S3, must travel by hand (secrets):** `.env` (mode 600, ~100 keys —
Dhan creds, Telegram, `DASHBOARD_TOKEN`, proxy URL, DB password, `S3_BUCKET`)
and `run/dhan_token.json` (current access token — or just re-generate via
PIN+TOTP on the new box). Copy them off the T1700 before wiping it, or accept
re-enrolling Dhan TOTP. SEC-2 (credential rotation) was already pending —
a machine swap is the natural moment to do it.

## Rebuild sequence on the new box

1. Ubuntu 24.04, timezone `Asia/Kolkata`, Docker + compose, Node 22
   (NodeSource), Python 3.12, awscli, Tailscale (join the tailnet — the egress
   proxy VM is unchanged and unaffected by this swap).
2. `git clone git@github.com:armaanfarshori/dhan_algo.git
   ~/dhan_algo && cd ~/dhan_algo`
3. `infra/scripts/setup_local.sh` — idempotent: venv, `/opt/dhan-trading`
   symlink, systemd units (enabled-not-started), IST cron block (health,
   egress-check 09:00, calibration, EOD, CAS capture 16:10, backup 02:30).
4. Restore `.env` (hand-carried), `chmod 600 .env`.
5. `docker compose up -d` (TimescaleDB 2.17.2-pg16, loopback-only), then
   restore the dump:
   `aws s3 cp s3://dhan-trading-data-155304839154/backups/<latest>.dump . &&
   docker compose exec -T timescaledb pg_restore -U trader -d dhan_trading --no-owner <file>`
   Verify: `set -a && . ./.env && .venv/bin/alembic current` → 015;
   `approximate_row_count('bars')` ≈ 302M. (Timescale dumps need the
   extension pre-created — `docs/local-db.md` has the exact restore notes.)
6. Restore research + memory:
   `aws s3 cp .../handoff/research-2026-08-16.tar.zst - | zstd -d | tar -x -C ~/dhan_data`
   and untar the memory archive into
   `~/.claude/projects/-home-armaanfarshori-dhan-algo/`.
7. `cd dashboard && npm ci && npm run build`; `pytest -q` (expect green —
   note the known local-only env-leakage failures in `test_api_handlers` /
   `test_egress_proxy` if a real `DASHBOARD_TOKEN` is in the environment).
8. `sudo systemctl start dhan-trader dhan-api`; run
   `scripts/egress_check.py` before any live-path use.

## Agenda a fresh session should pick up

- **N2 CAS-surprise capture** (`cas_surprise`, cron 16:10 IST) — accumulate to
  ~60 sessions (≈Nov 2026), then run the pre-registered decision test. Every
  day the box is offline is a lost session; restore promptly.
- **N3 CAS feed audit** — `scripts/cas_feed_audit.py` observationally during
  15:05–15:45 on any live session (never yet run).
- **E8 pairs clean re-run** — the sole classic-20 survivor (net SR 1.35, OOS
  1.20); needs external sector data + duplicate-cohort suppression +
  fixed-capital normalization before candidacy.
- **E1 gap continuation** — parked; re-test only after 120 forward sessions of
  gated net-positive (nothing to do until ~Feb 2027).
- **Corporate-action-adjusted bhavcopy panel** — highest-leverage data build;
  unblocks PEAD (E4) and a proper momentum re-look.
- **Operator decisions:** which sleeve (if any) goes live first; SEC-2
  rotation; AWS residuals (the tfstate bucket + this data bucket are the only
  AWS things left costing money).
- **Hygiene:** slow-shutdown SIGTERM→SIGKILL on `dhan-trader` restarts trips
  a spurious `dhan-alert@`; local test env-leakage (above); `memtest86+` is
  moot once the T1700 is gone.

The research record is closed and honest: no validated live edge as of
2026-08-16 (N1 falsified, E1 real-gross/parked, 19/20 classics dead, premium
selling comprehensively falsified on real prices). N2 is the only armed
forward experiment. Do not re-test closed strategies without new data or a new
mechanism — the CONCLUSION block in `CLAUDE.md` and the reports in the
research archive are the record.

## S3 cost of the preserved set

~21 GB total in Standard storage ≈ **$0.53/month** (~₹45) at ap-south-1
Standard ($0.025/GB-mo). Uploads were free (ingress). If the bucket is meant
as cold insurance rather than working storage, a lifecycle rule to Glacier
Deep Archive drops it to ~$0.04/month, at the cost of 12-hour retrieval and
90-day minimum billing — reasonable once the new box is verified restored.
