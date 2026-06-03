---
name: kronos_forecast
description: Run Kronos OHLCV foundation model on all watchlist securities, write scored signals to the signals table.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Kronos, AI, Signals, NSE]
    category: dhan-trading
---

# Kronos Forecast Skill

Loads the last 400 1-min bars per security from TimescaleDB, runs `KronosSignalEngine.score_batch()`, and writes BUY/SELL/HOLD signals with confidence scores to the `signals` table.

## When to Use

- Pre-market: 08:45–09:00 IST — generate morning signals before ORB window opens
- User asks "what does Kronos say about RELIANCE?" or "run the forecast"
- After significant market moves: re-score to check if signals flipped

## Prerequisites

- `bars` table populated (run `backfill_check` first if unsure)
- HuggingFace model downloaded (`NeoQuasar/Kronos-small` — auto-downloads on first run, ~300MB)

## How to Run

```bash
cd /opt/dhan-trading
set -a && source .env && set +a
python3 ~/.hermes/skills/dhan/kronos_forecast/scripts/forecast.py
```

## Output Format

```
RELIANCE (2885): BUY   score=0.0031  confidence=0.71  forecast_return=+0.31%
HDFCBANK (1333): HOLD  score=0.0002  confidence=0.45  forecast_return=+0.02%
INFY     (1594): SELL  score=0.0018  confidence=0.63  forecast_return=-0.18%
TCS      (11536): BUY  score=0.0025  confidence=0.68  forecast_return=+0.25%
4 signals written to signals table.
```
