---
name: drawdown_alert
description: Check today's realized PnL against MAX_DAILY_LOSS. Sends Telegram alert when drawdown crosses 50%, 75%, or 90% of the daily limit.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Risk, PnL, Drawdown, Alert]
    category: dhan-trading
---

# Drawdown Alert Skill

Reads `daily_pnl` for today, computes loss as % of MAX_DAILY_LOSS, and alerts at key thresholds. Runs every 5 minutes during market hours via cron.

## When to Use

- Cron: every 5 min during market hours (09:15–15:30 IST)
- User asks "what's my drawdown?" or "how much have I lost today?"
- Proactively before making any manual trades

## How to Run

```bash
cd /opt/dhan-trading && set -a && source .env && set +a
python3 ~/.hermes/skills/dhan/drawdown_alert/scripts/check_drawdown.py
```

## Output

```
=== Drawdown Check | 03 Jun 2026 ===
Realized PnL today: -₹1,250.00
MAX_DAILY_LOSS:      ₹5,000.00
Drawdown:            25.0%  🟡 CAUTION

Thresholds:  ✅ <50%  ⬜ 75%  ⬜ 90%
```
