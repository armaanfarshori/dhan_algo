#!/usr/bin/env python3
"""Backfill gap checker — called by Hermes backfill_check skill."""
import argparse, os, subprocess, sys
from datetime import date, timedelta
sys.path.insert(0, "/opt/dhan-trading")
from dotenv import load_dotenv; load_dotenv("/opt/dhan-trading/.env")
from db import init_db, get_session
from sqlalchemy import text

def last_bar_date(session, security_id, timeframe="1m"):
    row = session.execute(
        text("SELECT MAX(time)::date FROM bars WHERE security_id=:s AND timeframe=:t"),
        {"s": security_id, "t": timeframe},
    ).fetchone()
    return row[0] if row else None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("--ids", help="Comma-separated security IDs")
    args = parser.parse_args()

    cfg_ids = os.getenv("WATCHLIST_SECURITY_IDS", "2885,1333,1594,11536").split(",")
    ids = [s.strip() for s in args.ids.split(",")] if args.ids else cfg_ids

    db_url = (
        f"postgresql+psycopg2://{os.getenv('DB_USER','trader')}:"
        f"{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST','localhost')}:"
        f"{os.getenv('DB_PORT','5432')}/{os.getenv('DB_NAME','dhan_trading')}"
    )
    init_db(db_url)
    today = date.today()
    gaps = []

    with get_session() as session:
        for sid in ids:
            last = last_bar_date(session, sid)
            if last is None:
                print(f"  {sid}: NO DATA — never backfilled")
                gaps.append(sid)
            elif (today - last).days > 1:
                print(f"  {sid}: GAP — last bar {last}, missing {(today-last).days-1} day(s)")
                gaps.append(sid)
            else:
                print(f"  {sid}: OK — last bar {last}")

    if gaps and args.fix:
        print(f"\nBackfilling {len(gaps)} securities...")
        result = subprocess.run(
            ["/opt/dhan-trading/.venv/bin/python3", "/opt/dhan-trading/backfill.py",
             "--ids", ",".join(gaps), "--all"],
            capture_output=True, text=True, cwd="/opt/dhan-trading",
        )
        print(result.stdout[-500:] if result.stdout else "")
        if result.returncode == 0:
            print("Backfill complete.")
        else:
            print(f"Backfill error: {result.stderr[-200:]}")
    elif gaps:
        print(f"\n{len(gaps)} gap(s) found. Run with --fix to backfill.")
    else:
        print("\nAll data is fresh.")

if __name__ == "__main__":
    main()
