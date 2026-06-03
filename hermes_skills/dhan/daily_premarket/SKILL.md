---
name: daily_premarket
description: Pre-market analysis — backfill check, Kronos signals, ORB range prep, risk limits check. Sends Telegram summary.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Pre-market, NSE, ORB, Risk, Daily]
    category: dhan-trading
---

# Daily Pre-market Skill

Runs every weekday at 08:45 IST. Orchestrates the morning startup sequence:
1. Verify data freshness (backfill gaps if any)
2. Run Kronos forecast → write signals
3. Log current risk state (daily loss limit, capital deployed)
4. Send morning briefing to Telegram

## When to Use

- Cron: every weekday at 08:45 IST (03:15 UTC)
- User asks "what's the morning setup?" or "ready for market open?"

## How to Run

```bash
cd /opt/dhan-trading
set -a && source .env && set +a
python3 ~/.hermes/skills/dhan/daily_premarket/scripts/premarket.py
```

## Telegram Output Example

```
🌅 Pre-market | 03 Jun 2026 | 08:45 IST

📊 Data: All 4 securities fresh (last bar: yesterday 15:29)

🤖 Kronos signals:
  • RELIANCE: BUY  (conf=0.71)
  • HDFCBANK: HOLD (conf=0.45)
  • INFY:     SELL (conf=0.63)
  • TCS:      BUY  (conf=0.68)

⚖️ Risk: ₹0 deployed | Daily limit ₹5,000 | Kill-switch: OFF
📋 Mode: PAPER | Strategy: ORB (15-min window)

Market opens in 30 min. ORB window: 09:15–09:30 IST.
```
