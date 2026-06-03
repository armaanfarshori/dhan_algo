---
name: signal_calibration
description: Weekly Kronos accuracy check — compares signal direction vs actual next-session price move. Informs fine-tuning decision.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Kronos,Calibration,Accuracy,Weekly]
    category: dhan-trading
---

# Signal Calibration Skill

Weekly Kronos direction accuracy check.

## Cron Schedule
09:30 IST Sunday: `0 4 * * 0`
