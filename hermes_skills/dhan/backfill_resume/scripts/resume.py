#!/usr/bin/env python3
"""
Backfill watchdog — autonomous.
Cron: */15 * * * * (every 15 min, always — backfill runs 24/7 until done).
SILENT when running. Alerts + auto-restarts if screen session is dead.
"""
import subprocess, sys, os
from datetime import datetime

LOG_FILE = "/tmp/backfill.log"
SCREEN   = "backfill"

def python_process_alive():
    """Check if a backfill.py Python process is actually running (more reliable than screen)."""
    r = subprocess.run(["pgrep", "-f", "backfill.py"], capture_output=True, text=True)
    return r.returncode == 0

def last_log_age_minutes():
    try:
        mtime = os.path.getmtime(LOG_FILE)
        return (datetime.now().timestamp() - mtime) / 60
    except FileNotFoundError:
        return 9999

alive = python_process_alive()
age   = last_log_age_minutes()
# Only restart if BOTH the process is dead AND the log is very stale (>30 min)
# This prevents cascade restarts from log overwrites or slow sections
stale = age > 30

if alive:
    sys.exit(0)  # process running — always silent, even if log is stale

if not stale:
    sys.exit(0)  # process dead but log recent — might be normal restart, wait

# Both dead AND stale — likely genuine failure
print(f"⚠️ BACKFILL STOPPED | process dead, log stale {age:.0f} min | {datetime.now().strftime('%H:%M IST')}")
print("Restarting backfill queue...")

cmd = (
    "cd /opt/dhan-trading && "
    "sudo git pull -q && "   # always run with latest fixes before restart
    "set -a && source .env && set +a && "
    "echo \"Watchdog restart $(TZ=Asia/Kolkata date)\" >> /tmp/backfill.log && "
    ".venv/bin/python3 backfill.py --nse-eq --all --from 2021-06-01 --concurrency 4 "
    ">> /tmp/backfill.log 2>&1"
)
subprocess.Popen(["screen", "-dmS", SCREEN, "bash", "-c", cmd])
print(f"✅ Backfill restarted")
