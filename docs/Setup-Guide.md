# Setup Guide

Two paths: **local development** (Mac/Linux, docker-compose, paper only) and
the **production AWS deploy**. All live execution belongs on AWS — Dhan locks
order placement to one whitelisted IP, and there is no sandbox.

---

## Local development

**Prerequisites:** Python 3.11+, Docker, Node 18+ (for the dashboard).

```bash
git clone https://github.com/armaanfarshori/dhan_algo && cd dhan_algo
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Minimum: DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN (from web.dhan.co → DhanHQ APIs)
# For token auto-refresh: DHAN_PIN + DHAN_TOTP_SECRET

docker compose up -d          # throwaway local TimescaleDB on :5432
```

Run Alembic migrations (schema head is **007**). `alembic/env.py` reads `DB_*`
from environment variables — source `.env` before running:

```bash
set -a && source .env && set +a
alembic upgrade head
```

```bash
python backfill.py --instruments     # scrip master, ~224K instruments

# Backfill runtime: the full NSE_EQ history (--nse-eq --all) takes DAYS and
# is rate-limited to Dhan's 100K API calls/day quota. For a local smoke test,
# fetch a single security — it completes in seconds:
python backfill.py --ids 2885        # quick single-security smoke test
# Run python backfill.py --help to see all flags (--nse-eq, --from, --all, etc.)
```

**Kronos model cache.** `KRONOS_OFFLINE=true` is the default — the model is
loaded from the local HuggingFace cache only. On a fresh machine with no cache,
the first start will log a model-load failure. Prime the cache once:

```bash
KRONOS_OFFLINE=false python -m apps.trader
# Downloads NeoQuasar/Kronos-small + NeoQuasar/Kronos-Tokenizer-base (~100 MB).
# Subsequent starts use the cache and never call the network.
# Alternatively, copy ~/.cache/huggingface/ from another machine.
```

The gate is fail-open: a missing model never blocks trades, but Kronos scoring
will not function until the cache is primed.

```bash
# Run (two terminals)
python -m apps.trader                # paper mode by default
python -m apps.api                   # http://localhost:8765

# Dashboard dev with hot reload (optional)
cd dashboard && npm install && npm run dev

# Tests
pytest -q                            # 71 passing
```

The local DB is for development only — the production database is the permanent
landing zone for market data; don't try to mirror it onto a laptop. For
research, pull curated Parquet extracts from S3 and query them with DuckDB.

---

## Production deploy (AWS)

### 1. Provision infrastructure

Terraform state lives in S3 + DynamoDB — see the *Remote state* note below.
The working Terraform clone is `~/Documents/codecode/Tessera/infra` (holds
`terraform.tfvars` + the gitignored `backend.hcl`). Do **not** run Terraform
from `~/Desktop/dhan_algo/infra` on the Mac — it lacks those private files.

```bash
cd ~/Documents/codecode/Tessera/infra

# First time (or migrating local → S3 state):
terraform init -migrate-state -backend-config=backend.hcl
# Subsequent inits:
terraform init -backend-config=backend.hcl

# Review before applying — watch for any aws_instance or aws_eip replace/destroy:
terraform plan
terraform apply
# Outputs: agent Elastic IP, DB private IP, S3 bucket name
```

**Remote state** is in
`s3://dhan-trading-tfstate-<ACCOUNT_ID>/dhan-trading/terraform.tfstate`
(versioned + encrypted, private), locked via DynamoDB table
`dhan-trading-tflock`. The bucket name embeds the AWS account ID and must not
be committed. `infra/backend.hcl.example` shows the required key; copy to
`backend.hcl` and fill in the real bucket name.

**`terraform apply` safety rule:** always review for `aws_instance` or
`aws_eip` `replace`/`destroy` operations before confirming. A drift on
`associate_public_ip_address` once forced a destructive agent-instance
replacement — that attribute is now in `lifecycle.ignore_changes`.

Infrastructure created: VPC with public (agent) + private (DB) subnets, two
ARM Graviton t4g EC2 instances, Elastic IP, gp3 EBS data volume for
TimescaleDB (with DLM snapshot policy), S3 bucket (versioned + encrypted +
lifecycle rules), SSM parameter store, IAM instance profiles. Estimated
cost ≈ $56/month.

### 2. Whitelist the Elastic IP at Dhan

DevPortal → API settings → add the agent's Elastic IP. **This applies to
order-placement APIs only** — data, historical, and WebSocket APIs work from
any IP. Note: once set, Dhan locks IP changes for **7 days**.

### 3. Configure the agent

The bootstrap script (`infra/scripts/setup_agent.sh`) runs automatically via
EC2 user-data: it clones the repo, writes `.env` from SSM, creates the venv,
runs Alembic, installs systemd units, sets log rotation, and wires up the
crontab. On a fresh instance nothing else is needed.

To verify or re-configure after the fact:

```bash
ssh -i <your-key> ubuntu@<agent-elastic-ip>
cat /opt/dhan-trading/.env           # confirm values
# Edit if needed — fields to verify:
#   DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN / DHAN_PIN / DHAN_TOTP_SECRET
#   DB_HOST=<db-private-ip>  DB_PASSWORD=<from SSM>
#   TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
#   PAPER_TRADING=true  (leave it)
```

Run migrations if this is a re-deploy and the schema changed:

```bash
cd /opt/dhan-trading
set -a && source .env && set +a
.venv/bin/alembic upgrade head       # expects head: 007
```

### 4. Install / reinstall systemd services

The bootstrap installs all three units automatically. To reinstall manually
(e.g. after editing `infra/systemd/*.service`):

```bash
sudo cp infra/systemd/dhan-trader.service   /etc/systemd/system/
sudo cp infra/systemd/dhan-api.service      /etc/systemd/system/
sudo cp infra/systemd/dhan-alert@.service   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dhan-trader dhan-api
```

Key unit details:
- Both `dhan-trader` and `dhan-api` have `StartLimitIntervalSec=300` /
  `StartLimitBurst=5` and `OnFailure=dhan-alert@%n.service`.
- `dhan-alert@.service` is a oneshot template that fires `python -m core.notify`
  with the failed unit name whenever either service enters `failed` state.
- Logs go to `/var/log/dhan/{trader,api}.log` (daily rotation, 14-day
  retention via `/etc/logrotate.d/dhan-trading`).

### 5. Prime the Kronos model cache

A fresh agent box has no HuggingFace cache. Prime it once before the service
starts (or the service will log a model-load warning and run gate-less):

```bash
cd /opt/dhan-trading
source .env   # sets KRONOS_MODEL etc.
KRONOS_OFFLINE=false .venv/bin/python -m apps.trader   # Ctrl-C after model downloads
```

This downloads `NeoQuasar/Kronos-small` and `NeoQuasar/Kronos-Tokenizer-base`
(~100 MB total) to `~/.cache/huggingface/`. Subsequent starts use the local
cache only (`KRONOS_OFFLINE=true` default).

### 6. Backfill historical data

```bash
cd /opt/dhan-trading
.venv/bin/python backfill.py --instruments        # scrip master (once)
screen -S backfill
.venv/bin/python backfill.py --nse-eq --all --from 2021-06-01
# Ctrl-A D to detach; checkpointed, safe to kill + resume
```

The backfill respects Dhan's 100K calls/day quota and writes a checkpoint
(`backfill_ckpt_NSE_EQ.json`). The cron watchdog (every 15 min, weekdays)
restarts the screen if it dies.

> **Runtime warning:** `--nse-eq --all` covers ~22K securities across multiple
> 90-day intraday chunks each. Expect **several days** at the API rate limit.
> For a quick connectivity check: `python backfill.py --ids 2885`.

### 7. Dashboard access

The dashboard binds to localhost on the agent. Reach it through an SSH tunnel:

```bash
~/Desktop/dhan_aws_access/connect.sh dashboard
# open http://localhost:8765
# or manually:
ssh -i <your-key> -N -L 8765:localhost:8765 ubuntu@<agent-elastic-ip>
```

### 8. Deploying updates

**Normal flow (Mac → agent):**

```bash
# Mac
git push

# Agent
cd /opt/dhan-trading && sudo git pull --ff-only
sudo systemctl restart dhan-trader           # if engine code changed
# if frontend changed:
cd dashboard && PATH="$HOME/.local/bin:$HOME/.hermes/node/bin:$PATH" npm run build
sudo systemctl restart dhan-api
```

> The `PATH` extension is needed on the agent because Node is installed under
> `~/.local/bin` / `~/.hermes/node/bin` and is not in the default root path.

**After a history rewrite / force-push to main** (happened once):

```bash
cd /opt/dhan-trading
sudo git fetch origin
sudo git reset --hard origin/main
```

See the [Operations-Runbook](Operations-Runbook.md) for monitoring, recovery,
alerting, backups, and the kill switch.

---

## Going live (eventually)

Do **not** simply flip the flag. The intended sequence: 2-year backtest passes
with realistic costs (M3) → shadow Kronos validation period → `PAPER_TRADING=false`
+ `ALLOW_LIVE_TOGGLE=true` in `.env` → restart `dhan-trader` → tiny capital.
The live executor confirms every fill against the broker and reconciles positions
on boot, but the discipline is procedural, not technical: live is a decision,
never a default.
