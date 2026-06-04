#!/usr/bin/env python3
"""
Backfill + system error monitor — autonomous.
Cron: */10 * * * * (every 10 minutes, always active).
SILENT when clean. Alerts on errors in any log file.
"""
import os, sys, subprocess
from datetime import datetime, timedelta
from pathlib import Path

LOG_FILES = {
    "NSE_EQ backfill":    "/tmp/backfill.log",
    "NSE_CDS backfill":   "/tmp/backfill_nse_cds.log",
    "BSE_EQ backfill":    "/tmp/backfill_bse_eq.log",
    "MCX backfill":       "/tmp/backfill_mcx.log",
    "Backfill queue":     "/tmp/backfill_queue.log",
    "Platform":           "/tmp/platform.log",
}

# Only scan lines written in last 15 minutes
LOOKBACK_MINUTES = 15

def recent_errors(log_path: str) -> list[str]:
    """Return error lines from the last LOOKBACK_MINUTES in a log file."""
    p = Path(log_path)
    if not p.exists():
        return []
    cutoff = datetime.now() - timedelta(minutes=LOOKBACK_MINUTES)
    errors = []
    try:
        lines = p.read_text(errors="ignore").splitlines()[-500:]  # last 500 lines max
        for line in lines:
            if "ERROR" in line or "CRITICAL" in line or "Chunk failed" in line:
                # Try to parse timestamp from log line
                # Format: "13:05:51  ERROR  ..."
                try:
                    parts = line.split()
                    if parts and ":" in parts[0]:
                        t = datetime.strptime(parts[0], "%H:%M:%S").replace(
                            year=datetime.now().year,
                            month=datetime.now().month,
                            day=datetime.now().day,
                        )
                        if t >= cutoff:
                            errors.append(line.strip()[:120])
                except Exception:
                    errors.append(line.strip()[:120])  # include if can't parse time
    except Exception:
        pass
    return errors[:5]  # max 5 errors per log

all_errors = {}
for label, path in LOG_FILES.items():
    errs = recent_errors(path)
    if errs:
        all_errors[label] = errs

if all_errors:
    print(f"⚠️ BACKFILL/SYSTEM ERRORS | {datetime.now().strftime('%H:%M IST')}")
    for label, errs in all_errors.items():
        print(f"\n{label}:")
        for e in errs:
            print(f"  {e}")
    print(f"\n{sum(len(v) for v in all_errors.values())} error(s) across {len(all_errors)} log(s)")
# Silent if no errors
