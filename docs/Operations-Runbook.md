# Operations Runbook

> **Historical (pre-2026-08-14).** This entire runbook describes day-2 operations for the AWS
> two-EC2-instance deployment, which is now **terminated** — see `CLAUDE.md`'s "single-host T1700
> deployment" section and `docs/migration-2026-08.md` for what replaced it. `~/Desktop/dhan_aws_access/`
> no longer exists; there is no second box to SSH into; the DB is a local Docker container, not a
> separate VPC-only EC2 instance. Kept as-is rather than rewritten wholesale — the general shape
> (kill switch, mode changes, troubleshooting patterns) still applies on the T1700 box with `sudo`
> run locally instead of over SSH, but every AWS-specific command below (Elastic IP, S3, DLM,
> Terraform paths) is dead. For current operations, see `docs/local-setup.md`, `docs/local-db.md`,
> and `docs/local-db-backups.md`.

Day-2 operations for the AWS deployment. Conventions: the agent EC2 runs both
services and the backfill; the DB EC2 is reachable only from inside the VPC.
Real IPs, account IDs, and tokens live in `~/Desktop/dhan_aws_access/`, never here.

---

## Quick-reference: SSH helpers (Mac)

```bash
~/Desktop/dhan_aws_access/connect.sh agent        # SSH to agent EC2
~/Desktop/dhan_aws_access/connect.sh dashboard    # SSH tunnel → http://localhost:8765
~/Desktop/dhan_aws_access/connect.sh trader-log   # tail trader.log
~/Desktop/dhan_aws_access/connect.sh status       # systemctl status + heartbeat
~/Desktop/dhan_aws_access/connect.sh help         # full list
```

---

## Service management

```bash
# status
systemctl is-active dhan-trader dhan-api
ss -tlnp | grep 8765

# restart
sudo systemctl restart dhan-trader      # trading engine
sudo systemctl restart dhan-api         # dashboard/API

# logs
tail -50 /var/log/dhan/trader.log
tail -50 /var/log/dhan/api.log
tail -50 /var/log/dhan/calibration.log
tail -50 /var/log/dhan/health_alert.log
tail -50 /var/log/dhan/eod_summary.log
```

**Heartbeat sanity** (what the dashboard's status spine reads):

```bash
cat /opt/dhan-trading/run/trader_heartbeat.json | python3 -m json.tool
```

> Boot takes ~45–60 s (screener query). Right after a restart the file still
> belongs to the *old* process — do not trust a verification read until
> `uptime_seconds` confirms the new one.

A healthy market-hours heartbeat shows: `feed.connected: true`,
`bars.bars_written` climbing, every strategy with `or_locked: true` (or an
explicit no-range state), and `risk.halted: false`.

**systemd restart limits:** both `dhan-trader` and `dhan-api` are configured
with `StartLimitIntervalSec=300` / `StartLimitBurst=5`. After five crashes in
five minutes the unit enters a failed state and will not restart automatically
— an `OnFailure` Telegram alert fires immediately. To reset:

```bash
sudo systemctl reset-failed dhan-trader   # or dhan-api
sudo systemctl start dhan-trader
```

---

## Deploying changes

```bash
# Mac: push the branch
git push

# On the agent
cd /opt/dhan-trading && sudo git pull --ff-only
# engine code changed?
sudo systemctl restart dhan-trader
# frontend changed?
cd dashboard && PATH="$HOME/.local/bin:$HOME/.hermes/node/bin:$PATH" npm run build
sudo systemctl restart dhan-api
```

**If git pull fails** (e.g. after a history rewrite or force-push to main):

```bash
cd /opt/dhan-trading
sudo git fetch origin
sudo git reset --hard origin/main
```

Restarting the trader mid-session is safe by design: positions reconcile from
the DB, opening ranges reseed from REST intraday bars, and the EOD square-off
is unconditional. Still, prefer deploying outside 09:15–15:30 IST when possible.

**Reinstalling systemd units** (after editing `infra/systemd/*.service`):

```bash
sudo cp infra/systemd/dhan-trader.service   /etc/systemd/system/
sudo cp infra/systemd/dhan-api.service      /etc/systemd/system/
sudo cp infra/systemd/dhan-alert@.service   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart dhan-trader dhan-api
```

---

## Kill switch

```bash
curl -X POST http://localhost:8765/api/killswitch    # via tunnel, or:
touch /opt/dhan-trading/run/killswitch               # by hand on the agent
```

The trader's risk loop detects the file within ~10 s, halts, flattens all
positions, and sends a Telegram alert. To resume: remove the file and restart
`dhan-trader`.

```bash
rm /opt/dhan-trading/run/killswitch
sudo systemctl restart dhan-trader
```

---

## Mode changes (paper ↔ live)

Edit `/opt/dhan-trading/.env` (`PAPER_TRADING`, and for live also
`ALLOW_LIVE_TOGGLE=true`), then `sudo systemctl restart dhan-trader`.
`POST /api/mode` is read-only by design until the auth layer (M6) exists.

---

## DB migration (Alembic)

Schema head is **014** (was 007 when this runbook was written; see
`docs/local-db.md` for the current migration set). `alembic/env.py` reads
`DB_*` from environment variables — source `.env` first, guarded so a missing
file can't leave `allexport` stuck on for the rest of the shell:

```bash
cd /opt/dhan-trading
[ -f .env ] && { set -a; source .env; set +a; }
.venv/bin/alembic upgrade head
```

---

## Backfill operations

```bash
screen -r backfill                          # attach (Ctrl-A D to detach)
tail -f /tmp/backfill.log                   # progress
cat /opt/dhan-trading/backfill_ckpt_NSE_EQ.json   # checkpoint {index, total}
```

- Checkpointed: kill + relaunch resumes where it left off.
- A cron watchdog (every 15 min, weekdays) restarts the screen if it died and
  pings Telegram.
- **DH-901 token errors flooding the log** — the in-memory token went stale on
  a multi-day run: restart the backfill process (it re-reads the token store).
- **DH-904 rate-limit errors** — `charts/intraday` tolerates ~1 req/s; space
  calls and retry with backoff.
- Skipped chunks from transient timeouts are recorded; run the gap-scan script
  after completion for a repair pass (see PLAN-02 below).

---

## Scheduled automation (agent crontab, weekdays)

All times are UTC; `IST = UTC+5:30`.

| UTC cron | IST | Job |
|---|---|---|
| `*/5 * * * *` | every 5 min, all hours | `scripts/health_alert.py` → `/var/log/dhan/health_alert.log` |
| `*/15 * * * 1-5` | every 15 min, weekdays | Backfill watchdog — restart screen + Telegram alert if dead |
| `15 11 * * 1-5` | 16:45 IST, weekdays | `python -m ml.calibration fill && report` → `calibration.log` |
| `30 11 * * 1-5` | 17:00 IST, weekdays | `scripts/eod_summary.py` → Telegram + `eod_summary.log` |
| `0 2 * * *` | 07:30 IST daily | `db_backup.sh` — pg_dump → S3 (on DB box, `/etc/cron.d/timescaledb-backup`) |

The retired LLM ops agent must stay retired — plain cron + Telegram does this
job at zero cost. Likewise the old process-watchdog cron (which kill-9'd slow
boots) must never be re-added; systemd owns restarts.

---

## Alerting coverage

All alerts land in the Telegram channel configured by `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID` in `.env`.

| Source | Condition | Timing |
|---|---|---|
| `dhan-trader` (engine) | Kill-switch triggered / risk halt | Immediately on event |
| `dhan-alert@.service` (OPS-08) | `dhan-trader` or `dhan-api` enters `failed` state | On any non-zero exit |
| `scripts/health_alert.py` (cron `*/5`) | See table below | All hours (market gate inside script) |
| `scripts/eod_summary.py` (cron 17:00 IST) | Daily trades, P&L, gate verdicts | Weekdays after square-off |
| Backfill watchdog (cron `*/15`, weekdays) | Backfill screen dead | While backfill runs |
| `db_backup.sh` (OPS-09) | Backup failure on DB box | After nightly pg_dump |

### `health_alert.py` — conditions monitored

| Condition key | Trigger | Market-hours only? |
|---|---|---|
| `heartbeat_stale` | `run/trader_heartbeat.json` missing or `ts` > 90 s old | Yes (09:15–15:30 IST, weekdays) |
| `feed_down` | `feed.connected == false` in heartbeat | Yes |
| `risk_halted` | `risk.halted == true` in heartbeat | No (catches overnight halts too) |
| `disk_full` | `/` usage > 80% (`shutil.disk_usage`) | No |
| `log_errors` | New `CRITICAL` or `REJECTED` lines in `/var/log/dhan/trader.log` since last run | No |

**De-duplication:** state is persisted in `run/health_alert_state.json`.
Single-condition checks alert once on the False→True edge and send a CLEARED
message on recovery; a byte offset into `trader.log` prevents re-alerting the
same lines on the next run.

**Run manually (safe — prints only, no Telegram):**

```bash
cd /opt/dhan-trading
python scripts/health_alert.py --dry-run
```

---

## Backups and DR

### Nightly pg_dump → S3 (DB box)

`/usr/local/bin/db_backup.sh` runs at 02:00 UTC (`/etc/cron.d/timescaledb-backup`).
It streams `pg_dump -Fc` directly to `s3://<bucket>/db-backups/`. On failure it
sends a Telegram alert via raw API (independent of the Python stack). S3
lifecycle rule expires backups after 365 days.

```bash
# Trigger manually on DB box
sudo /usr/local/bin/db_backup.sh
tail -20 /var/log/db_backup.log
```

### EBS snapshots via AWS DLM (OPS-09)

A DLM lifecycle policy (state `ENABLED`) takes daily snapshots of the
TimescaleDB data volume (tagged `Backup=dlm`) at 02:00 UTC with 7-day
retention. This is the block-level backstop independent of pg_dump.

```bash
# Verify on Mac
aws dlm get-lifecycle-policies --profile dhan-terraform
```

### Restore from pg_dump

```bash
# On DB box — list available dumps
aws s3 ls s3://<bucket>/db-backups/ | sort | tail -5
# Download and restore
aws s3 cp s3://<bucket>/db-backups/<dump>.dump /tmp/
# Bare-metal restore (no Docker):
sudo -u postgres pg_restore -d dhan_trading \
  --clean --if-exists /tmp/<dump>.dump
```

---

## Database operations

```bash
# size / row estimates — ALWAYS catalog queries on hypertables:
#   SELECT approximate_row_count('bars');
#   SELECT hypertable_size('bars');
#   SELECT * FROM timescaledb_information.chunks WHERE hypertable_name='bars';
# NEVER: SELECT COUNT(*) FROM bars; or ORDER BY time LIMIT 1
# (full scan / chunk decompression — minutes of hang under backfill load)
```

The DB EC2 runs with a swapfile and bounded `maintenance_work_mem` — history
shows that an unbounded maintenance task with no swap OOM-kills Postgres during
compression.

TimescaleDB image is **pinned** at `timescale/timescaledb:2.17.2-pg16` in
`infra/scripts/setup_db.sh`. Do not change the tag without a tested upgrade plan.

---

## Terraform remote state (OPS-02)

State is stored in S3 (`dhan-trading/terraform.tfstate`, versioned + encrypted)
with a DynamoDB lock table (`dhan-trading-tflock`). The bucket name embeds the
AWS account ID and must not be committed to this repo (private — but never rely on that).

**Working clone for Terraform:** `~/Documents/codecode/Tessera/infra`
(holds `terraform.tfvars` + `backend.hcl`). Do **not** run Terraform from the
Mac's `~/Desktop/dhan_algo/infra` — it lacks those private files.

**Init:**

```bash
cd ~/Documents/codecode/Tessera/infra
# First time (local → S3 migration):
terraform init -migrate-state -backend-config=backend.hcl
# Subsequent inits:
terraform init -backend-config=backend.hcl
```

**backend.hcl.example** (in-repo) shows the required key. Copy to `backend.hcl`
and fill in the real bucket name.

**Before every apply:**

```bash
terraform plan
# Carefully review any aws_instance or aws_eip replace/destroy lines.
# A drift on associate_public_ip_address once forced a destructive agent
# replacement — this is now guarded by lifecycle.ignore_changes.
terraform apply
```

---

## WARNING — Elastic IP and the 7-day Dhan whitelist lock

The agent EC2's Elastic IP is whitelisted at Dhan for **order placement**. Dhan
enforces a **7-day change lock** — once you register an IP you cannot change it
for 7 days.

**Releasing or replacing the EIP means zero live order capability for up to
7 days** while you wait for re-whitelisting.

Rules:

- **Never release the EIP while in live trading mode** (or planning to be in
  live mode within the next 7 days).
- Data APIs, historical REST calls, and the WebSocket feed are **not** affected
  — the IP whitelist applies to order placement only.
- If the EIP must change, submit the new IP via the Dhan DevPortal immediately
  after the change and budget the full 7-day window before live orders resume.
- Paper trading is unaffected by IP changes.

---

## Troubleshooting quick table

| Symptom | First check | Likely cause / fix |
|---|---|---|
| Dashboard offline banner | `systemctl is-active dhan-trader`; heartbeat age | Trader down or booting; check `trader.log` |
| `dhan-trader` in `failed` state | `journalctl -u dhan-trader -n 50`; Telegram alert should have fired | Crashed 5× in 5 min; `systemctl reset-failed` then fix root cause |
| Feed connected but `bars_written: 0` | Heartbeat `bars` section during market hours | Subscription problem — Dhan v2 requires **string** SecurityIds; ints subscribe silently and stream nothing |
| Every gate verdict `STALE` / `age=None` | Live bars landing for today? | BarBuilder not receiving ticks — same SecurityId type issue |
| Dashboard frozen but API answers | `api.log`; slow query in executor | A heavy DB query starved file serving — keep analytics cached + in the dedicated pool |
| `DH-904` rate-limit errors | Which endpoint? | `charts/intraday` tolerates ~1 req/s — space calls, retry with backoff |
| `DH-901` token errors in backfill | Backfill runtime? | In-memory token stale on multi-day run — restart the backfill process |
| Trader restart leaves positions unmanaged | Heartbeat strategies: `or_locked` | Should self-heal via OR seeding; EOD square-off is the unconditional backstop |
| Postgres down / OOM | `systemctl status postgresql` on DB box; `dmesg` | `sudo systemctl restart postgresql`; check swap + `maintenance_work_mem`; never reintroduce uncompressed duplicate tables |
| Backup failure Telegram alert | `/var/log/db_backup.log` on DB box | Check S3 permissions, IAM role, disk space; re-run manually |

---

## Monitoring checklist (during market hours)

1. Status spine green: ENGINE uptime ticking, FEED connected, mode badge correct.
2. Heartbeat `bars_written` increasing every flush.
3. Gate verdicts arriving with small `data_age_min`.
4. Equity curve drawing on the Portfolio tab.
5. Telegram silent (alerts fire on halt/watchdog events — silence is good).
6. `health_alert.py` log not showing any `ALERT` lines.

---

## Log rotation

Managed by `/etc/logrotate.d/dhan-trading` (installed by the bootstrap script):
daily rotation, 14-day retention, `compress` + `delaycompress`, `copytruncate`
(safe for services writing to open file descriptors). No manual intervention
needed unless `/var/log/dhan/` fills the disk — that will trigger a
`disk_full` health alert.

---

## PLAN-02 — Gap-scan and repair pass after backfill completes

The historical backfill skips chunks that time out rather than hanging
indefinitely. Approximately **13.5K chunks were skipped** during the initial
NSE_EQ run, leaving 90-day gaps for affected securities. These must be repaired
before M2.5 (clean DB build) and M3 (2-year backtest), as missing bars distort
indicators and produce misleading Sharpe numbers.

### Procedure

1. **Run the gap scan** after the backfill checkpoint reaches 100%:

   ```bash
   python hermes_skills/dhan/gap_scan/scripts/scan_gaps.py
   ```

   Outputs `(security_id, missing_date_range)` pairs. Uses chunk-catalog
   queries — never `COUNT(*)`.

2. **Re-fetch missing chunks** using backfill with narrowed date ranges:

   ```bash
   python backfill.py --ids <comma-separated-security-ids> --from <gap-start> --to <gap-end>
   ```

   Repeat in batches. The backfill is idempotent (safe upsert).

3. **Verify** by re-running the gap scan and confirming empty output (or only
   suspended/delisted securities that genuinely have no data).

4. **Proceed to M2.5** (`scripts/build_clean_db.py`) only after the gap scan
   is clean. An incomplete raw layer produces survivorship-biased equity curves.
