---
name: sector_heatmap
description: Rank NSE sectors by today's price move %. Use to filter ORB signals — avoid longs in sectors down >1%.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Sectors,NSE,Heatmap,Signal]
    category: dhan-trading
---

# Sector Heatmap Skill

Ranks NSE sectors by today's % change to filter signal direction.

## When to Use
- Cron: 09:30 IST weekdays (`0 4 * * 1-5`)
- User asks 'how are sectors doing?'

## How Hermes handles it
Use Dhan API `/api/scanner` or web search NSE sector performance.
Compare signal direction against sector trend — block longs in down sectors.
