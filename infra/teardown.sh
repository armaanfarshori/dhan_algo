#!/bin/bash
# Emergency deprovision — destroys ALL AWS resources in this Terraform workspace.
# Run this if you need to kill everything: costs stop, data is gone.
#
# Usage:
#   ./teardown.sh          # shows plan, asks for confirmation
#   ./teardown.sh --force  # skips confirmation (use with extreme caution)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo -e "${RED}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${RED}║          EMERGENCY TEARDOWN — DHAN TRADING INFRA    ║${NC}"
echo -e "${RED}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}This will permanently destroy:${NC}"
echo "  • EC2 DB server (t4g.medium) — ALL DATA ON ROOT EBS LOST"
echo "  • EC2 Agent server (t4g.micro)"
echo "  • EBS data volume (200 GB TimescaleDB data) — PERMANENTLY DELETED"
echo "  • Elastic IP — will be de-listed from Dhan DevPortal"
echo "  • VPC, subnets, security groups, IAM roles"
echo "  • S3 bucket contents (backups, archives)"
echo "  • SSM parameters (Dhan credentials)"
echo ""
echo -e "${YELLOW}What survives:${NC}"
echo "  • Local .cache/scrip_master.csv"
echo "  • Any local PostgreSQL/TimescaleDB you installed for dev"
echo "  • GitHub repo code"
echo "  • SSH key at ~/.ssh/dhan_trading_key"
echo ""

if [[ "${1:-}" != "--force" ]]; then
    read -r -p "Type DESTROY to confirm: " confirm
    if [[ "$confirm" != "DESTROY" ]]; then
        echo "Aborted."
        exit 0
    fi
fi

echo ""
echo "Running terraform destroy..."
terraform destroy -auto-approve

echo ""
echo "All resources destroyed. Monthly charges will stop within the hour."
echo "To rebuild: cd infra && terraform apply"
