#!/usr/bin/env python3
"""
Position reconciler — autonomous watcher.
Runs every 30 min during market hours (cron: */30 3-10 * * 1-5).
SILENT when positions match. Alerts only on mismatch.
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

    with get_session() as s:
        db_rows = s.execute(text("""
            SELECT DISTINCT ON (security_id) security_id, qty
            FROM positions WHERE qty != 0
            ORDER BY security_id, time DESC
        """)).fetchall()
    db_pos = {r[0]: int(r[1]) for r in db_rows}

    async with DhanClient(cfg.dhan_client_id, cfg.dhan_access_token) as client:
        live = await client.get_positions()
    live_pos = {str(p.get("securityId","")): p.get("netQty", 0)
                for p in live if p.get("netQty", 0) != 0}

    breaks = {sid for sid in set(db_pos)|set(live_pos)
              if db_pos.get(sid, 0) != live_pos.get(sid, 0)}

    if breaks:
        print(f"⚠️ POSITION MISMATCH | {date.today()}")
        for sid in breaks:
            print(f"  {sid}: DB={db_pos.get(sid,0)}  Dhan={live_pos.get(sid,0)}")
        print("Manual reconciliation required.")
    # If no breaks: silent — no Telegram alert

asyncio.run(main())
