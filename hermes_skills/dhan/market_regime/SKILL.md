---
name: market_regime
description: Classify today's market as TRENDING_UP, TRENDING_DOWN, HIGH_VOLATILITY, or SIDEWAYS using 20-day NIFTY bar history from TimescaleDB. Fires at market open to adjust Kronos confidence thresholds.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Market, Regime, Signal, Pre-market]
    category: dhan-trading
---

# Market Regime Skill

Runs at market open every weekday. Output is used by `daily_premarket` to adjust Kronos confidence thresholds and position sizing recommendations.

## Cron Schedule

09:15 IST weekdays: `45 3 * * 1-5`

## Regime → Trading adjustment

| Regime | ORB action | Kronos threshold |
|---|---|---|
| TRENDING_UP | Longs preferred | ≥0.35 |
| TRENDING_DOWN | Shorts preferred | ≥0.60 for longs |
| HIGH_VOLATILITY | Widen stops 1.5×, size 50% | ≥0.65 |
| SIDEWAYS | Skip ORB | ≥0.55 |
