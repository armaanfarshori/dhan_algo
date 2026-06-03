---
name: watchlist_update
description: Replace static WATCHLIST_SECURITY_IDS with today's top 10 most volatile securities from the ATR screener.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Watchlist,Screener,Dynamic,Daily]
    category: dhan-trading
---

# Watchlist Update Skill

Replaces static watchlist with dynamic top-10 from ATR screener.

## Cron Schedule
08:30 IST weekdays: `0 3 * * 1-5`

## How it works
1. Runs `python backfill.py --nse-eq` screener via `get_top_volatile(n=10)`
2. Updates `WATCHLIST_SECURITY_IDS` in `/opt/dhan-trading/.env`
3. Sends Telegram: 'Watchlist updated: RELIANCE, HDFCBANK, ... (top 10 by ATR)'
