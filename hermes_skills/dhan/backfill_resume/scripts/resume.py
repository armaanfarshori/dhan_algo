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

def screen_alive():
    r = subprocess.run(["screen", "-ls"], capture_output=True, text=True)
    return SCREEN in r.stdout

def last_log_age_minutes():
    try:
        mtime = os.path.getmtime(LOG_FILE)
        return (datetime.now().timestamp() - mtime) / 60
    except FileNotFoundError:
        return 9999

alive  = screen_alive()
age    = last_log_age_minutes()
stale  = age > 10  # no log update in 10 min = likely stuck

if alive and not stale:
    sys.exit(0)  # silent — all good

# Something is wrong — alert and attempt restart
reason = []
if not alive:
    reason.append("screen session died")
if stale:
    reason.append(f"log stale {age:.0f} min")

print(f"⚠️ BACKFILL INTERRUPTED | {', '.join(reason)}")
print("Restarting backfill...")

# Kill any zombie session first
subprocess.run(["screen", "-S", SCREEN, "-X", "quit"], capture_output=True)

# Restart
cmd = (
    "cd /opt/dhan-trading && "
    "set -a && source .env && set +a && "
    "echo \"Backfill restarted at $(date)\" > /tmp/backfill.log && "
    ".venv/bin/python3 backfill.py --nse-eq --all --from 2021-06-01 "
    ">> /tmp/backfill.log 2>&1 && "
    "echo \"Backfill finished at $(date)\" >> /tmp/backfill.log"
)
subprocess.Popen(["screen", "-dmS", SCREEN, "bash", "-c", cmd])

print(f"✅ Backfill restarted in screen '{SCREEN}'")
print("It will resume from where it left off (ON CONFLICT DO NOTHING).")
