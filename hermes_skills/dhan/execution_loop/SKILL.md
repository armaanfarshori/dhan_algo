---
name: execution_loop
description: Intraday monitoring loop — polls live quotes, checks ORB + Kronos signals, routes through RiskManager, places paper/live orders.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Intraday, Execution, ORB, Live, Monitoring]
    category: dhan-trading
---

# Execution Loop Skill

Monitors live price action during NSE market hours (09:15–15:30 IST). Feeds tick data to the ORB strategy, gates entries through Kronos, checks RiskManager, and places orders.

**This skill is what `main.py` already does.** Invoke this skill to start/check/stop the main trading agent.

## When to Use

- Market open (09:15 IST): start the agent if not running
- User asks "is the agent running?" or "start trading"
- Check current positions and signals

## How to Run

```bash
# Check if agent is running
systemctl status dhan-agent

# Start for the session
cd /opt/dhan-trading && set -a && source .env && set +a
python3 main.py &

# Or via systemd (auto-starts at 09:00 IST via cron)
sudo systemctl start dhan-agent
```

## Status Output

```
Agent status: RUNNING (pid 12345, uptime 1h 23m)
Mode: PAPER | Strategy: ORB (15-min window)
Open positions: 1
  RELIANCE: LONG 1 share @ ₹1,340.50 (entry 09:32 IST)
  OR High: ₹1,345.20 | OR Low: ₹1,335.10 | Range: ₹10.10
  Target: ₹1,355.65 | Stop: ₹1,332.43
Signals today: 3 (2 taken, 1 Kronos-blocked)
PnL today: +₹177.00 (realized)
```
