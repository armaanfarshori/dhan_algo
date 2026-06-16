# Setup Guide

Two paths: **local development** (Mac/Linux, docker-compose, paper only) and the **production AWS deploy**. All live execution belongs on AWS — Dhan locks order placement to one whitelisted IP, and there is no sandbox.

---

## Local development

Prerequisites: Python 3.11+, Docker, Node 18+ (for the dashboard).

```bash
git clone https://github.com/armaanfarshori/dhan_algo && cd dhan_algo
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Minimum: DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN (from web.dhan.co → DhanHQ APIs)
# For token auto-refresh: DHAN_PIN + DHAN_TOTP_SECRET

docker compose up -d          # throwaway local TimescaleDB on :5432
alembic upgrade head          # expect head: 005

python backfill.py --instruments     # scrip master, ~224K instruments

# Backfill runtime: the full NSE_EQ history (--nse-eq --all) takes DAYS and
# is rate-limited to Dhan's 100K API calls/day quota. For a local smoke test,
# fetch a single security — it completes in seconds:
#   python backfill.py --ids 2885
# Run python backfill.py --help to see all flags (--nse-eq, --from, --all, etc.)
python backfill.py --ids 2885        # quick single-security smoke test

# Kronos model cache: KRONOS_OFFLINE=true by default — the model is loaded
# from the local HuggingFace cache only. On a fresh machine with no cache,
# the model will fail to load. Prime the cache with one download:
#
#   KRONOS_OFFLINE=false python -m apps.trader
#
# This downloads NeoQuasar/Kronos-small and NeoQuasar/Kronos-Tokenizer-base
# (~100 MB total) from HuggingFace. Subsequent starts use the cache and never
# call the network. Alternatively, pre-download on another machine and copy
# ~/.cache/huggingface/ across.
#
# The gate is fail-open: a missing model never blocks trades, but Kronos
# scoring won't work until the cache is primed.

# Run (two terminals)
python -m apps.trader                # paper mode by default
python -m apps.api                   # http://localhost:8765

# Dashboard dev with hot reload (optional)
cd dashboard && npm install && npm run dev

# Tests
pytest -q                            # 71 passing
```

The local DB is for development only — the production database is the permanent landing zone for market data; don't try to mirror it onto a laptop. For research, pull curated Parquet extracts from S3 and query them with DuckDB.

---

## Production deploy (AWS)

### 1. Provision

```bash
cd infra && terraform init && terraform apply
# Outputs: agent Elastic IP, DB private IP, S3 bucket name
```

This creates: a VPC with a public subnet (agent) and private subnet (DB), two ARM Graviton EC2 instances, an Elastic IP, an S3 bucket, SSM parameters for secrets, and IAM instance profiles. Estimated cost ≈ $56/month.

### 2. Whitelist the Elastic IP at Dhan

DevPortal → API settings → add the agent's Elastic IP. **This applies to order-placement APIs only** — data, historical, and WebSocket APIs work from any IP. Note: once set, Dhan locks IP changes for 7 days.

### 3. Configure the agent

```bash
ssh -i <your-key> ubuntu@<agent-elastic-ip>
cd /opt/dhan-trading                      # repo cloned by the bootstrap script
cp .env.example .env                      # fill in:
#   DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN / DHAN_PIN / DHAN_TOTP_SECRET
#   DB_HOST=<db-private-ip>  DB_PASSWORD=<from SSM>
#   TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   (alerts)
#   PAPER_TRADING=true                      (leave it)
alembic upgrade head
```

### 4. Install the services

```bash
sudo cp infra/systemd/dhan-trader.service /etc/systemd/system/
sudo cp infra/systemd/dhan-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dhan-trader dhan-api
```

Logs land in `/var/log/dhan/{trader,api}.log`.

### 5. Backfill historical data

```bash
python backfill.py --instruments                       # once
screen -S backfill
python backfill.py --nse-eq --all --from 2021-06-01    # ~days; checkpointed,
                                                       # safe to kill + resume
```

The backfill respects Dhan's 100K calls/day quota and writes a checkpoint file (`backfill_ckpt_NSE_EQ.json`); a cron watchdog can restart it if the screen dies.

> **Runtime warning:** `--nse-eq --all` covers ~22K securities across multiple 90-day intraday chunks each. Expect **several days** at the API rate limit — this is normal. The process is checkpointed and safe to kill and resume. For a quick connectivity check, fetch a single security first: `python backfill.py --ids 2885`. Run `python backfill.py --help` for all available flags.

> **Kronos model cache:** `KRONOS_OFFLINE=true` is the default. A fresh agent box has no HuggingFace cache, so `apps.trader` will log a model-load failure on first start. Prime the cache once before starting the service: `KRONOS_OFFLINE=false python -m apps.trader` (downloads `NeoQuasar/Kronos-small` + `NeoQuasar/Kronos-Tokenizer-base`, ~100 MB). The gate is fail-open — a missing model never blocks trades — but Kronos scoring won't function until the cache exists.

### 6. Dashboard access

The dashboard binds to localhost on the agent. Reach it through an SSH tunnel:

```bash
ssh -i <your-key> -N -L 8765:localhost:8765 ubuntu@<agent-elastic-ip>
# open http://localhost:8765
```

### 7. Deploying updates

```bash
# on the agent
cd /opt/dhan-trading && sudo git pull --ff-only
sudo systemctl restart dhan-trader            # if engine code changed
cd dashboard && npm run build                 # if frontend changed
sudo systemctl restart dhan-api
```

See the [Operations-Runbook](Operations-Runbook.md) for monitoring, recovery, and the kill switch.

---

## Going live (eventually)

Do **not** simply flip the flag. The intended sequence: 2-year backtest passes with realistic costs → shadow validation period → `PAPER_TRADING=false` + `ALLOW_LIVE_TOGGLE=true` in `.env` → restart `dhan-trader` → tiny capital. The live executor confirms every fill against the broker and reconciles positions on boot, but the discipline is procedural, not technical: live is a decision, never a default.
