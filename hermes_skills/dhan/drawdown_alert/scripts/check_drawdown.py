#!/usr/bin/env python3
"""
Drawdown alert — autonomous watcher.
Runs every 5 min during market hours (cron: */5 3-10 * * 1-5).
SILENT on safe drawdown. Only outputs when threshold is crossed so
Hermes delivers a Telegram alert only when action is needed.
"""
import os, sys
from datetime import date
sys.path.insert(0, "/opt/dhan-trading")
from dotenv import load_dotenv; load_dotenv("/opt/dhan-trading/.env")
from urllib.parse import quote_plus
from db import init_db, get_session
from config import get_config
from sqlalchemy import text

cfg = get_config()
init_db(f"postgresql+psycopg2://{quote_plus(cfg.db_user)}:{quote_plus(cfg.db_password)}"
        f"@{cfg.db_host}:{cfg.db_port}/{cfg.db_name}")

today = date.today()
with get_session() as s:
    row = s.execute(text(
        "SELECT realized_pnl, trades_count FROM daily_pnl WHERE date=:d"
    ), {"d": today}).fetchone()

realized = float(row[0]) if row else 0.0
trades   = int(row[1])   if row else 0
max_loss = cfg.max_daily_loss
pct      = abs(realized) / max_loss * 100 if realized < 0 else 0.0

# Only alert on threshold crossings — stay silent if safe
ALERT_THRESHOLDS = [50, 75, 90]
for threshold in sorted(ALERT_THRESHOLDS, reverse=True):
    if pct >= threshold:
        icon = "⛔" if threshold >= 90 else ("🔴" if threshold >= 75 else "🟡")
        print(f"{icon} DRAWDOWN ALERT {threshold}%+ | {today}")
        print(f"Loss today: ₹{abs(realized):,.0f} / ₹{max_loss:,.0f} ({pct:.1f}%)")
        print(f"Trades: {trades}")
        if pct >= 90:
            print("⛔ Kill-switch will fire soon — reduce positions now.")
        break
# If pct < 50: no output → Hermes stays silent, no Telegram message
