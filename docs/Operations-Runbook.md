# Operations Runbook

Day-2 operations for the AWS deployment. Conventions: the agent EC2 runs both services and the backfill; the DB EC2 is reachable only from inside the VPC. Real IPs/identifiers live in your private access notes, not in this repo.

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
```

**Heartbeat sanity** (what the dashboard's status spine reads):

```bash
cat /opt/dhan-trading/run/trader_heartbeat.json | python3 -m json.tool
```

> Boot takes ~45–60 s (screener query). Right after a restart the file still belongs to the *old* process — don't trust a verification read until `uptime_seconds` confirms the new one.

A healthy market-hours heartbeat shows: `feed.connected: true`, `bars.bars_written` climbing, every strategy with `or_locked: true` (or an explicit no-range state), and `risk.halted: false`.

## Deploying changes

```bash
cd /opt/dhan-trading && sudo git pull --ff-only
# engine code changed?
sudo systemctl restart dhan-trader
# frontend changed?
cd dashboard && npm run build && sudo systemctl restart dhan-api
```

Restarting the trader mid-session is safe by design: positions reconcile from the DB, opening ranges reseed from REST intraday bars, and the EOD square-off is unconditional. Still, prefer deploying outside 09:15–15:30 IST when possible.

## Kill switch

```bash
curl -X POST http://localhost:8765/api/killswitch    # via tunnel, or:
touch /opt/dhan-trading/run/killswitch               # by hand on the agent
```

The trader's risk loop detects the file within ~10 s, halts, flattens all positions, and sends a Telegram alert. Remove the file and restart `dhan-trader` to resume.

## Mode changes (paper ↔ live)

Edit `/opt/dhan-trading/.env` (`PAPER_TRADING`, and for live also `ALLOW_LIVE_TOGGLE=true`), then `sudo systemctl restart dhan-trader`. `POST /api/mode` is read-only by design until the auth layer (M6) exists.

## Backfill operations

```bash
screen -r backfill                          # attach (Ctrl-A D to detach)
tail -f /tmp/backfill.log                   # progress
cat /opt/dhan-trading/backfill_ckpt_NSE_EQ.json   # checkpoint {index, total}
```

- Checkpointed: kill + relaunch resumes where it left off.
- A cron watchdog (every 15 min, weekdays) restarts the screen if it died and pings Telegram.
- **DH-901 token errors flooding the log** → the in-memory token went stale on a multi-day run: restart the backfill process (it re-reads the token store).
- Skipped chunks from transient timeouts are recorded; run the gap-scan script after completion for a repair pass.

## Scheduled automation (agent crontab, weekdays)

| When (IST) | Job |
|---|---|
| every 15 min | Backfill watchdog (restart + alert if dead) |
| 16:45 | `python -m ml.calibration fill` + `report` → calibration.log |
| 17:00 | EOD summary → Telegram (trades, P&L, gate verdicts, backfill %) |

The retired LLM ops agent must stay retired — plain cron + Telegram does this job at zero cost. Likewise the old process-watchdog cron (which kill-9'd slow boots) must never be re-added; systemd owns restarts.

## Database operations

```bash
# size / row estimates — ALWAYS catalog queries on hypertables:
#   approximate_row_count('bars'), hypertable_size('bars'),
#   chunk ranges from timescaledb_information.chunks
# NEVER: SELECT COUNT(*) FROM bars;  or  ORDER BY time LIMIT 1
# (full scan / chunk decompression — minutes of hang under backfill load)

# backup (streams pg_dump to S3; bucket derived from the AWS account)
bash hermes_skills/dhan/db_backup/scripts/backup.sh nightly
```

The DB EC2 runs with a swapfile and bounded `maintenance_work_mem` — history says an unbounded maintenance task plus no swap OOM-kills Postgres during compression.

## Troubleshooting quick table

| Symptom | First check | Likely cause / fix |
|---|---|---|
| Dashboard offline banner | `systemctl is-active dhan-trader`; heartbeat age | Trader down or booting; check trader.log |
| Feed connected but `bars_written: 0` | Heartbeat `bars` section during market hours | Subscription problem — Dhan v2 requires **string** SecurityIds; ints subscribe silently and stream nothing |
| Every gate verdict `STALE` / `age=None` | Live bars landing? (`bars` for today) | BarBuilder not receiving ticks — same as above |
| Dashboard frozen but API answers | api.log; slow query in executor | A heavy DB query starved file serving — keep analytics cached + in the dedicated pool |
| `DH-904` rate-limit errors | Which endpoint? | `charts/intraday` tolerates ~1 req/s — space calls, retry with backoff |
| Trader restart leaves positions unmanaged | Heartbeat strategies: `or_locked` | Should self-heal via OR seeding; EOD square-off is the unconditional backstop |
| Postgres down / OOM | `docker ps` on DB box, dmesg | Restart container; check swap + maintenance_work_mem; never reintroduce uncompressed duplicate tables |

## Monitoring checklist (during market hours)

1. Status spine green: ENGINE uptime ticking, FEED connected, mode badge correct
2. Heartbeat `bars_written` increasing every flush
3. Gate verdicts arriving with small `data_age_min`
4. Equity curve drawing on the Portfolio tab
5. Telegram silent (alerts fire on halt/watchdog events — silence is good)
