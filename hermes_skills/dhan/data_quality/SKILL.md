---
name: data_quality
description: Nightly scan of bars table for zero-volume candles, OHLC violations, >15% price spikes, and duplicate timestamps. Alerts only when anomalies found.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Data, Quality, Nightly, Audit]
    category: dhan-trading
---

# Data Quality Skill

Runs nightly at 02:00 IST. Silent on clean data. Alerts immediately if bad candles are found that would corrupt Kronos forecasts.

## Cron Schedule

02:00 IST nightly: `30 20 * * *`
