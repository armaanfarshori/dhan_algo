---
name: trade_reflection
description: Post-market PnL review — summarize today's trades, write to journal table, compare vs backtest expectations, detect drift.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Post-market, PnL, Journal, Review]
    category: dhan-trading
---

# Trade Reflection Skill

Runs every weekday at 15:45 IST (after market close). Reads today's trades from the DB, computes PnL, writes a structured entry to the `journal` table, and sends a summary to Telegram.

## When to Use

- Cron: every weekday at 15:45 IST (10:15 UTC)
- User asks "how did we do today?" or "show today's trades"

## How to Run

```bash
cd /opt/dhan-trading
set -a && source .env && set +a
python3 ~/.hermes/skills/dhan/trade_reflection/scripts/reflect.py
```

## Telegram Output Example

```
📈 End of Day | 03 Jun 2026

Trades: 2 | Wins: 1 | Losses: 1
Realized PnL: +₹245.00
Open positions: 0 (all squared off)

RELIANCE: BUY @ ₹1,340.50 → EXIT @ ₹1,358.20  +₹177.00  ✅
INFY:     SELL @ ₹1,295.00 → SL ₹1,302.45      -₹75.00   ❌

Running total (Jun): +₹420.00
Daily limit remaining: ₹4,755.00

Journal entry written.
```
