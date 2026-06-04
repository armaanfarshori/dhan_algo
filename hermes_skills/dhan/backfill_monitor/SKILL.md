---
name: backfill_monitor
description: Monitor all backfill logs (NSE_EQ, NSE_CDS, BSE_EQ, MCX) for errors and send Telegram alerts. Also monitors platform log for system errors. Runs every 10 minutes autonomously.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Backfill, Monitoring, Errors, Alert, Autonomous]
    category: dhan-trading
---

# Backfill Monitor Skill

Scans all backfill log files and the platform log for errors. Silent when clean. Sends Telegram alert with error details when issues are found.

## Cron Schedule

Every 10 minutes, always active: `*/10 * * * *`

## How to Run

```bash
python3 ~/.hermes/skills/dhan/backfill_monitor/scripts/monitor.py
```
