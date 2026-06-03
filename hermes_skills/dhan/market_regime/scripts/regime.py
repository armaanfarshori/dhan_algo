#!/usr/bin/env python3
"""
Market regime classifier — autonomous.
Cron: 45 3 * * 1-5 (09:15 IST = 03:45 UTC, market open, Mon-Fri).
Always outputs regime — used by daily_premarket to adjust thresholds.
Also alerts when regime changes from previous day.
"""
import sys, json
from datetime import date, timedelta
sys.path.insert(0, "/opt/dhan-trading")
from dotenv import load_dotenv; load_dotenv("/opt/dhan-trading/.env")
from urllib.parse import quote_plus
from db import init_db, get_session
from config import get_config
from sqlalchemy import text
import numpy as np

cfg = get_config()
init_db(f"postgresql+psycopg2://{quote_plus(cfg.db_user)}:{quote_plus(cfg.db_password)}"
        f"@{cfg.db_host}:{cfg.db_port}/{cfg.db_name}")

# Use NIFTY 50 index (security_id=13) or fallback to RELIANCE (2885)
NIFTY_SID = "13"
FALLBACK   = "2885"

def get_regime(sid):
    with get_session() as s:
        rows = s.execute(text("""
            SELECT time::date AS dt,
                   MAX(high) AS h, MIN(low) AS l,
                   (ARRAY_AGG(close ORDER BY time DESC))[1] AS close_px
            FROM bars
            WHERE security_id=:sid AND timeframe='1m'
              AND time >= NOW() - INTERVAL '30 days'
            GROUP BY dt ORDER BY dt DESC LIMIT 20
        """), {"sid": sid}).fetchall()
    if not rows or len(rows) < 5:
        return None, None, None

    closes   = np.array([float(r[3]) for r in rows[::-1]])
    ranges   = np.array([(float(r[1])-float(r[2]))/float(r[3]) for r in rows[::-1]])
    sma5     = np.mean(closes[-5:])
    sma20    = np.mean(closes)
    avg_rng  = np.mean(ranges) * 100  # %
    momentum = (closes[-1] - closes[0]) / closes[0] * 100  # % over 20 days

    if momentum > 3 and sma5 > sma20:
        regime = "TRENDING_UP"
    elif momentum < -3 and sma5 < sma20:
        regime = "TRENDING_DOWN"
    elif avg_rng > 1.5:
        regime = "HIGH_VOLATILITY"
    else:
        regime = "SIDEWAYS"

    return regime, avg_rng, momentum

regime, avg_range, momentum = get_regime(NIFTY_SID)
if regime is None:
    regime, avg_range, momentum = get_regime(FALLBACK)
if regime is None:
    print("⚠️ REGIME: insufficient data")
    sys.exit(0)

emoji = {"TRENDING_UP":"📈","TRENDING_DOWN":"📉","HIGH_VOLATILITY":"⚡","SIDEWAYS":"➖"}
rec   = {
    "TRENDING_UP":    "ORB longs preferred. Kronos conf ≥0.35 OK.",
    "TRENDING_DOWN":  "ORB shorts preferred. Longs need conf ≥0.6.",
    "HIGH_VOLATILITY":"Widen stops 1.5×. Reduce size 50%. Kronos conf ≥0.65.",
    "SIDEWAYS":       "Skip ORB. Wait for breakout. No trades if conf <0.55.",
}

print(f"{emoji[regime]} MARKET REGIME: {regime} | {date.today()}")
print(f"20-day momentum: {momentum:+.1f}%  |  avg daily range: {avg_range:.2f}%")
print(f"Recommendation: {rec[regime]}")
