#!/bin/bash
# DB server bootstrap — injected as Terraform user_data.
#
# BARE-METAL PostgreSQL 16 + TimescaleDB (NO Docker). The platform migrated off a
# Docker TimescaleDB container to a native systemd Postgres in 2026-06; this script
# is the from-scratch provisioner for that bare-metal setup. It ADOPTS an existing
# cluster on the EBS data volume if one is present (the migration path), and only
# runs initdb on a truly empty volume.
#
# ⚠️ This provisioner is NOT exercised on every deploy (the live DB box is long-lived
# and snapshot-protected). Validate it on a throwaway instance before relying on it
# for disaster recovery — package/repo names and TimescaleDB tuning may drift.
#
# Terraform templatefile substitutes ${...}; literal bash ${...} is escaped as $${...};
# a plain $VAR is left untouched.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

# ── Terraform-substituted values (the only single-brace expansions below) ────
PROJECT="${project}"
AWS_REGION="${aws_region}"
S3_BUCKET="${s3_bucket}"
SSM_PREFIX="${ssm_prefix}"

# ── Mount EBS data volume FIRST (PGDATA lives on it) ──────────────────────────
# Attachment takes a few seconds.
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
grep -q timescaledb-data /etc/fstab || \
  echo "LABEL=timescaledb-data /data/timescaledb ext4 defaults,nofail 0 2" >> /etc/fstab
mount -a

# ── Packages: PostgreSQL 16 + TimescaleDB from their official apt repos ───────
apt-get update -y
apt-get install -y awscli jq curl gnupg ca-certificates lsb-release postgresql-common

# PGDG (PostgreSQL 16)
install -d /usr/share/postgresql-common/pgdg
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list

# TimescaleDB
curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey \
  | gpg --dearmor -o /usr/share/keyrings/timescaledb.gpg
echo "deb [signed-by=/usr/share/keyrings/timescaledb.gpg] https://packagecloud.io/timescale/timescaledb/ubuntu/ $(lsb_release -cs) main" \
  > /etc/apt/sources.list.d/timescaledb.list

apt-get update -y
apt-get install -y postgresql-16 postgresql-client-16 timescaledb-2-postgresql-16

# ── Point PG16 at the EBS data dir; adopt an existing cluster or initdb ───────
systemctl stop postgresql || true
PG_CONF=/etc/postgresql/16/main/postgresql.conf
PG_HBA=/etc/postgresql/16/main/pg_hba.conf

chown -R postgres:postgres /data/timescaledb/pgdata
chmod 700 /data/timescaledb/pgdata
if [ ! -s /data/timescaledb/pgdata/PG_VERSION ]; then
  # Truly empty volume — fresh cluster. (Migrated volumes already have PG_VERSION
  # and are adopted as-is, never re-initialised.)
  sudo -u postgres /usr/lib/postgresql/16/bin/initdb -D /data/timescaledb/pgdata
fi

sed -i "s|^#\?data_directory.*|data_directory = '/data/timescaledb/pgdata'|" "$PG_CONF"
# TimescaleDB must be preloaded; tune for the ~800M-row bars hypertable.
timescaledb-tune --quiet --yes --conf-path="$PG_CONF" \
  || echo "shared_preload_libraries = 'timescaledb'" >> "$PG_CONF"
echo "listen_addresses = '*'" >> "$PG_CONF"
grep -q "10.0.0.0/16" "$PG_HBA" \
  || echo "host all all 10.0.0.0/16 scram-sha-256" >> "$PG_HBA"

systemctl enable postgresql
systemctl start postgresql

# ── Role + DB + extension from SSM password (idempotent; no DO/$$ blocks) ─────
DB_PASSWORD=$(aws ssm get-parameter \
  --region "$AWS_REGION" \
  --name "$SSM_PREFIX/db_password" \
  --with-decryption \
  --query Parameter.Value \
  --output text)

for i in $(seq 1 30); do
  sudo -u postgres pg_isready && break || sleep 2
done

sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='trader'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE ROLE trader LOGIN PASSWORD '$DB_PASSWORD'"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='dhan_trading'" | grep -q 1 \
  || sudo -u postgres createdb -O trader dhan_trading
sudo -u postgres psql -d dhan_trading -c "CREATE EXTENSION IF NOT EXISTS timescaledb"

# ── Backup script (bare-metal pg_dump → S3; no Docker) ────────────────────────
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

if ! PGPASSWORD=$DB_PASS pg_dump -U trader -h localhost -d dhan_trading -Fc | \
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
