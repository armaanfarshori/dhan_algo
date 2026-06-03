---
name: market_summary
description: End-of-day summary at 15:30 IST: NIFTY/BankNIFTY close, top movers, today's trades and PnL.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [EOD,Summary,NIFTY,Daily]
    category: dhan-trading
---

# Market Summary Skill

End-of-day brief delivered to Telegram at 15:30 IST.

## Cron Schedule
15:30 IST weekdays: `0 10 * * 1-5`

## Output
NIFTY close and % change, BankNIFTY close, top 3 gainers/losers from watchlist,
today's trades from DB, today's realized PnL, next market open time.
