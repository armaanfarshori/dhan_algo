---
name: health_report
description: Weekly system health report — DB size, backfill coverage, API quota usage, AWS costs, agent uptime. Sent to Telegram every Sunday.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Health, Weekly, Monitoring, AWS]
    category: dhan-trading
---

# Health Report Skill

Runs every Sunday at 09:00 IST. Checks DB health, data coverage, and system costs.

## When to Use

- Cron: every Sunday at 09:00 IST (03:30 UTC)
- User asks "system health?" or "how's the DB?"

## How to Run

```bash
cd /opt/dhan-trading
set -a && source .env && set +a
python3 ~/.hermes/skills/dhan/health_report/scripts/report.py
```

## Telegram Output Example

```
🏥 Weekly Health Report | 01 Jun 2026

📦 Database
  Total bars: 741,696 (1m) + 4,949 (1d)
  Securities: 4 loaded | 22,646 instruments in master
  Oldest 1m bar: 2024-06-03 | Latest: 2026-06-01
  DB size: 2.1 GB | Compressed: 210 MB

🔑 Dhan API
  Token valid until: 2026-06-04 23:59
  Backfill calls today: 1,240 / 100,000

☁️ AWS (est.)
  EC2 (agent + DB): ~$31
  EBS (230GB): ~$21
  S3: $0.04
  Total: ~$52

⚙️ Agent
  Uptime: 4d 6h | Backfill: RUNNING (screen session)
  Last trade: 2026-06-03 | Total trades: 12

All systems nominal. ✅
```
