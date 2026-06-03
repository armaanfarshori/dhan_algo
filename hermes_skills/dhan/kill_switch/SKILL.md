---
name: kill_switch
description: Emergency halt — immediately stops all trading, squares off open positions, cancels pending orders, sends critical alert to Telegram.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Emergency, Kill-switch, Safety, Critical]
    category: dhan-trading
---

# Kill Switch Skill

**EMERGENCY USE ONLY.** Immediately halts all trading activity.

## When to Use

- User says "stop trading", "kill switch", "emergency halt", "halt all"
- Unexpected market event or system error
- Daily loss limit approaching

## Safety

This skill is **fail-safe** — if any step errors, it attempts the next step anyway and logs everything. It will NOT restart automatically. To resume trading, explicitly run `hermes chat` and say "resume trading" after verifying the situation.

## How to Run

```bash
cd /opt/dhan-trading
set -a && source .env && set +a
python3 ~/.hermes/skills/dhan/kill_switch/scripts/halt.py
```

Or via Telegram: send "HALT" to your bot.

## Actions Taken (in order)

1. Write `KILL_SWITCH=true` to `~/.hermes/kill_switch.lock`
2. Call `RiskManager.activate_kill_switch()` if agent is running
3. Query open positions via Dhan API → place market SELL orders (paper mode: log only)
4. Cancel all pending orders
5. Update `runs` table: status = 'halted', reason logged
6. Send CRITICAL Telegram alert
7. Stop `dhan-agent` systemd service

## Telegram Alert

```
🚨 KILL SWITCH ACTIVATED — 03 Jun 2026 14:32 IST

All trading HALTED.
Open positions squared off: 2
Pending orders cancelled: 0
Reason: manual operator command

To resume: hermes chat → "resume trading after confirming situation"
```
