#!/usr/bin/env python3
"""
Data quality scanner — autonomous.
Cron: 30 20 * * * (02:00 IST = 20:30 UTC, nightly).
SILENT when clean. Alerts with specifics if anomalies found.
"""
import sys
from datetime import date, timedelta
sys.path.insert(0, "/opt/dhan-trading")
from dotenv import load_dotenv; load_dotenv("/opt/dhan-trading/.env")
from urllib.parse import quote_plus
from db import init_db, get_session
from config import get_config
from sqlalchemy import text

cfg = get_config()
init_db(f"postgresql+psycopg2://{quote_plus(cfg.db_user)}:{quote_plus(cfg.db_password)}"
        f"@{cfg.db_host}:{cfg.db_port}/{cfg.db_name}")

issues = []
window = date.today() - timedelta(days=7)

with get_session() as s:
    # 1. Zero-volume candles (suspicious)
    r = s.execute(text("""
        SELECT COUNT(*), COUNT(DISTINCT security_id)
        FROM bars WHERE timeframe='1m' AND volume=0 AND time >= :w
    """), {"w": window}).fetchone()
    if r[0] > 0:
        issues.append(f"Zero-volume candles: {r[0]:,} across {r[1]} securities (last 7d)")

    # 2. OHLC violations (high < low, open outside high/low)
    r = s.execute(text("""
        SELECT COUNT(*) FROM bars WHERE timeframe='1m'
          AND (high < low OR open > high OR open < low OR close > high OR close < low)
          AND time >= :w
    """), {"w": window}).fetchone()
    if r[0] > 0:
        issues.append(f"OHLC violations: {r[0]:,} rows (h<l or price outside range)")

    # 3. Price spikes >15% in a single 1m bar
    r = s.execute(text("""
        SELECT COUNT(*), COUNT(DISTINCT security_id)
        FROM bars WHERE timeframe='1m'
          AND (high - low) / NULLIF(close, 0) > 0.15 AND time >= :w
    """), {"w": window}).fetchone()
    if r[0] > 0:
        issues.append(f"Price spikes >15%: {r[0]:,} candles across {r[1]} securities")

    # 4. Duplicate timestamps
    r = s.execute(text("""
        SELECT COUNT(*) FROM (
            SELECT security_id, timeframe, time, COUNT(*)
            FROM bars WHERE time >= :w
            GROUP BY 1,2,3 HAVING COUNT(*) > 1
        ) dup
    """), {"w": window}).fetchone()
    if r[0] > 0:
        issues.append(f"Duplicate bar timestamps: {r[0]:,} sets")

if issues:
    print(f"⚠️ DATA QUALITY ISSUES | {date.today()}")
    for issue in issues:
        print(f"  • {issue}")
    print("\nRun: python backfill.py --nse-eq --all to re-backfill affected securities.")
# Silent on clean
