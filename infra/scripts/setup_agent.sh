#!/bin/bash
# Agent server bootstrap — runs once on first launch
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

# Variables injected by Terraform templatefile
PROJECT="${project}"
AWS_REGION="${aws_region}"
S3_BUCKET="${s3_bucket}"
SSM_PREFIX="${ssm_prefix}"
DB_HOST="${db_host}"

# ── Packages ──────────────────────────────────────────────────────────────────
apt-get update -y
apt-get install -y python3.11 python3.11-venv python3.11-dev \
  git awscli jq curl build-essential libpq-dev

curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="/root/.local/bin:$$PATH"

# ── Pull secrets from SSM ─────────────────────────────────────────────────────
get_param() {
  aws ssm get-parameter --region "$$AWS_REGION" --name "$$SSM_PREFIX/$$1" \
    --with-decryption --query Parameter.Value --output text
}

DHAN_CLIENT_ID=$$(get_param dhan_client_id)
DHAN_ACCESS_TOKEN=$$(get_param dhan_access_token)
DHAN_TOTP_SECRET=$$(get_param dhan_totp_secret)
DHAN_PIN=$$(get_param dhan_pin)
DB_PASSWORD=$$(get_param db_password)

# ── Clone repo ────────────────────────────────────────────────────────────────
APP_DIR=/opt/dhan-trading
git clone https://github.com/armaanfarshori/dhan_algo.git "$$APP_DIR"

# ── Write .env ────────────────────────────────────────────────────────────────
cat > "$$APP_DIR/.env" << ENV
DHAN_CLIENT_ID=$$DHAN_CLIENT_ID
DHAN_ACCESS_TOKEN=$$DHAN_ACCESS_TOKEN
DHAN_TOTP_SECRET=$$DHAN_TOTP_SECRET
DHAN_PIN=$$DHAN_PIN

PAPER_TRADING=true
STRATEGY=orb
MAX_DAILY_LOSS=5000
CAPITAL=100000
RISK_PER_TRADE=0.01
ORB_RANGE_MINUTES=15

WATCHLIST_SECURITY_IDS=2885,1333,1594,11536
WATCHLIST_EXCHANGE_SEGMENT=NSE_EQ

DB_HOST=$$DB_HOST
DB_PORT=5432
DB_NAME=dhan_trading
DB_USER=trader
DB_PASSWORD=$$DB_PASSWORD

KRONOS_TOKENIZER=NeoQuasar/Kronos-Tokenizer-base
KRONOS_MODEL=NeoQuasar/Kronos-small
KRONOS_LOOKBACK=400
KRONOS_PRED_LEN=30
KRONOS_SAMPLES=5
KRONOS_THRESH=0.001
ENV

chmod 600 "$$APP_DIR/.env"

# ── Python venv + deps ────────────────────────────────────────────────────────
cd "$$APP_DIR"
uv venv .venv --python 3.11
uv pip install -r requirements.txt

# ── Wait for DB and run migrations ────────────────────────────────────────────
for i in $$(seq 1 24); do
  PGPASSWORD="$$DB_PASSWORD" psql -h "$$DB_HOST" -U trader -d dhan_trading \
    -c "SELECT 1" &>/dev/null && break
  echo "Waiting for DB... ($$i/24)"
  sleep 10
done

.venv/bin/alembic upgrade head

# ── systemd service ───────────────────────────────────────────────────────────
cat > /etc/systemd/system/dhan-agent.service << SVC
[Unit]
Description=Dhan Algorithmic Trading Agent
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=ubuntu
WorkingDirectory=$$APP_DIR
EnvironmentFile=$$APP_DIR/.env
ExecStart=$$APP_DIR/.venv/bin/python3 main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload

# NSE trading hours (IST = UTC+5:30)
# Pre-open 09:00 IST = 03:30 UTC | Close+15min 15:45 IST = 10:15 UTC
cat > /etc/cron.d/dhan-agent << 'CRON'
30 3 * * 1-5 root systemctl start dhan-agent
15 10 * * 1-5 root systemctl stop dhan-agent
CRON

chown -R ubuntu:ubuntu "$$APP_DIR"
echo "Agent setup complete" > /var/log/setup_agent_done.txt
