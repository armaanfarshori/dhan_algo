---
name: earnings_calendar
description: Check NSE earnings schedule for watchlist securities. Alerts 2 days before any earnings to avoid trading into gaps.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Earnings,Calendar,Risk,NSE]
    category: dhan-trading
---

# Earnings Calendar Skill

Checks NSE earnings calendar for stocks in the watchlist and alerts 2 days before any results.

## When to Use
- Cron: 08:00 IST daily (`30 2 * * 1-5`)
- User asks 'any earnings this week?'

## How Hermes handles it
Search NSE earnings calendar at moneycontrol.com or NSE India for upcoming results.
Alert format: '⚠️ HDFCBANK Q4 results in 2 days — avoid ORB entry today/tomorrow'
