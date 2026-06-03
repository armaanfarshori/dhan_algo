# Setup Guide

Two paths: local dev using docker-compose (Mac/Linux), and the production AWS deploy. All live execution happens on AWS — the Mac is an editor only.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| DhanHQ account | API access enabled at https://developer.dhan.co/ |
| Python 3.11 | ARM64 on EC2; any arch for local dev |
| Docker + Docker Compose | Local dev only |
| Terraform >= 1.5 | AWS deploy only |
| AWS CLI configured | Profile `dhan-terraform` used by infra |
| SSH key at `~/.ssh/dhan_trading_key` | EC2 access |

---

## Path A — Local Dev (docker-compose)

Use this to edit code and test backfill/API logic against a local TimescaleDB. Nothing is traded and no Dhan orders are placed (paper mode).

### 1. Clone and configure

```bash
git clone <repo-url> DhanAIBot
cd DhanAIBot

cp .env.example .env
# Edit .env — minimum required:
#   DHAN_CLIENT_ID=<from developer.dhan.co>
#   DHAN_ACCESS_TOKEN=<JWT from developer.dhan.co>
#   PAPER_TRADING=true        ← never change this locally
#   DB_HOST=localhost
#   DB_PORT=5432
#   DB_NAME=dhan_trading
#   DB_USER=trader
#   DB_PASSWORD=trader123
```

### 2. Start TimescaleDB

```bash
docker compose up -d
# Starts TimescaleDB on localhost:5432
# Credentials match the defaults in .env.example
```

Verify it is running:

```bash
docker compose ps
# Should show trading-db as "running"

docker exec -it trading-db psql -U trader -d dhan_trading -c "SELECT version();"
```

### 3. Create virtual environment and install dependencies

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Apply database schema

```bash
alembic upgrade head
alembic current   # should show: 003_auth_tables (head)
```

### 5. Load instrument master

Downloads the Dhan scrip master CSV (~224K instruments) and upserts into the `instruments` table. Run once, then only again when instrument data is stale.

```bash
python backfill.py --instruments
# Output: Done — 204000 instruments loaded across N segments
```

### 6. Backfill market data

Default: 90 days of 1-min bars for the securities in `WATCHLIST_SECURITY_IDS` (RELIANCE, HDFCBANK, INFY, TCS).

```bash
# Watchlist only, last 90 days (fastest, ~30 seconds)
python backfill.py

# Specific securities, 5 years, both 1-min and daily
python backfill.py --ids 2885,1333,1594,11536 --all --from 2021-06-01

# Dry run — fetch only, no DB writes
python backfill.py --all --from 2021-06-01 --dry-run
```

Verify data landed:

```bash
python -c "
from db import init_db, get_session
from config import get_config
from sqlalchemy import text
cfg = get_config()
init_db(cfg.db_url)
with get_session() as s:
    for row in s.execute(text('SELECT timeframe, COUNT(*), MIN(time)::date, MAX(time)::date FROM bars GROUP BY timeframe')):
        print(row)
"
```

### 7. Run the platform

```bash
source venv/bin/activate
python main.py
```

Expected startup output:

```
DhanHQ Algo Trading Platform  v1.0
Mode:     PAPER TRADING
Strategy: scalper
Client:   <your_client_id>
Dashboard: http://localhost:8765
F&O Scanner: NIFTY · BANKNIFTY · SENSEX · FINNIFTY · NIFTYNXT50 · MIDCPNIFTY
Equity Scanner: top 15 NSE movers · SMA crossover
```

Open `http://localhost:8765` in your browser.

### 8. Build the React dashboard (optional)

The platform serves a fallback HTML page from `static/index.html` by default. For the full React dashboard:

```bash
cd dashboard
npm install
npm run build
cd ..
python main.py   # now serves the compiled React build
```

For frontend dev with hot reload:

```bash
# Terminal 1
cd dashboard && npm run dev   # Vite dev server on :5173 (proxies /api/* to :8765)

# Terminal 2
python main.py                # API backend
```

### Stopping

`Ctrl+C` — the platform handles SIGINT and SIGTERM gracefully. All asyncio tasks are cancelled cleanly.

---

## Path B — AWS Deploy (production)

All steps except the `terraform apply` and initial git push run on the agent EC2 via SSH.

### 1. Deploy infrastructure (Mac, once)

```bash
cd infra
terraform init
terraform apply
# Review the plan, type "yes"
# Takes ~3 minutes
```

Save the outputs:

```
agent_elastic_ip = "13.206.66.237"   ← whitelist this in Dhan DevPortal
db_private_ip    = "10.0.1.155"
```

### 2. Whitelist agent IP in Dhan DevPortal

Go to https://developer.dhan.co/ → API settings → whitelist `13.206.66.237` (order APIs only). Data APIs need no whitelist. Once whitelisted, the IP cannot be changed for 7 days.

### 3. Verify DB server is running

```bash
# SSH to DB via agent jump
ssh -J ubuntu@13.206.66.237 -i ~/.ssh/dhan_trading_key ubuntu@10.0.1.155
sudo docker ps
# Should show trading-db container running
```

### 4. SSH to agent and verify schema

```bash
ssh -i ~/.ssh/dhan_trading_key ubuntu@13.206.66.237
cd ~/dhan_algo && source .venv/bin/activate
alembic current   # expect: 003_auth_tables (head)
```

The `setup_agent.sh` first-boot script has already cloned the repo, created the venv, pulled SSM secrets into `.env`, and applied migrations.

### 5. Pull latest code

After any Mac edits pushed to GitHub:

```bash
git pull origin main
```

### 6. Load instruments

```bash
python backfill.py --instruments
# ~204,000 NSE/BSE scrips loaded into instruments table
```

### 7. Backfill Nifty 50 — 5 years

```bash
# Nifty 50 — 1-min + daily, 5 years back (~3.5 min at 5 req/s)
python backfill.py --all --from 2021-06-01

# Expand to all NSE equities (runs for ~5 days — start in screen/tmux)
screen -S backfill
python backfill.py --nse-eq --all --from 2021-06-01
# Ctrl+A, D to detach; screen -r backfill to reattach
```

Backfill is resumable — re-running skips already-loaded date chunks. Safe to Ctrl+C and restart.

### 8. Run the platform

```bash
python main.py
```

Check backfill status from the dashboard or:

```bash
curl -s http://localhost:8765/api/backfill/status | python3 -m json.tool
```

### Dashboard access from Mac

```bash
ssh -L 8765:localhost:8765 ubuntu@13.206.66.237
# Then open http://localhost:8765 on Mac browser
```

---

## Hermes Setup

Hermes is already installed and running on the agent EC2 with the @farshoribot Telegram gateway. This section covers what was done and how to reconnect if the gateway drops.

### What is already configured

- Hermes installed at `~/.local/bin/hermes`
- Model: `meta-llama/llama-3.3-70b-instruct` via OpenRouter (key in SSM)
- Skills in `~/dhan_algo/hermes_skills/dhan/`: `backfill_check`, `daily_premarket`, `execution_loop`, `health_report`, `kill_switch`, `kronos_forecast`, `trade_reflection`
- Telegram bot: @farshoribot, connected to Hermes gateway

### Check gateway status

```bash
# From agent EC2
curl -s http://localhost:8765/api/hermes/status | python3 -m json.tool

# Or directly
export PATH=$HOME/.local/bin:$PATH
hermes gateway status
```

### Reconnect Telegram gateway (if dropped)

```bash
export PATH=$HOME/.local/bin:$PATH
hermes gateway setup   # follow the prompts to reconnect
```

### Run a Hermes skill manually

```bash
hermes run ~/dhan_algo/hermes_skills/dhan/health_report
hermes run ~/dhan_algo/hermes_skills/dhan/backfill_check
```

---

## Systemd Service (agent EC2)

The platform runs as a systemd service set up by `setup_agent.sh`. To manage it:

```bash
sudo systemctl status dhan-algo
sudo systemctl restart dhan-algo
sudo journalctl -u dhan-algo -f    # follow logs
```

---

## SSM Parameter Names

The following SSM SecureStrings are pulled by `setup_agent.sh` into `.env` on first boot:

| Parameter | Maps to |
|---|---|
| `/dhan/client_id` | `DHAN_CLIENT_ID` |
| `/dhan/access_token` | `DHAN_ACCESS_TOKEN` |
| `/dhan/pin` | `DHAN_PIN` |
| `/dhan/totp_secret` | `DHAN_TOTP_SECRET` |
| `/dhan/db_password` | `DB_PASSWORD` |

To update a secret:

```bash
aws ssm put-parameter \
  --name "/dhan/access_token" \
  --value "<new_token>" \
  --type SecureString \
  --overwrite \
  --region ap-south-1
```

Then on agent EC2:

```bash
# Re-pull SSM secrets into .env
source ~/dhan_algo/infra/scripts/setup_agent.sh --update-env
sudo systemctl restart dhan-algo
```

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `ModuleNotFoundError` | venv not active | `source venv/bin/activate` |
| `401 Unauthorized` from Dhan | Expired token | Regenerate at developer.dhan.co; update SSM + .env |
| `alembic current` shows nothing | DB not reachable | Check `DB_HOST` in `.env`; verify TimescaleDB container |
| Dashboard blank / 404 | React build not present | `npm run build` in `dashboard/` or use fallback `static/index.html` |
| `backfill.py --nse-eq` — no instruments | instruments table empty | Run `python backfill.py --instruments` first |
| Port 8765 already in use | Stale process | `fuser -k 8765/tcp` or `WEBHOOK_PORT=8766 python main.py` |
| Kronos model download hangs | First run, ~300MB | Wait; check HuggingFace connectivity from EC2 |
| Rate limit errors from Dhan | Too many requests | `core/client.py` enforces 5 req/s — check for parallel backfill processes |
| Hermes gateway offline | Process died | `hermes gateway start` on agent EC2 |
