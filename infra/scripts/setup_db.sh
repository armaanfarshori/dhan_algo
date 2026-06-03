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
  timescale/timescaledb:latest-pg16

for i in $(seq 1 30); do
  docker exec timescaledb pg_isready -U trader -d dhan_trading && break || sleep 5
done

# ── Write backup script (no heredoc — plain file write) ───────────────────────
cat > /usr/local/bin/db_backup.sh /dev/null << 'BACKUP_EOF'
#!/bin/bash
set -e
DATE=$(date +%Y%m%d_%H%M%S)
source /etc/dhan-env
DB_PASS=$(aws ssm get-parameter --region "$AWS_REGION" \
  --name "$SSM_PREFIX/db_password" --with-decryption \
  --query Parameter.Value --output text)
PGPASSWORD=$DB_PASS docker exec timescaledb \
  pg_dump -U trader -d dhan_trading -Fc | \
  aws s3 cp - "s3://$S3_BUCKET/db-backups/dhan_trading_$DATE.dump"
echo "Backup done: $DATE"
BACKUP_EOF

# Write env file for backup script
printf 'AWS_REGION=%s\nSSM_PREFIX=%s\nS3_BUCKET=%s\n' \
  "$AWS_REGION" "$SSM_PREFIX" "$S3_BUCKET" > /etc/dhan-env

chmod +x /usr/local/bin/db_backup.sh
echo "0 2 * * * root /usr/local/bin/db_backup.sh >> /var/log/db_backup.log 2>&1" \
  > /etc/cron.d/timescaledb-backup

echo "DB server setup complete" > /var/log/setup_db_done.txt
