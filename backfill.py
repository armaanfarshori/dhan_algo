"""
Historical OHLCV Backfill
=========================
Pulls up to 365 days of 1-min intraday data from Dhan /v2/charts/intraday
(max 90 days per call) and upserts into both:
  - bars (timeframe='1m')  — primary, authoritative price history
  - ohlcv_1min             — legacy; kept for backwards compatibility

Usage:
    python backfill.py                        # last 90 days, default watchlist
    python backfill.py --from 2025-01-01 --to 2026-06-03 --ids 2885,1333
    python backfill.py --dry-run              # fetch only, no DB write
"""

import argparse
import asyncio
import logging
import sys
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

_MAX_CHUNK_DAYS = 90


def _parse_response(data: dict[str, Any], security_id: str, exchange_segment: str) -> pd.DataFrame:
    """Convert the Dhan intraday API response dict to a DataFrame."""
    inner = data.get("data", data)  # some responses wrap under "data"
    timestamps = inner.get("timestamp", [])
    if not timestamps:
        return pd.DataFrame()

    df = pd.DataFrame({
        "security_id":     security_id,
        "exchange_segment":exchange_segment,
        "ts":  pd.to_datetime(timestamps, unit="s", utc=True),
        "open":  inner["open"],
        "high":  inner["high"],
        "low":   inner["low"],
        "close": inner["close"],
        "volume":inner["volume"],
    })
    return df


def _upsert_candles(df: pd.DataFrame) -> int:
    """Upsert candles into bars (primary) and ohlcv_1min (legacy). Returns row count."""
    if df.empty:
        return 0

    rows_bars = [
        {
            "time":        r["ts"],
            "security_id": r["security_id"],
            "timeframe":   "1m",
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

    legacy_rows = df.rename(columns={"ts": "ts"}).to_dict(orient="records")
    legacy_sql = text("""
        INSERT INTO ohlcv_1min (security_id, exchange_segment, ts, open, high, low, close, volume)
        VALUES (:security_id, :exchange_segment, :ts, :open, :high, :low, :close, :volume)
        ON CONFLICT (security_id, ts) DO UPDATE SET
            open   = EXCLUDED.open,
            high   = EXCLUDED.high,
            low    = EXCLUDED.low,
            close  = EXCLUDED.close,
            volume = EXCLUDED.volume
    """)

    with get_session() as session:
        session.execute(bars_sql, rows_bars)
        session.execute(legacy_sql, legacy_rows)

    return len(rows_bars)


async def backfill_security(
    client: DhanClient,
    security_id: str,
    exchange_segment: str,
    from_date: date,
    to_date: date,
    dry_run: bool = False,
) -> int:
    """Backfill one security in 90-day chunks. Returns total candles stored."""
    total = 0
    cursor = from_date

    while cursor <= to_date:
        chunk_end = min(cursor + timedelta(days=_MAX_CHUNK_DAYS - 1), to_date)
        logger.info(
            "  %s  %s → %s",
            security_id,
            cursor.strftime("%Y-%m-%d"),
            chunk_end.strftime("%Y-%m-%d"),
        )

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
            logger.error("  Failed chunk %s→%s: %s", cursor, chunk_end, exc)
            cursor = chunk_end + timedelta(days=1)
            continue

        df = _parse_response(data, security_id, exchange_segment)
        logger.info("  Received %d candles", len(df))

        if not dry_run and not df.empty:
            n = _upsert_candles(df)
            total += n
        elif not df.empty:
            total += len(df)

        cursor = chunk_end + timedelta(days=1)
        await asyncio.sleep(0.3)  # respect Dhan data rate limit

    return total


async def run_backfill(
    security_ids: list[str],
    exchange_segment: str,
    from_date: date,
    to_date: date,
    dry_run: bool,
    cfg,
):
    async with DhanClient(cfg.dhan_client_id, cfg.dhan_access_token) as client:
        for sid in security_ids:
            logger.info("Backfilling security_id=%s", sid)
            n = await backfill_security(
                client, sid, exchange_segment, from_date, to_date, dry_run
            )
            logger.info("  Done — %d candles %s", n, "(dry run)" if dry_run else "upserted")


def main():
    parser = argparse.ArgumentParser(description="Backfill OHLCV data into TimescaleDB")
    parser.add_argument(
        "--from", dest="from_date",
        default=(date.today() - timedelta(days=90)).strftime("%Y-%m-%d"),
        help="Start date YYYY-MM-DD (default: 90 days ago)",
    )
    parser.add_argument(
        "--to", dest="to_date",
        default=date.today().strftime("%Y-%m-%d"),
        help="End date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--ids",
        help="Comma-separated security IDs (default: from .env WATCHLIST_SECURITY_IDS)",
    )
    parser.add_argument(
        "--segment",
        default=None,
        help="Exchange segment (default: from .env WATCHLIST_EXCHANGE_SEGMENT)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch only, skip DB writes")
    args = parser.parse_args()

    cfg = get_config()
    from_date = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    to_date = datetime.strptime(args.to_date, "%Y-%m-%d").date()
    security_ids = [s.strip() for s in args.ids.split(",")] if args.ids else cfg.watchlist_security_ids
    exchange_segment = args.segment or cfg.watchlist_exchange_segment

    if not args.dry_run:
        logger.info("Connecting to TimescaleDB at %s:%s/%s", cfg.db_host, cfg.db_port, cfg.db_name)
        init_db(cfg.db_url)

    logger.info(
        "Backfill  %s → %s  |  %d securities  |  segment=%s  |  dry_run=%s",
        from_date, to_date, len(security_ids), exchange_segment, args.dry_run,
    )

    asyncio.run(run_backfill(security_ids, exchange_segment, from_date, to_date, args.dry_run, cfg))
    logger.info("Backfill complete.")


if __name__ == "__main__":
    main()
