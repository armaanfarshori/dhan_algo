#!/bin/bash
# DB server bootstrap — runs once on first launch via user_data
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

# Variables injected by Terraform templatefile
PROJECT="${project}"
AWS_REGION="${aws_region}"
S3_BUCKET="${s3_bucket}"
SSM_PREFIX="${ssm_prefix}"

# ── Packages ──────────────────────────────────────────────────────────────────
apt-get update -y
apt-get install -y docker.io awscli jq postgresql-client

systemctl enable docker
systemctl start docker

# ── Mount the data EBS volume ─────────────────────────────────────────────────
# On Nitro instances /dev/sdf shows as /dev/nvme1n1
DATA_DEV=""
for candidate in /dev/nvme1n1 /dev/sdf /dev/xvdf; do
  if [ -b "$$candidate" ]; then
    DATA_DEV="$$candidate"
    break
  fi
done

if [ -z "$$DATA_DEV" ]; then
  echo "ERROR: data volume not found" && exit 1
fi

if ! blkid "$$DATA_DEV" &>/dev/null; then
  mkfs.ext4 -L timescaledb-data "$$DATA_DEV"
fi

mkdir -p /data/timescaledb/pgdata
echo "LABEL=timescaledb-data /data/timescaledb ext4 defaults,nofail 0 2" >> /etc/fstab
mount -a

# ── Pull DB password from SSM ─────────────────────────────────────────────────
DB_PASSWORD=$$(aws ssm get-parameter \
  --region "$$AWS_REGION" \
  --name "$$SSM_PREFIX/db_password" \
  --with-decryption \
  --query Parameter.Value \
  --output text)

# ── Start TimescaleDB ─────────────────────────────────────────────────────────
docker run -d \
  --name timescaledb \
  --restart always \
  -e POSTGRES_DB=dhan_trading \
  -e POSTGRES_USER=trader \
  -e POSTGRES_PASSWORD="$$DB_PASSWORD" \
  -v /data/timescaledb/pgdata:/var/lib/postgresql/data \
  -p 5432:5432 \
  timescale/timescaledb:latest-pg16

# Wait for DB ready
for i in $$(seq 1 30); do
  docker exec timescaledb pg_isready -U trader -d dhan_trading && break || sleep 5
done

# ── Nightly backup cron ───────────────────────────────────────────────────────
cat > /usr/local/bin/db_backup.sh << 'SCRIPT'
#!/bin/bash
set -e
DATE=$(date +%Y%m%d_%H%M%S)
DB_PASS=$(aws ssm get-parameter --region "$AWS_REGION" --name "$SSM_PREFIX/db_password" \
  --with-decryption --query Parameter.Value --output text)
PGPASSWORD=$DB_PASS docker exec timescaledb \
  pg_dump -U trader -d dhan_trading -Fc | \
  aws s3 cp - "s3://$S3_BUCKET/db-backups/dhan_trading_$DATE.dump"
echo "Backup done: $DATE"
SCRIPT

chmod +x /usr/local/bin/db_backup.sh

# Inject env vars into backup script
sed -i "s|\$AWS_REGION|$$AWS_REGION|g; s|\$SSM_PREFIX|$$SSM_PREFIX|g; s|\$S3_BUCKET|$$S3_BUCKET|g" \
  /usr/local/bin/db_backup.sh

echo "0 2 * * * root /usr/local/bin/db_backup.sh >> /var/log/db_backup.log 2>&1" \
  > /etc/cron.d/timescaledb-backup

echo "DB server setup complete" > /var/log/setup_db_done.txt
