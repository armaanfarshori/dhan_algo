#!/bin/bash
# DB backup — user-triggered or emergency.
# For routine nightly backup, see setup_db.sh (already cron'd at 2AM).
# This script is for on-demand backups before risky operations.
set -euo pipefail

DATE=$(date +%Y%m%d_%H%M%S)
LABEL="${1:-manual}"
S3_BUCKET=$(aws ssm get-parameter --region ap-south-1 \
  --name /dhan-trading/s3_bucket --with-decryption \
  --query Parameter.Value --output text 2>/dev/null || echo "dhan-trading-data-155304839154")
DB_PASS=$(aws ssm get-parameter --region ap-south-1 \
  --name /dhan-trading/db_password --with-decryption \
  --query Parameter.Value --output text)

DEST="s3://$S3_BUCKET/db-backups/dhan_trading_${LABEL}_${DATE}.dump"
echo "Starting backup → $DEST"

PGPASSWORD="$DB_PASS" pg_dump -h 10.0.1.155 -U trader -d dhan_trading -Fc \
  | aws s3 cp - "$DEST"

SIZE=$(aws s3 ls "$DEST" | awk '{print $3}')
echo "✅ Backup complete: $DEST ($SIZE bytes)"
