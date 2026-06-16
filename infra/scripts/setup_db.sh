#!/bin/bash
# DB server bootstrap — injected as user_data.
# Minimal Terraform template substitution; all bash vars use plain $ syntax
# because this script has NO heredocs and no complex expansions.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

# ── Terraform-substituted values (these are the ONLY ${} expansions) ─────────
PROJECT="${project}"
AWS_REGION="${aws_region}"
S3_BUCKET="${s3_bucket}"
SSM_PREFIX="${ssm_prefix}"

# ── Packages ──────────────────────────────────────────────────────────────────
apt-get update -y
apt-get install -y docker.io awscli jq postgresql-client
systemctl enable docker
systemctl start docker

# ── Mount EBS data volume ─────────────────────────────────────────────────────
# Wait for the volume to appear (attachment takes a few seconds)
sleep 10
DATA_DEV=""
for candidate in /dev/nvme1n1 /dev/xvdf /dev/sdf; do
  if [ -b "$candidate" ]; then
    DATA_DEV="$candidate"
    break
  fi
done
[ -z "$DATA_DEV" ] && { echo "ERROR: data volume not found"; exit 1; }

if ! blkid "$DATA_DEV" &>/dev/null; then
  mkfs.ext4 -L timescaledb-data "$DATA_DEV"
fi
mkdir -p /data/timescaledb/pgdata
echo "LABEL=timescaledb-data /data/timescaledb ext4 defaults,nofail 0 2" >> /etc/fstab
mount -a

# ── Pull DB password from SSM ─────────────────────────────────────────────────
DB_PASSWORD=$(aws ssm get-parameter \
  --region "$AWS_REGION" \
  --name "$SSM_PREFIX/db_password" \
  --with-decryption \
  --query Parameter.Value \
  --output text)

# ── Start TimescaleDB ─────────────────────────────────────────────────────────
docker run -d \
  --name timescaledb \
  --restart always \
  -e POSTGRES_DB=dhan_trading \
  -e POSTGRES_USER=trader \
  -e "POSTGRES_PASSWORD=$DB_PASSWORD" \
  -v /data/timescaledb/pgdata:/var/lib/postgresql/data \
  -p 5432:5432 \
  timescale/timescaledb:2.17.2-pg16

for i in $(seq 1 30); do
  docker exec timescaledb pg_isready -U trader -d dhan_trading && break || sleep 5
done

# ── Write backup script (no heredoc — plain file write) ───────────────────────
cat > /usr/local/bin/db_backup.sh << 'BACKUP_EOF'
#!/bin/bash
set -e
DATE=$(date +%Y%m%d_%H%M%S)
source /etc/dhan-env

DB_PASS=$(aws ssm get-parameter --region "$AWS_REGION" \
  --name "$SSM_PREFIX/db_password" --with-decryption \
  --query Parameter.Value --output text)

# OPS-09: wrap dump+upload in a failure check; send Telegram alert on non-zero exit.
# TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be present in /etc/dhan-env (or .env).
_backup_notify_fail() {
  local msg="[dhan-trading] BACKUP FAILED on $(hostname) at $DATE — check /var/log/db_backup.log"
  if [ -n "$${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "$${TELEGRAM_CHAT_ID:-}" ]; then
    curl -s -X POST "https://api.telegram.org/bot$${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d "chat_id=$${TELEGRAM_CHAT_ID}" \
      -d "text=$${msg}" > /dev/null || true
  else
    echo "WARNING: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — cannot send alert" >&2
  fi
}

if ! PGPASSWORD=$DB_PASS docker exec timescaledb \
    pg_dump -U trader -d dhan_trading -Fc | \
    aws s3 cp - "s3://$S3_BUCKET/db-backups/dhan_trading_$DATE.dump"; then
  echo "ERROR: backup failed at $DATE" >&2
  _backup_notify_fail
  exit 1
fi

echo "Backup done: $DATE"
BACKUP_EOF

# Write env file for backup script.
# OPS-09: pull Telegram creds from SSM so backup alerts work without hardcoding them.
TELEGRAM_BOT_TOKEN=$(aws ssm get-parameter --region "$AWS_REGION" \
  --name "$SSM_PREFIX/telegram_bot_token" --with-decryption \
  --query Parameter.Value --output text 2>/dev/null || echo "")
TELEGRAM_CHAT_ID=$(aws ssm get-parameter --region "$AWS_REGION" \
  --name "$SSM_PREFIX/telegram_chat_id" --with-decryption \
  --query Parameter.Value --output text 2>/dev/null || echo "")
printf 'AWS_REGION=%s\nSSM_PREFIX=%s\nS3_BUCKET=%s\nTELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' \
  "$AWS_REGION" "$SSM_PREFIX" "$S3_BUCKET" "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID" > /etc/dhan-env

chmod +x /usr/local/bin/db_backup.sh
echo "0 2 * * * root /usr/local/bin/db_backup.sh >> /var/log/db_backup.log 2>&1" \
  > /etc/cron.d/timescaledb-backup

echo "DB server setup complete" > /var/log/setup_db_done.txt
