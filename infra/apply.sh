#!/bin/bash
# Smart apply — provisions infra, tears down automatically on failure to avoid idle charges.
# Usage: ./apply.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}Running terraform apply...${NC}"

if terraform apply -auto-approve; then
    echo -e "${GREEN}Apply succeeded.${NC}"

    # ── Write outputs to local file (not committed to git) ────────────────────
    AGENT_IP=$(terraform output -raw agent_elastic_ip 2>/dev/null || echo "unknown")
    DB_IP=$(terraform output -raw db_private_ip 2>/dev/null || echo "unknown")
    S3=$(terraform output -raw s3_bucket 2>/dev/null || echo "unknown")
    AGENT_ID=$(terraform output -raw agent_instance_id 2>/dev/null || echo "unknown")
    DB_ID=$(terraform output -raw db_instance_id 2>/dev/null || echo "unknown")

    cat > aws_outputs.local << OUTPUT
# AWS infrastructure outputs — LOCAL ONLY, never committed to git
# Generated: $(date -u +%Y-%m-%d\ %H:%M:%S\ UTC)
# Region: ap-south-1

AGENT_ELASTIC_IP=$AGENT_IP
AGENT_INSTANCE_ID=$AGENT_ID
DB_INSTANCE_ID=$DB_ID
DB_PRIVATE_IP=$DB_IP
S3_BUCKET=$S3

SSH_AGENT=ssh -i ~/.ssh/dhan_trading_key ubuntu@$AGENT_IP
SSH_DB=ssh -J ubuntu@$AGENT_IP -i ~/.ssh/dhan_trading_key ubuntu@$DB_IP
SSH_TUNNEL_DASHBOARD=ssh -i ~/.ssh/dhan_trading_key -L 8765:localhost:8765 ubuntu@$AGENT_IP

# WHITELIST THIS IP IN DHAN DEVPORTAL (required for live orders):
DHAN_WHITELIST_IP=$AGENT_IP

# Emergency teardown: cd infra && ./teardown.sh
OUTPUT

    echo ""
    echo -e "${GREEN}Key outputs:${NC}"
    echo "  Agent Elastic IP : $AGENT_IP  ← whitelist in Dhan DevPortal"
    echo "  DB private IP    : $DB_IP"
    echo "  S3 bucket        : $S3"
    echo ""
    echo "  SSH agent : ssh -i ~/.ssh/dhan_trading_key ubuntu@$AGENT_IP"
    echo "  SSH DB    : ssh -J ubuntu@$AGENT_IP -i ~/.ssh/dhan_trading_key ubuntu@$DB_IP"
    echo ""
    echo "  Outputs saved to: infra/aws_outputs.local"

else
    echo ""
    echo -e "${RED}Apply FAILED. Running teardown to avoid idle charges...${NC}"
    terraform destroy -auto-approve || true
    echo -e "${RED}Teardown complete. Fix the error above and re-run ./apply.sh${NC}"
    exit 1
fi
