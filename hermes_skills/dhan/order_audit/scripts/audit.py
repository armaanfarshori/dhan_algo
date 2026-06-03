#!/usr/bin/env python3
"""
Order audit — autonomous EOD check.
Cron: 15 10 * * 1-5 (15:45 IST = 10:15 UTC, after market close).
SILENT when clean. Alerts on orphaned open orders.
"""
import asyncio, sys
from datetime import date
sys.path.insert(0, "/opt/dhan-trading")
from dotenv import load_dotenv; load_dotenv("/opt/dhan-trading/.env")
from urllib.parse import quote_plus
from db import init_db, get_session
from config import get_config
from core.client import DhanClient
from sqlalchemy import text

async def main():
    cfg = get_config()
    init_db(f"postgresql+psycopg2://{quote_plus(cfg.db_user)}:{quote_plus(cfg.db_password)}"
            f"@{cfg.db_host}:{cfg.db_port}/{cfg.db_name}")

    # Orders in DB marked OPEN or PENDING from today
    with get_session() as s:
        db_open = s.execute(text("""
            SELECT id, dhan_order_id, security_id, side, qty, status
            FROM orders WHERE status IN ('OPEN','PENDING')
              AND created_at::date = :d
        """), {"d": date.today()}).fetchall()

    # Live order book from Dhan
    async with DhanClient(cfg.dhan_client_id, cfg.dhan_access_token) as client:
        live_orders = await client.get_order_book()
    live_open = {o.get("orderId"): o.get("orderStatus","")
                 for o in live_orders if o.get("orderStatus") in ("PENDING","TRANSIT")}

    orphans = [r for r in db_open if r[1] not in live_open]

    if orphans:
        print(f"⚠️ ORPHANED ORDERS | {date.today()} EOD")
        for o in orphans:
            print(f"  DB id={o[0]} dhan_id={o[1]} {o[3]} {o[4]}× {o[2]} (status={o[5]})")
        print("These orders exist in DB but not in Dhan order book — reconcile manually.")
    if live_open:
        print(f"\n⚠️ OPEN ORDERS AT CLOSE: {len(live_open)} orders still pending in Dhan")
        for oid, status in live_open.items():
            print(f"  {oid}: {status}")
    # Silent if no orphans and no open orders

asyncio.run(main())
