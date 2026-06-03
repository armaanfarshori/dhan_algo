#!/bin/bash
# Safe rolling deploy — user-triggered only (never run from cron).
# Pulls latest from GitHub, installs new deps, restarts agent.
# Waits for health check before reporting success.
set -euo pipefail

APP_DIR="/opt/dhan-trading"
cd "$APP_DIR"

echo "=== Deploy started at $(date) ==="

echo "→ git pull"
git pull origin main

echo "→ uv pip install (new deps only)"
$HOME/.local/bin/uv pip install -r requirements.txt --quiet

echo "→ alembic upgrade head"
set -a && source .env && set +a
.venv/bin/alembic upgrade head

echo "→ restarting dhan-agent service"
sudo systemctl restart dhan-agent

# Wait for service to be active
for i in $(seq 1 12); do
  if systemctl is-active --quiet dhan-agent; then
    echo "✅ dhan-agent is running"
    systemctl status dhan-agent --no-pager | grep "Active:"
    exit 0
  fi
  sleep 5
done

echo "❌ dhan-agent failed to start after restart"
journalctl -u dhan-agent --no-pager -n 20
exit 1
