---
name: vix_monitor
description: Monitor India VIX level. Alerts when VIX crosses 20 (elevated) or 25 (high). Adjusts position sizing recommendation.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [VIX,Volatility,Risk,Alert]
    category: dhan-trading
---

# Vix Monitor Skill

Fetches India VIX from NSE and alerts when crossing key volatility thresholds.

## When to Use
- Cron: every 30 min during market hours (`*/30 3-10 * * 1-5`)
- User asks 'what is VIX today?'

## How Hermes handles it
Ask Hermes to web-search 'India VIX NSE today' and compare to thresholds:
- VIX <15: Low volatility, normal sizing
- VIX 15-20: Moderate, tighten stops
- VIX >20: Elevated, reduce size 30%
- VIX >25: High, reduce size 50%, raise Kronos threshold to 0.7
