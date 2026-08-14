"""Intraday F&O live loop (one tick) — cron every ~5 min during market hours.

Each tick, while the market is open:
  1. Snapshots the LIVE NIFTY option chain (nearest expiry) into
     option_chain_snapshot via snapshot_option_chain(allow_market_hours=True).
     This keeps /api/fno/chain fresh (the dashboard Chain panel reads the latest
     snapshot) — i.e. a near-live chain on the dashboard.
  2. Records the vol-gated iron-condor paper entry for the current weekly cycle
     (idempotent — ON CONFLICT DO NOTHING, so only one entry per expiry) and
     resolves any matured positions at expiry.

Off-hours it exits immediately (the EOD cron / scripts/fno_paper_eod.py covers
the post-close run). PAPER / forward-log only — no live orders.

    python3 scripts/fno_intraday.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402
from config import get_config  # noqa: E402
import core.fno_backfill as fb  # noqa: E402
import core.fno_paper as fp  # noqa: E402
from core.client import DhanClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("dhan.fno_intraday")


async def _snapshot_live(cfg) -> dict:
    token = await fb.resolve_access_token()
    async with DhanClient(
        cfg.dhan_client_id, token,
        proxy_url=cfg.dhan_proxy_url or None,
        proxy_categories=cfg.dhan_proxy_categories_set,
    ) as client:
        return await fb.snapshot_option_chain(
            client, "NIFTY", allow_market_hours=True,
        )


def main() -> None:
    cfg = get_config()
    db.init_db(cfg.db_url)

    if not fb.is_market_hours():
        logger.info("fno_intraday: off-hours — skipping (EOD cron handles post-close)")
        return

    snap = asyncio.run(_snapshot_live(cfg))
    logger.info("fno_intraday: live chain snapshot %s", snap)

    entry = fp.record_paper_entry()
    resolved = fp.resolve_paper_trades()
    logger.info("fno_intraday: paper entry recorded=%s reason=%s | resolved=%d",
                entry.get("recorded"), entry.get("reason"), resolved)
    logger.info("fno_intraday: summary %s", fp.paper_summary())


if __name__ == "__main__":
    main()
