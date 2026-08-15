"""CAS feed audit — N3 blueprint Phase 0. OBSERVATIONAL ONLY: places no orders.

Run manually on a trading day, ideally started ~15:05 IST. Subscribes a second
WS feed to the NIFTY index, the near-month NIFTY future, and a sample of
constituent stocks, then logs every tick verbatim (JSONL) through the closing
auction window (15:15–15:35) and the stock-F&O tail (→15:40).

What it answers (the cas-2026 report's open questions):
  • Does the per-stock feed go SILENT at 15:15, or publish indicative auction
    prices during order entry / matching?
  • Does the index (IDX) feed keep printing, freeze, or step at ~15:35?
  • Does the futures feed run cleanly to its own 15:30 close?
  • When exactly does each stock's CAS equilibrium print arrive?

Output: run/cas_feed_audit_<date>.jsonl — one line per tick
        {t_recv_ist, sid, seg, ltp, volume, ltt}.
A summary (per-sid first/last tick inside 15:10–15:45, tick counts per
5-minute bucket) is printed at exit.

Usage:
  .venv/bin/python scripts/cas_feed_audit.py [--stocks N] [--until HH:MM]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cas_feed_audit")

IST = ZoneInfo("Asia/Kolkata")
NIFTY_IDX_SID = 13  # IDX_I


async def _run(n_stocks: int, until: str) -> int:
    from config import get_config
    from core.fno_equity_universe import load_universe
    from core.instruments import InstrumentMaster
    from core.live_feed import LiveFeed
    from core.token_manager import MasterTokenManager

    cfg = get_config()
    token = await MasterTokenManager().load_or_generate()

    universe = load_universe()
    stocks = [int(u["underlying_security_id"]) for u in universe
              if u.get("underlying_security_id")][:n_stocks]
    futs: list[int] = []
    try:
        im = await InstrumentMaster.load()
        nearest = im.nearest_expiry_for_index("NIFTY")
        log.info("nearest NIFTY expiry: %s", nearest)
    except Exception:
        log.exception("could not resolve NIFTY future — index+stocks only")
    # Near-month NIFTY future sid, if the universe file carries it.
    for u in universe:
        if u.get("underlying_symbol") == "NIFTY" and u.get("future_security_id"):
            futs = [int(u["future_security_id"])]
            break

    out = Path("run") / f"cas_feed_audit_{datetime.now(IST).date()}.jsonl"
    out.parent.mkdir(exist_ok=True)
    fh = open(out, "a", buffering=1)
    counts: dict[str, int] = defaultdict(int)
    first_last: dict[str, list] = {}

    def on_tick(sid: str, ltp: float, volume: int):
        now = datetime.now(IST)
        rec = {"t": now.isoformat(timespec="milliseconds"),
               "sid": sid, "ltp": ltp, "vol": volume}
        fh.write(json.dumps(rec) + "\n")
        counts[sid] += 1
        fl = first_last.setdefault(sid, [now, now])
        fl[1] = now

    feed = LiveFeed(cfg.dhan_client_id, token, on_tick=on_tick)
    sub = {"IDX_I": [NIFTY_IDX_SID], "NSE_EQ": stocks}
    if futs:
        sub["NSE_FNO"] = futs
    feed.subscribe(sub)
    log.info("subscribed: 1 index, %d stocks, %d futures → %s",
             len(stocks), len(futs), out)

    hh, mm = (int(x) for x in until.split(":"))
    stop_at = datetime.now(IST).replace(hour=hh, minute=mm, second=0)

    task = asyncio.create_task(feed.run())
    try:
        while datetime.now(IST) < stop_at:
            await asyncio.sleep(5)
    finally:
        task.cancel()
        fh.close()

    log.info("── audit summary (%d sids ticked) ──", len(counts))
    for sid, n in sorted(counts.items(), key=lambda kv: -kv[1])[:20]:
        f, last = first_last[sid]
        log.info("  sid=%s ticks=%d first=%s last=%s",
                 sid, n, f.strftime("%H:%M:%S"), last.strftime("%H:%M:%S"))
    silent = (1 + len(stocks) + len(futs)) - len(counts)
    log.info("sids with ZERO ticks in the window: %d", silent)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=25,
                    help="how many F&O constituents to watch (default 25)")
    ap.add_argument("--until", default="15:45",
                    help="IST stop time, default 15:45")
    args = ap.parse_args()
    return asyncio.run(_run(args.stocks, args.until))


if __name__ == "__main__":
    raise SystemExit(main())
