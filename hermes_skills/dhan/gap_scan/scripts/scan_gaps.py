#!/usr/bin/env python3
"""
Gap scanner — autonomous.
Cron: 0 21 * * * (02:30 IST = 21:00 UTC, nightly after data_quality).
SILENT when no gaps. Alerts with securities needing backfill.
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

today    = date.today()
yesterday= today - timedelta(days=1)
# Skip if yesterday was weekend
if yesterday.weekday() >= 5:
    sys.exit(0)

with get_session() as s:
    # Securities that have bars but are missing yesterday's data
    gaps = s.execute(text("""
        SELECT security_id FROM (
            SELECT DISTINCT security_id FROM bars
            WHERE timeframe='1m' AND time >= NOW() - INTERVAL '30 days'
        ) active
        WHERE security_id NOT IN (
            SELECT DISTINCT security_id FROM bars
            WHERE timeframe='1m' AND time::date = :yd
        )
        ORDER BY security_id
        LIMIT 50
    """), {"yd": yesterday}).fetchall()

gap_ids = [r[0] for r in gaps]

if gap_ids:
    print(f"⚠️ DATA GAPS DETECTED | {len(gap_ids)} securities missing {yesterday}")
    sample = ", ".join(gap_ids[:10])
    print(f"  First 10: {sample}{'...' if len(gap_ids)>10 else ''}")
    print(f"\nTo fix:")
    print(f"  python backfill.py --ids {','.join(gap_ids[:20])} --from {yesterday} --to {today}")
# Silent when all securities have yesterday's data
