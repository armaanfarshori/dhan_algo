#!/usr/bin/env python3
"""
Kronos signal calibration — autonomous weekly (Telegram-friendly summary).
Cron: 0 4 * * 0 (09:30 IST Sunday = 04:00 UTC).

Delegates to ml/calibration.py — outcomes over the model's ACTUAL 30-minute
forecast horizon (the old version compared against next-DAY moves, which is
not what score_from_db predicts). Decisions scored on stale bars are
excluded from the recommendation by construction.

The weekday EOD cron does the heavy `fill`; this just tops up and reports.
"""
import sys

sys.path.insert(0, "/opt/dhan-trading")
from dotenv import load_dotenv

load_dotenv("/opt/dhan-trading/.env")

from config import get_config
from db import init_db

init_db(get_config().db_url)

from ml.calibration import build_report, fill_outcomes

filled = fill_outcomes(days=14)
rep = build_report(days=30)

ma = rep["model_accuracy"]
gv = rep["gate_value"]
print(f"📊 KRONOS CALIBRATION (30-min horizon, last {rep['window_days']}d)")
print(f"Outcomes: {rep['rows_with_outcomes']} (+{filled} new) | "
      f"fresh-data: {ma['fresh']['n']} acc={ma['fresh']['accuracy']} | "
      f"stale excluded: {ma['stale_excluded']['n']}")
print(f"Gate  ALLOW n={gv['allow']['n']} avg={gv['allow']['avg_return_bps']}bps | "
      f"BLOCK n={gv['block']['n']} avg={gv['block']['avg_return_bps']}bps")
print(f"► {rep['recommendation']}")
