"""EOD F&O paper-trade runner.

Records one vol-gated iron-condor paper entry per weekly cycle from the real
option chain (core.fno_paper.record_paper_entry) and resolves any matured
positions at expiry (resolve_paper_trades). Idempotent (ON CONFLICT DO NOTHING),
so safe to run daily.

Scheduled via cron AFTER core.fno_collector (which writes the live-shape chain
snapshot with spot into option_chain_snapshot). PAPER/forward-log only — this is
the real-IV forward truth test of the iron-condor edge.

    python3 scripts/fno_paper_eod.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402
from config import get_config  # noqa: E402
import core.fno_paper as fp  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("dhan.fno_paper_eod")


def main() -> None:
    db.init_db(get_config().db_url)
    entry = fp.record_paper_entry()
    resolved = fp.resolve_paper_trades()
    logger.info("fno_paper EOD — entry recorded=%s reason=%s | resolved=%d",
                entry.get("recorded"), entry.get("reason"), resolved)
    logger.info("fno_paper summary: %s", fp.paper_summary())


if __name__ == "__main__":
    main()
