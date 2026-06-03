---
name: order_audit
description: EOD audit of open/pending orders after market close. Alerts on orphaned orders that were never filled or cancelled.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Orders,Audit,EOD,Risk]
    category: dhan-trading
---

# Order Audit Skill

EOD check for unclosed orders after market hours.

## Cron Schedule
15:45 IST weekdays: `15 10 * * 1-5`
