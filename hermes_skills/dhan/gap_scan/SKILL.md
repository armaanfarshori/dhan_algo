---
name: gap_scan
description: Nightly scan for securities that have historical bars but are missing yesterday's data. Outputs the exact backfill command to fix gaps.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Data, Gaps, Nightly, Backfill]
    category: dhan-trading
---

# Gap Scan Skill

Runs at 02:30 IST nightly. Identifies securities that had data loaded but missed yesterday's session. Generates the targeted backfill command.

## Cron Schedule

02:30 IST nightly: `0 21 * * *`
