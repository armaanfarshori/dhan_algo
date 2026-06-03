---
name: backfill_resume
description: Autonomous watchdog for the 5-day backfill job. Checks every 15 min if the screen session is alive and log is updating. Auto-restarts if dead — no human needed.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Data, Backfill, Watchdog, Autonomous]
    category: dhan-trading
---

# Backfill Resume Skill

Monitors the `backfill` screen session. If it dies (crash, EC2 reboot, OOM), auto-restarts from where it left off — all upserts are idempotent (`ON CONFLICT DO NOTHING`).

## Cron Schedule

Every 15 minutes, always active: `*/15 * * * *`

## When to Use

- Automatically (cron) — alerts + restarts if dead
- After manual EC2 reboot to confirm backfill resumed
- User asks "is the backfill still running?"

## How to Run

```bash
python3 ~/.hermes/skills/dhan/backfill_resume/scripts/resume.py
```
