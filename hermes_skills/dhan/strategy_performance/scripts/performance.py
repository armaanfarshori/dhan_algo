#!/usr/bin/env python3
"""
Strategy performance report — autonomous weekly.
Cron: 30 3 * * 0 (09:00 IST Sunday = 03:30 UTC).
Always sends — this is the weekly report, not a threshold alert.
"""
import sys, math
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
month_ago= date.today() - timedelta(days=30)

with get_session() as s:
    # Weekly trades
    wk = s.execute(text("""
        SELECT COUNT(*) AS n,
               SUM(pnl) AS total,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
               AVG(pnl) AS avg_pnl,
               MIN(pnl) AS worst,
               MAX(pnl) AS best
        FROM trades WHERE exit_ts::date >= :w AND status='CLOSED'
    """), {"w": week_ago}).fetchone()

    # Monthly trades (for Sharpe estimate)
    mo = s.execute(text("""
        SELECT date, realized_pnl FROM daily_pnl
        WHERE date >= :m ORDER BY date
    """), {"m": month_ago}).fetchall()

daily_pnls = [float(r[1]) for r in mo] if mo else []
avg_daily  = sum(daily_pnls)/len(daily_pnls) if daily_pnls else 0
std_daily  = math.sqrt(sum((x-avg_daily)**2 for x in daily_pnls)/len(daily_pnls)) if len(daily_pnls) > 1 else 1
sharpe     = (avg_daily / std_daily) * math.sqrt(252) if std_daily > 0 else 0

n     = int(wk[0]) if wk[0] else 0
total = float(wk[1] or 0)
wins  = int(wk[2] or 0)
avg   = float(wk[3] or 0)
worst = float(wk[4] or 0)
best  = float(wk[5] or 0)
wr    = wins/n*100 if n > 0 else 0

print(f"📊 WEEKLY PERFORMANCE | {week_ago} → {date.today()}")
print(f"Trades: {n}  |  Win rate: {wr:.0f}%  |  P&L: {'+'if total>=0 else ''}₹{total:,.0f}")
print(f"Avg trade: {'+'if avg>=0 else ''}₹{avg:,.0f}  |  Best: +₹{best:,.0f}  |  Worst: ₹{worst:,.0f}")
print(f"Monthly Sharpe (est.): {sharpe:.2f}")
if n == 0:
    print("\nNo closed trades this week — paper mode active, agent may not be running.")
elif wr < 40:
    print("\n⚠️ Win rate below 40% — consider reviewing strategy parameters.")
elif sharpe < 0.5:
    print("\n⚠️ Sharpe below 0.5 — returns may not justify risk.")
else:
    print("\n✅ Performance within expected range.")
