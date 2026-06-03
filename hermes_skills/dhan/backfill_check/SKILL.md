---
name: backfill_check
description: Check NSE OHLCV data freshness in TimescaleDB, detect gaps, trigger incremental backfill if needed.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [NSE, Data, TimescaleDB, Backfill]
    category: dhan-trading
---

# Backfill Check Skill

Queries the `bars` hypertable to verify data freshness for all instruments in the watchlist. If any security is missing recent bars (gap > 1 trading day), triggers `backfill.py` for only the gap period.

## When to Use

- Morning pre-market (before 09:00 IST): verify yesterday's data landed
- After any system restart: confirm no gaps were introduced
- User asks "is the data up to date?" or "any data gaps?"
- Weekly health check

## Prerequisites

- `/opt/dhan-trading/.env` with `DB_HOST`, `DB_PASSWORD`, `DHAN_*` creds
- TimescaleDB running on `DB_HOST:5432`
- Python venv at `/opt/dhan-trading/.venv`

## How to Run

```bash
cd /opt/dhan-trading
set -a && source .env && set +a
python3 ~/.hermes/skills/dhan/backfill_check/scripts/check.py
```

## Quick Reference

```
python3 check.py              # check all watchlist securities
python3 check.py --fix        # check and auto-backfill gaps
python3 check.py --ids 2885   # check specific security
```

## Output Format

```
RELIANCE (2885): OK — last bar 2026-06-03 15:29 IST, 22125 bars today
HDFCBANK (1333): GAP — missing 2026-06-02, triggering backfill...
Backfill complete. 1500 bars inserted.
```
