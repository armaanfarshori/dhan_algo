---
name: strategy_performance
description: Weekly performance report: Sharpe ratio, win rate, avg PnL per trade from trades table. Fires Sunday 09:00 IST.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Performance,Weekly,Sharpe,Trades]
    category: dhan-trading
---

# Strategy Performance Skill

Weekly automated report delivered every Sunday.

## Cron Schedule
09:00 IST Sunday: `30 3 * * 0`
