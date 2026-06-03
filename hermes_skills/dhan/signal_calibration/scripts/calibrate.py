#!/usr/bin/env python3
"""
Kronos signal calibration — autonomous weekly.
Cron: 0 4 * * 0 (09:30 IST Sunday = 04:00 UTC).
Compares signal direction vs actual next-session price move.
Always outputs — informs fine-tuning decision.
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

week_ago = date.today() - timedelta(days=7)

with get_session() as s:
    # Join signals with next day's actual price move
    rows = s.execute(text("""
        WITH sig AS (
            SELECT s.id, s.security_id, s.side, s.confidence, s.ts::date AS sig_date
            FROM signals s
            WHERE s.strategy LIKE '%kronos%' AND s.ts::date >= :w
        ),
        actual AS (
            SELECT b.security_id,
                   b.time::date AS bar_date,
                   (ARRAY_AGG(b.close ORDER BY b.time DESC))[1] AS close_px,
                   (ARRAY_AGG(b.open  ORDER BY b.time ASC ))[1] AS open_px
            FROM bars b WHERE b.timeframe='1m' AND b.time::date >= :w
            GROUP BY b.security_id, b.time::date
        )
        SELECT sig.side,
               CASE WHEN actual.close_px > actual.open_px THEN 'UP' ELSE 'DOWN' END AS actual_dir,
               sig.confidence
        FROM sig
        JOIN actual ON actual.security_id = sig.security_id
                    AND actual.bar_date = sig.sig_date + 1
        WHERE sig.side IN ('BUY','SELL')
    """), {"w": week_ago}).fetchall()

if not rows:
    print(f"📊 SIGNAL CALIBRATION | {week_ago} → {date.today()}")
    print("No Kronos signals with next-day data yet.")
    sys.exit(0)

total = len(rows)
correct = sum(1 for r in rows if
              (r[0]=='BUY' and r[1]=='UP') or (r[0]=='SELL' and r[1]=='DOWN'))
accuracy = correct / total * 100

high_conf = [r for r in rows if float(r[2] or 0) >= 0.6]
hc_correct= sum(1 for r in high_conf if
               (r[0]=='BUY' and r[1]=='UP') or (r[0]=='SELL' and r[1]=='DOWN'))
hc_acc    = hc_correct/len(high_conf)*100 if high_conf else 0

print(f"📊 KRONOS SIGNAL CALIBRATION | {week_ago} → {date.today()}")
print(f"Signals evaluated: {total}")
print(f"Direction accuracy (all):        {accuracy:.1f}%")
print(f"Direction accuracy (conf ≥0.6):  {hc_acc:.1f}%  ({len(high_conf)} signals)")

if accuracy < 50:
    print("\n⚠️ Below random baseline. Fine-tuning on NSE data is strongly recommended.")
elif accuracy < 55:
    print("\n🟡 Marginal edge. Consider fine-tuning to improve NSE-specific accuracy.")
else:
    print("\n✅ Meaningful edge. Continue monitoring — fine-tune when >500 securities loaded.")
