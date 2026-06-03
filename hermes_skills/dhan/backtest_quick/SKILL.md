---
name: backtest_quick
description: Run a 30-day ORB backtest on a specific symbol. Triggered by user: 'backtest RELIANCE 2885'.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Backtest,ORB,OnDemand,Quick]
    category: dhan-trading
---

# Backtest Quick Skill

Quick 30-day ORB backtest on a specific symbol, triggered on demand.

## How to Use
Tell Hermes: 'backtest RELIANCE' or 'backtest 2885'
Hermes runs: `python backfill.py --ids 2885 --all && python -m core.backtest ...`

## Output
Trades, win rate, Sharpe, max drawdown, equity curve summary for last 30 days.
