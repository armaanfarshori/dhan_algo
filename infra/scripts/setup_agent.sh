#!/bin/bash
# Agent server bootstrap — minimal user_data.
# Only substitutes the 5 Terraform vars below; everything else uses
# plain shell variables so no $$ escaping is needed anywhere.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

# ── Terraform-substituted values (ONLY these are single-brace substitutions) ──
PROJECT="${project}"
AWS_REGION="${aws_region}"
S3_BUCKET="${s3_bucket}"
SSM_PREFIX="${ssm_prefix}"
DB_HOST="${db_host}"

# ── Packages ──────────────────────────────────────────────────────────────────
apt-get update -y
apt-get install -y python3.12 python3.12-venv python3.12-dev \
  git awscli jq curl build-essential libpq-dev postgresql-client

# uv — fast Python package manager
curl -LsSf https://astral.sh/uv/install.sh | sh
UV=/root/.local/bin/uv

# ── SSM helper (no function, just inline calls to avoid $$ issues) ────────────
get_ssm() {
  aws ssm get-parameter \
    --region "$AWS_REGION" \
    --name "$SSM_PREFIX/$1" \
    --with-decryption \
    --query Parameter.Value \
    --output text
}

DHAN_CLIENT_ID=$(get_ssm dhan_client_id)
DHAN_ACCESS_TOKEN=$(get_ssm dhan_access_token)
DHAN_TOTP_SECRET=$(get_ssm dhan_totp_secret)
DHAN_PIN=$(get_ssm dhan_pin)
DB_PASSWORD=$(get_ssm db_password)

# ── Clone repo ────────────────────────────────────────────────────────────────
APP_DIR=/opt/dhan-trading
git clone https://github.com/armaanfarshori/dhan_algo.git "$APP_DIR"

# ── Write .env (printf avoids heredoc $$ issues) ──────────────────────────────
printf '%s\n' \
  "DHAN_CLIENT_ID=$DHAN_CLIENT_ID" \
  "DHAN_ACCESS_TOKEN=$DHAN_ACCESS_TOKEN" \
  "DHAN_TOTP_SECRET=$DHAN_TOTP_SECRET" \
  "DHAN_PIN=$DHAN_PIN" \
  "PAPER_TRADING=true" \
  "STRATEGY=orb" \
  "MAX_DAILY_LOSS=5000" \
  "CAPITAL=100000" \
  "RISK_PER_TRADE=0.01" \
  "ORB_RANGE_MINUTES=15" \
  "WATCHLIST_EXCHANGE_SEGMENT=NSE_EQ" \
  "DB_HOST=$DB_HOST" \
  "DB_PORT=5432" \
  "DB_NAME=dhan_trading" \
  "DB_USER=trader" \
  "DB_PASSWORD=$DB_PASSWORD" \
  "KRONOS_TOKENIZER=NeoQuasar/Kronos-Tokenizer-base" \
  "KRONOS_MODEL=NeoQuasar/Kronos-small" \
  "KRONOS_LOOKBACK=400" \
  "KRONOS_PRED_LEN=30" \
  "KRONOS_SAMPLES=5" \
  "KRONOS_THRESH=0.001" \
  > "$APP_DIR/.env"

chmod 600 "$APP_DIR/.env"

# ── Python venv + deps ────────────────────────────────────────────────────────
cd "$APP_DIR"
"$UV" venv .venv --python 3.12
"$UV" pip install -r requirements.txt

# ── Wait for DB + run Alembic migrations ─────────────────────────────────────
for i in $(seq 1 24); do
  PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -U trader -d dhan_trading \
    -c "SELECT 1" &>/dev/null && break
  echo "Waiting for DB ($i/24)..."
  sleep 10
done
.venv/bin/alembic upgrade head

# ── Log directory (units write to /var/log/dhan/) ─────────────────────────────
mkdir -p /var/log/dhan
chown ubuntu:ubuntu /var/log/dhan

# ── Log rotation (daily, 14 days, compressed; copytruncate safe for open FDs) ──
cat > /etc/logrotate.d/dhan-trading << 'LOGROTATE_EOF'
/var/log/dhan/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
LOGROTATE_EOF

# ── systemd services (copy canonical units from repo) ─────────────────────────
cp "$APP_DIR/infra/systemd/dhan-trader.service"  /etc/systemd/system/dhan-trader.service
cp "$APP_DIR/infra/systemd/dhan-api.service"     /etc/systemd/system/dhan-api.service
cp "$APP_DIR/infra/systemd/dhan-alert@.service"  /etc/systemd/system/dhan-alert@.service

systemctl daemon-reload
systemctl enable --now dhan-trader dhan-api

# ── Agent crontab (weekday automation + health monitor) ───────────────────────
# All times are UTC; IST = UTC+5:30
# health_alert.py: every 5 min (all hours — disk + log errors are 24/7;
#   market-hours gate is inside the script itself)
# Backfill watchdog: every 15 min weekdays (existing)
# Calibration fill+report: 11:15 UTC = 16:45 IST weekdays (existing)
# EOD summary: 11:30 UTC = 17:00 IST weekdays (existing)
(crontab -l -u ubuntu 2>/dev/null || true; cat <<'CRON_EOF'
*/5 * * * * cd /opt/dhan-trading && .venv/bin/python scripts/health_alert.py >> /var/log/dhan/health_alert.log 2>&1
15 11 * * 1-5 cd /opt/dhan-trading && .venv/bin/python -m ml.calibration fill >> /var/log/dhan/calibration.log 2>&1 && .venv/bin/python -m ml.calibration report >> /var/log/dhan/calibration.log 2>&1
30 11 * * 1-5 cd /opt/dhan-trading && .venv/bin/python scripts/eod_summary.py >> /var/log/dhan/eod_summary.log 2>&1
CRON_EOF
) | crontab -u ubuntu -

chown -R ubuntu:ubuntu "$APP_DIR"
echo "Agent setup complete" > /var/log/setup_agent_done.txt
