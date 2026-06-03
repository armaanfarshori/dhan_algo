---
name: options_expiry_watch
description: Alert on Thursday mornings (weekly NSE F&O expiry). Reminds to widen ORB range multiplier for higher volatility.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Options,Expiry,NSE,Thursday]
    category: dhan-trading
---

# Options Expiry Watch Skill

Thursday-morning alert about weekly NSE F&O expiry and its effect on ORB.

## Cron Schedule
08:45 IST every Thursday: `15 3 * * 4`

## Alert
'⚡ Weekly F&O expiry today — expect higher volatility on Nifty/BankNifty underlying.
Recommendation: widen ORB range multiplier to 2.0×, raise Kronos threshold to 0.6.'
