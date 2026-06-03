---
name: kronos_forecast
description: Dynamically screen the top 20 most volatile NSE equities from the bars table, run Kronos OHLCV foundation model on each, write BUY/SELL/HOLD signals with confidence scores to the signals table.
version: 0.2.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Kronos, AI, Signals, NSE, Screener, Volatility]
    category: dhan-trading
---

# Kronos Forecast Skill

**Dynamic screener + Kronos AI forecast.** Does NOT use a static watchlist.

## How it picks securities

**Pre-market mode (default):** Queries the `bars` hypertable for the last 30 trading days,
computes normalized ATR% `(day_high - day_low) / close` per security, filters out
illiquid stocks (avg volume < 50K), and selects the top 20 by volatility. These are
the stocks most likely to produce clean ORB setups.

**Live mode (`--live`):** During market hours, fetches today's OHLC from Dhan for the
entire bars universe (~500 instruments) and ranks by today's intraday price move %.
Captures stocks that are already running or breaking out.

## Screener logic (core/nse_screener.py)

```
ATR% = avg over 30d of: (day_high - day_low) / close
Filter: avg_daily_volume >= 50,000 shares
Rank: top 20 by ATR%
```

## When to Use

- Pre-market 08:45 IST (cron): identify today's volatile setup candidates
- User says "run Kronos", "what should we trade?", "volatility scan"
- Intraday: `--live` to catch breakouts already in motion

## Prerequisites

- `bars` table must have ≥10 trading days for at least 20 securities
- HuggingFace model auto-downloads on first run (~300MB, cached after)
- DB connection via `/opt/dhan-trading/.env`

## How to Run

```bash
cd /opt/dhan-trading
set -a && source .env && set +a

# Pre-market: top 20 by 30-day ATR% (default)
python3 ~/.hermes/skills/dhan/kronos_forecast/scripts/forecast.py

# Top 30 instead of 20
python3 ~/.hermes/skills/dhan/kronos_forecast/scripts/forecast.py --n 30

# Live (intraday): rank by today's price move
python3 ~/.hermes/skills/dhan/kronos_forecast/scripts/forecast.py --live

# Dry run: score but don't write to signals table
python3 ~/.hermes/skills/dhan/kronos_forecast/scripts/forecast.py --dry-run
```

## Output Format

```
────────────────────────────────────────────────────
  Kronos Forecast  |  04 Jun 2026 08:45 IST
────────────────────────────────────────────────────
Selecting top 20 volatile NSE equities...

Selected 20 securities:
   1.     2885  ATR%=1.82%  avgVol=4,521,230
   2.     1333  ATR%=1.71%  avgVol=8,102,445
   3.     3045  ATR%=1.68%  avgVol=6,234,100
  ...

Loading Kronos model...

Scoring with Kronos (20 securities):

  📈     2885  BUY   score=0.0031  conf=0.71  Δ+0.31%  [ATR%=1.82%  avgVol=4.5M]
  📉     1333  SELL  score=0.0018  conf=0.63  Δ-0.18%  [ATR%=1.71%  avgVol=8.1M]
  ➖     3045  HOLD  score=0.0002  conf=0.45  Δ+0.02%  [ATR%=1.68%  avgVol=6.2M]
  ...

────────────────────────────────────────────────────
  Results: 8 BUY  |  6 SELL  |  6 HOLD
  20 signals written to signals table.

  Strongest BUY : security 2885  conf=0.71  Δ+0.31%
  Strongest SELL: security 1333  conf=0.63  Δ-0.18%
────────────────────────────────────────────────────
```
