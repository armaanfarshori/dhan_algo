"""Daily CAS-surprise capture — N2 blueprint Phase 0/1 (research data, no trading).

For every NSE stock-F&O underlying (the ~190 names that participate in the
Closing Auction Session since 2026-08-03), capture after the close:

  ref_vwap   — 15:00–15:15 IST volume-weighted price from the day's 1-min bars
               (the pre-auction reference window per the CAS design)
  cas_close  — the official close (the auction equilibrium price for F&O names),
               from one batched marketfeed/ohlc call
  surprise   — ln(cas_close / ref_vwap)

One row per (trade_date, security_id) into ``cas_surprise`` (migration 015).
Runs from cron at 16:10 IST weekdays — after the post-close session, before the
EOD summary. Pure data-category REST: no orders, no proxy, runs from any IP.

The value of this script is the *accumulating series*: the auction is days old,
no vendor sells its history, and the N2 cross-sectional test needs ~60 sessions
of it. Missing a day is a permanent hole — failures alert via Telegram.

Usage:
  .venv/bin/python scripts/cas_surprise_capture.py [--date YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cas_surprise")

IST = ZoneInfo("Asia/Kolkata")
REF_START = dtime(15, 0)
REF_END = dtime(15, 15)   # exclusive: 15:00 ≤ bar < 15:15
AUCTION_FROM = dtime(15, 30)

# Between consecutive charts/intraday calls — the endpoint tolerates ~1 req/s.
INTRADAY_SPACING_S = 1.1


def _ref_window(bars: dict, on: date):
    """(vwap, volume, auction_volume) for the reference window of one day.

    ``bars`` is the charts/intraday response data: parallel arrays keyed
    timestamp/open/high/low/close/volume (epoch seconds, exchange time).
    Returns (None, 0, None) when the window has no volume.
    """
    inner = bars.get("data", bars) or {}
    ts = inner.get("timestamp") or []
    close = inner.get("close") or []
    vol = inner.get("volume") or []
    pv = v_sum = 0.0
    auction_v = 0
    saw_auction = False
    for i, t in enumerate(ts):
        dt = datetime.fromtimestamp(int(t), tz=IST)
        if dt.date() != on:
            continue
        bt = dt.time()
        if REF_START <= bt < REF_END:
            v = float(vol[i] or 0)
            pv += float(close[i]) * v
            v_sum += v
        elif bt >= AUCTION_FROM:
            saw_auction = True
            auction_v += int(vol[i] or 0)
    if v_sum <= 0:
        return None, 0, None
    return pv / v_sum, int(v_sum), (auction_v if saw_auction else None)


async def _capture(on: date, dry_run: bool) -> int:
    from config import get_config
    from core.client import DhanClient
    from core.fno_equity_universe import load_universe

    cfg = get_config()
    universe = load_universe()
    if not universe:
        log.error("stock-F&O universe file empty/missing — run "
                  "core.fno_equity_universe refresh first")
        return 2
    names = [(str(u["underlying_security_id"]), u["underlying_symbol"])
             for u in universe if u.get("underlying_security_id")]
    log.info("capturing CAS surprise for %d F&O underlyings, date=%s",
             len(names), on)

    today = datetime.now(IST).date()
    if on != today:
        # marketfeed/ohlc returns the LATEST close only — for an older date the
        # "cas_close" below would silently be the wrong session's price.
        log.warning("--date %s is not the latest session; cas_close comes from "
                    "the live quote and is only correct when %s IS the most "
                    "recent trading day", on, on)
    rows = []
    async with DhanClient(cfg.dhan_client_id, cfg.dhan_access_token,
                          proxy_url=cfg.dhan_proxy_url or None,
                          proxy_categories=cfg.dhan_proxy_categories_set) as client:
        # 1) Official closes for the whole universe in ONE batched quote call.
        ohlc = await client.get_ohlc({"NSE_EQ": [int(sid) for sid, _ in names]})
        closes = {}
        for sid_str, node in ((ohlc.get("data") or {}).get("NSE_EQ") or {}).items():
            c = (node or {}).get("ohlc", {}).get("close") or (node or {}).get("last_price")
            if c:
                closes[str(sid_str)] = float(c)
        log.info("ohlc batch: %d closes", len(closes))

        # 2) Per-name 1-min bars for the reference window (rate-spaced).
        day = on.isoformat()
        for sid, sym in names:
            cas_close = closes.get(sid)
            if not cas_close:
                continue
            try:
                resp = await client.get_intraday_historical(
                    security_id=sid, exchange_segment="NSE_EQ",
                    instrument="EQUITY", interval="1",
                    from_date=day, to_date=day)
            except Exception as exc:
                log.warning("%s(%s): intraday fetch failed: %s", sym, sid, exc)
                await asyncio.sleep(INTRADAY_SPACING_S)
                continue
            vwap, ref_vol, auction_vol = _ref_window(resp or {}, on)
            if vwap:
                rows.append({
                    "trade_date": on, "security_id": sid, "symbol": sym,
                    "ref_vwap": round(vwap, 4), "ref_volume": ref_vol,
                    "cas_close": cas_close, "auction_volume": auction_vol,
                    "surprise": round(math.log(cas_close / vwap), 8),
                })
            await asyncio.sleep(INTRADAY_SPACING_S)

    log.info("computed %d/%d surprises", len(rows), len(names))
    if dry_run:
        for r in rows[:10]:
            log.info("DRY %s", r)
        return 0
    if not rows:
        return 1

    from sqlalchemy import text

    from db import get_engine
    sql = text("""
        INSERT INTO cas_surprise (trade_date, security_id, symbol, ref_vwap,
                                  ref_volume, cas_close, auction_volume, surprise)
        VALUES (:trade_date, :security_id, :symbol, :ref_vwap,
                :ref_volume, :cas_close, :auction_volume, :surprise)
        ON CONFLICT (trade_date, security_id) DO UPDATE SET
            ref_vwap = EXCLUDED.ref_vwap, ref_volume = EXCLUDED.ref_volume,
            cas_close = EXCLUDED.cas_close,
            auction_volume = EXCLUDED.auction_volume,
            surprise = EXCLUDED.surprise, captured_at = now()
    """)
    with get_engine().begin() as conn:
        conn.execute(sql, rows)
    log.info("upserted %d rows into cas_surprise for %s", len(rows), on)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="capture date (IST), default today")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    on = (date.fromisoformat(args.date) if args.date
          else datetime.now(IST).date())
    if on.weekday() >= 5:
        log.info("%s is a weekend — nothing to capture", on)
        return 0
    rc = asyncio.run(_capture(on, args.dry_run))
    if rc != 0 and not args.dry_run:
        try:
            from core.notify import send
            send(f"⚠️ cas_surprise capture FAILED for {on} (rc={rc}) — "
                 "this day's auction data is unrecoverable if not re-run "
                 "before the next session.")
        except Exception:
            log.exception("telegram alert failed")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
