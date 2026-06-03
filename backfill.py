"""
Historical OHLCV Backfill
=========================
Pulls historical bars from the Dhan API and upserts into TimescaleDB.

Endpoints used:
  - /v2/charts/intraday  → 1-min bars (max 90 days per call; tested to 2+ years)
  - /v2/charts/historical → daily bars (tested to 5+ years)

All data lands in:
  - bars  (timeframe='1m' or '1d')  — primary
  - ohlcv_1min                       — legacy, kept for backwards compat (1m only)

Usage:
    python backfill.py                                # 90d intraday, default watchlist
    python backfill.py --from 2024-01-01              # extended intraday
    python backfill.py --daily                        # 5-year daily bars
    python backfill.py --all                          # 2y intraday + 5y daily
    python backfill.py --ids 2885,1333 --dry-run      # fetch only, no DB write
"""

import argparse
import asyncio
import logging
from datetime import date, timedelta, datetime
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

from config import get_config
from db import init_db, get_session
from core.client import DhanClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dhan.backfill")

_MAX_INTRADAY_CHUNK = 90  # Dhan hard limit per /v2/charts/intraday call


# ── Parsing ────────────────────────────────────────────────────────────────────

def _parse_intraday(data: dict[str, Any], security_id: str, exchange_segment: str) -> pd.DataFrame:
    inner = data.get("data", data)
    timestamps = inner.get("timestamp", [])
    if not timestamps:
        return pd.DataFrame()
    return pd.DataFrame({
        "security_id":      security_id,
        "exchange_segment": exchange_segment,
        "ts":     pd.to_datetime(timestamps, unit="s", utc=True),
        "open":   inner["open"],
        "high":   inner["high"],
        "low":    inner["low"],
        "close":  inner["close"],
        "volume": inner["volume"],
    })


def _parse_daily(data: dict[str, Any], security_id: str, exchange_segment: str) -> pd.DataFrame:
    inner = data.get("data", data)
    timestamps = inner.get("timestamp", [])
    if not timestamps:
        return pd.DataFrame()
    return pd.DataFrame({
        "security_id":      security_id,
        "exchange_segment": exchange_segment,
        "ts":     pd.to_datetime(timestamps, unit="s", utc=True),
        "open":   inner["open"],
        "high":   inner["high"],
        "low":    inner["low"],
        "close":  inner["close"],
        "volume": inner["volume"],
    })


# ── DB writes ──────────────────────────────────────────────────────────────────

def _upsert_bars(df: pd.DataFrame, timeframe: str) -> int:
    """Upsert into bars table (and ohlcv_1min for 1m data)."""
    if df.empty:
        return 0

    rows = [
        {
            "time":        r["ts"],
            "security_id": r["security_id"],
            "timeframe":   timeframe,
            "open":   r["open"],
            "high":   r["high"],
            "low":    r["low"],
            "close":  r["close"],
            "volume": r["volume"],
        }
        for r in df.to_dict(orient="records")
    ]

    bars_sql = text("""
        INSERT INTO bars (time, security_id, timeframe, open, high, low, close, volume)
        VALUES (:time, :security_id, :timeframe, :open, :high, :low, :close, :volume)
        ON CONFLICT (security_id, timeframe, time) DO UPDATE SET
            open   = EXCLUDED.open,
            high   = EXCLUDED.high,
            low    = EXCLUDED.low,
            close  = EXCLUDED.close,
            volume = EXCLUDED.volume
    """)

    with get_session() as session:
        session.execute(bars_sql, rows)

        # also mirror into ohlcv_1min for 1-minute bars (legacy)
        if timeframe == "1m":
            legacy_rows = df.to_dict(orient="records")
            session.execute(text("""
                INSERT INTO ohlcv_1min
                    (security_id, exchange_segment, ts, open, high, low, close, volume)
                VALUES
                    (:security_id, :exchange_segment, :ts, :open, :high, :low, :close, :volume)
                ON CONFLICT (security_id, ts) DO UPDATE SET
                    open   = EXCLUDED.open,
                    high   = EXCLUDED.high,
                    low    = EXCLUDED.low,
                    close  = EXCLUDED.close,
                    volume = EXCLUDED.volume
            """), legacy_rows)

    return len(rows)


# ── Intraday backfill (1m) ─────────────────────────────────────────────────────

async def backfill_intraday(
    client: DhanClient,
    security_id: str,
    exchange_segment: str,
    from_date: date,
    to_date: date,
    dry_run: bool = False,
) -> int:
    total = 0
    cursor = from_date
    while cursor <= to_date:
        chunk_end = min(cursor + timedelta(days=_MAX_INTRADAY_CHUNK - 1), to_date)
        logger.info("  [1m] %s  %s → %s", security_id,
                    cursor.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d"))
        try:
            data = await client.get_intraday_historical(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument="EQUITY",
                interval="1",
                from_date=cursor.strftime("%Y-%m-%d"),
                to_date=chunk_end.strftime("%Y-%m-%d"),
            )
        except Exception as exc:
            logger.error("  Chunk failed: %s", exc)
            cursor = chunk_end + timedelta(days=1)
            continue

        df = _parse_intraday(data, security_id, exchange_segment)
        logger.info("  Received %d candles", len(df))
        if not dry_run and not df.empty:
            total += _upsert_bars(df, "1m")
        else:
            total += len(df)

        cursor = chunk_end + timedelta(days=1)
        await asyncio.sleep(0.25)

    return total


# ── Daily backfill (1d) ────────────────────────────────────────────────────────

async def backfill_daily(
    client: DhanClient,
    security_id: str,
    exchange_segment: str,
    from_date: date,
    to_date: date,
    dry_run: bool = False,
) -> int:
    logger.info("  [1d] %s  %s → %s", security_id,
                from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d"))
    try:
        data = await client.get_daily_historical(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument="EQUITY",
            from_date=from_date.strftime("%Y-%m-%d"),
            to_date=to_date.strftime("%Y-%m-%d"),
        )
    except Exception as exc:
        logger.error("  Daily fetch failed: %s", exc)
        return 0

    df = _parse_daily(data, security_id, exchange_segment)
    logger.info("  Received %d daily bars", len(df))
    if dry_run or df.empty:
        return len(df)
    return _upsert_bars(df, "1d")


# ── Orchestrator ───────────────────────────────────────────────────────────────

async def run_backfill(args, cfg):
    async with DhanClient(cfg.dhan_client_id, cfg.dhan_access_token) as client:
        for sid in args.security_ids:
            logger.info("═══ security_id=%s ═══", sid)

            if args.do_intraday:
                n = await backfill_intraday(
                    client, sid, args.exchange_segment,
                    args.from_date, args.to_date, args.dry_run,
                )
                logger.info("  1m done — %d rows %s", n, "(dry)" if args.dry_run else "upserted")

            if args.do_daily:
                n = await backfill_daily(
                    client, sid, args.exchange_segment,
                    args.daily_from, args.to_date, args.dry_run,
                )
                logger.info("  1d done — %d rows %s", n, "(dry)" if args.dry_run else "upserted")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backfill OHLCV into TimescaleDB")
    parser.add_argument("--from", dest="from_date",
        default=(date.today() - timedelta(days=90)).strftime("%Y-%m-%d"),
        help="Intraday start date (default: 90 days ago)")
    parser.add_argument("--to", dest="to_date",
        default=date.today().strftime("%Y-%m-%d"),
        help="End date (default: today)")
    parser.add_argument("--daily-from", dest="daily_from",
        default=(date.today() - timedelta(days=365*5)).strftime("%Y-%m-%d"),
        help="Daily bars start date (default: 5 years ago)")
    parser.add_argument("--ids",
        help="Comma-separated security IDs (default: WATCHLIST_SECURITY_IDS)")
    parser.add_argument("--segment", default=None)
    parser.add_argument("--daily",  action="store_true", help="Pull daily bars only")
    parser.add_argument("--all",    action="store_true", help="Pull intraday + daily")
    parser.add_argument("--dry-run",action="store_true")
    raw = parser.parse_args()

    cfg = get_config()

    raw.from_date    = datetime.strptime(raw.from_date,   "%Y-%m-%d").date()
    raw.to_date      = datetime.strptime(raw.to_date,     "%Y-%m-%d").date()
    raw.daily_from   = datetime.strptime(raw.daily_from,  "%Y-%m-%d").date()
    raw.security_ids = (
        [s.strip() for s in raw.ids.split(",")]
        if raw.ids else cfg.watchlist_security_ids
    )
    raw.exchange_segment = raw.segment or cfg.watchlist_exchange_segment
    raw.do_intraday = not raw.daily          # intraday unless --daily-only
    raw.do_daily    = raw.daily or raw.all

    if not raw.dry_run:
        init_db(cfg.db_url)

    logger.info(
        "Backfill  intraday=%s (%s→%s)  daily=%s (%s→%s)  ids=%s",
        raw.do_intraday, raw.from_date, raw.to_date,
        raw.do_daily,    raw.daily_from, raw.to_date,
        raw.security_ids,
    )

    asyncio.run(run_backfill(raw, cfg))
    logger.info("Backfill complete.")


if __name__ == "__main__":
    main()
