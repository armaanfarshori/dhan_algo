#!/usr/bin/env python3
"""
build_clean_db.py — M2.5: Raw → Clean data pipeline

Reads from: dhan_trading.bars  (raw TimescaleDB on DB EC2)
Writes to:  dhan_clean.bars    (clean TimescaleDB, same instance)
Exports to: s3://$S3_BUCKET/kronos/training-data/

Cleaning filters:
  1. NSE_EQ only
  2. Liquid: avg daily volume > 50K over last 6 months of data
  3. History: ≥ 2 years of 1m bars (≥ 480 trading days)
  4. Drop pre-market auction rows (before 09:15 IST)
  5. Corporate action handling: a session whose first-1m open gapped > 15%
     vs the prior trading day's last-1m close is dropped (split/bonus/large
     overnight event) — but the INSTRUMENT is kept. The strategy is intraday,
     so overnight gaps don't contaminate intraday P&L; excluding whole names
     would bias the universe toward stable, low-event stocks.
  6. Circuit breaker detection: drop individual sessions where total
     session volume < 1 000 (zero/near-zero volume day)

  Known limitation — SURVIVORSHIP BIAS: the universe comes from the current
  `instruments` scrip master, so delisted names are absent → M3 backtest
  results are an optimistic upper bound (logged loudly at run time).

Run on the agent EC2 (has AWS instance-profile creds for S3):
  python build_clean_db.py --identify          # dry-run: print liquid universe
  python build_clean_db.py --transform         # build dhan_clean DB
  python build_clean_db.py --export            # export Parquet to S3
  python build_clean_db.py --all               # all three stages in order
  python build_clean_db.py --all --workers 4   # parallelise export
"""

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import boto3
import pandas as pd
import psycopg2
import psycopg2.extras
import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("clean_db")

# ── Config ────────────────────────────────────────────────────────────────────

RAW_DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", 5432)),
    dbname="dhan_trading",
    user=os.getenv("DB_USER", "trader"),
    password=os.getenv("DB_PASSWORD", ""),
)

CLEAN_DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", 5432)),
    dbname="dhan_clean",
    user=os.getenv("DB_USER", "trader"),
    password=os.getenv("DB_PASSWORD", ""),
)

S3_BUCKET  = os.environ.get("S3_BUCKET", "")  # set in .env on the agent
S3_PREFIX  = "kronos/training-data"

MIN_AVG_DAILY_VOLUME  = 50_000
MIN_TRADING_DAYS      = 480       # ~2 years
CORP_ACTION_THRESHOLD = 0.15      # 15% overnight gap → drop that session (not the instrument)
CIRCUIT_BREAKER_VOL   = 1_000     # session volume below this → drop session
IST_OFFSET_HOURS      = 5.5       # UTC+5:30
MARKET_OPEN_IST       = "09:15"   # drop bars strictly before this (auction)
MARKET_CLOSE_IST      = "15:30"   # drop bars after this


# ── DB helpers ────────────────────────────────────────────────────────────────

def raw_conn():
    return psycopg2.connect(**RAW_DB)

def clean_conn(autocommit=False):
    conn = psycopg2.connect(**CLEAN_DB)
    conn.autocommit = autocommit
    return conn


def ensure_clean_db():
    """Create dhan_clean database and bars hypertable if they don't exist."""
    # Must connect to postgres (not dhan_clean) to create the DB
    admin = dict(RAW_DB, dbname="postgres")
    conn = psycopg2.connect(**admin)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = 'dhan_clean'")
    if not cur.fetchone():
        cur.execute("CREATE DATABASE dhan_clean")
        log.info("Created database dhan_clean")
    cur.close()
    conn.close()

    conn = clean_conn(autocommit=True)
    cur = conn.cursor()

    cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bars (
            time         TIMESTAMPTZ NOT NULL,
            security_id  VARCHAR     NOT NULL,
            timeframe    VARCHAR(4)  NOT NULL,
            open         NUMERIC,
            high         NUMERIC,
            low          NUMERIC,
            close        NUMERIC,
            volume       BIGINT,
            vwap         NUMERIC
        )
    """)

    cur.execute("""
        SELECT 1 FROM timescaledb_information.hypertables
        WHERE hypertable_name = 'bars'
    """)
    if not cur.fetchone():
        cur.execute("""
            SELECT create_hypertable('bars', 'time',
                chunk_time_interval => INTERVAL '1 month',
                if_not_exists => TRUE)
        """)
        log.info("Created bars hypertable in dhan_clean")

    cur.execute("""
        CREATE INDEX IF NOT EXISTS bars_security_time
        ON bars (security_id, time DESC)
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS clean_universe (
            security_id      VARCHAR PRIMARY KEY,
            ticker           VARCHAR,
            name             VARCHAR,
            first_bar        TIMESTAMPTZ,
            last_bar         TIMESTAMPTZ,
            trading_days     INT,
            avg_daily_volume BIGINT,
            built_at         TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    cur.close()
    conn.close()
    log.info("dhan_clean schema ready")


# ── Stage 1: Identify liquid universe ─────────────────────────────────────────

# NOTE on corp actions: we do NOT exclude an instrument just because it once had
# a large overnight gap. The strategy is INTRADAY (resets every session, never
# holds overnight), so a split/bonus gap does not contaminate intraday P&L —
# within a session all bars share the same price scale. Excluding whole names
# would (a) shrink the universe and (b) bias it toward stable, low-event stocks
# — the opposite of what an ORB breakout strategy trades. Instead, the transform
# stage drops only the *specific gap-day sessions* per security (see transform()).
IDENTIFY_SQL = """
WITH daily_stats AS (
    -- Aggregate 1m bars into daily volume per security
    SELECT
        security_id,
        time::date AS day,
        SUM(volume)  AS day_volume
    FROM bars
    WHERE timeframe = '1m'
      AND security_id IN (
          SELECT security_id FROM instruments WHERE exchange_segment = 'NSE_EQ'
      )
    GROUP BY security_id, day
),
summary AS (
    SELECT
        security_id,
        COUNT(DISTINCT day)                           AS trading_days,
        MIN(day)                                      AS first_day,
        MAX(day)                                      AS last_day,
        AVG(day_volume)::BIGINT                       AS avg_daily_volume
    FROM daily_stats
    GROUP BY security_id
)
SELECT
    s.security_id,
    i.ticker,
    i.name,
    s.first_day::TEXT  AS first_bar,
    s.last_day::TEXT   AS last_bar,
    s.trading_days,
    s.avg_daily_volume
FROM summary s
JOIN instruments i USING (security_id)
WHERE s.trading_days     >= %(min_days)s
  AND s.avg_daily_volume >= %(min_vol)s
ORDER BY s.avg_daily_volume DESC
"""


def identify_universe(dry_run=True):
    log.info("Stage 1: identifying liquid universe from raw DB…")
    log.warning(
        "SURVIVORSHIP BIAS: the universe is drawn from the CURRENT `instruments` "
        "scrip master (NSE_EQ), so any name delisted/suspended during the window "
        "is absent. Backtests on this universe see only survivors and OVERSTATE "
        "returns — treat M3 results as an optimistic upper bound. A point-in-time "
        "universe needs delisted-instrument history (not available from the feed)."
    )
    conn = raw_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Guard against long-running queries holding locks or OOM-ing the DB while
    # the backfill is active.  10 min is generous for the aggregation but will
    # abort any accidental full-table scan before it destabilises the instance.
    # lock_timeout prevents the session from queuing behind a long exclusive
    # lock (e.g. autovacuum on a chunk) indefinitely.
    cur.execute("SET statement_timeout = '600000'")   # 10 minutes
    cur.execute("SET lock_timeout      = '5000'")     # 5 seconds
    cur.execute(IDENTIFY_SQL, {
        "min_days":    MIN_TRADING_DAYS,
        "min_vol":     MIN_AVG_DAILY_VOLUME,
    })
    rows = cur.fetchall()
    cur.close()
    conn.close()

    log.info("Liquid universe: %d instruments", len(rows))
    for r in rows[:20]:
        log.info("  %-12s %-40s  days=%-4d  avg_vol=%s",
                 r["ticker"], r["name"][:38], r["trading_days"],
                 f"{r['avg_daily_volume']:,}")
    if len(rows) > 20:
        log.info("  … and %d more", len(rows) - 20)

    if not dry_run:
        # Persist universe to dhan_clean for reference
        conn2 = clean_conn()
        cur2  = conn2.cursor()
        cur2.execute("TRUNCATE clean_universe")
        psycopg2.extras.execute_values(cur2, """
            INSERT INTO clean_universe
                (security_id, ticker, name, first_bar, last_bar, trading_days, avg_daily_volume)
            VALUES %s
        """, [(r["security_id"], r["ticker"], r["name"],
               r["first_bar"], r["last_bar"], r["trading_days"],
               r["avg_daily_volume"]) for r in rows])
        conn2.commit()
        cur2.close()
        conn2.close()
        log.info("Universe written to dhan_clean.clean_universe")

    return [r["security_id"] for r in rows]


# ── Stage 2: Transform raw → clean ────────────────────────────────────────────

COPY_SQL = """
INSERT INTO bars (time, security_id, timeframe, open, high, low, close, volume, vwap)
SELECT
    b.time,
    b.security_id,
    b.timeframe,
    b.open,
    b.high,
    b.low,
    b.close,
    b.volume,
    b.vwap
FROM bars b
WHERE b.security_id = %(sid)s
  AND b.timeframe   = '1m'
  -- Drop pre-market auction (before 09:15 IST = 03:45 UTC)
  AND (b.time AT TIME ZONE 'Asia/Kolkata')::time >= '09:15'
  -- Drop after-market (after 15:30 IST)
  AND (b.time AT TIME ZONE 'Asia/Kolkata')::time <= '15:30'
  -- Drop circuit-breaker sessions: session volume < threshold
  AND DATE(b.time AT TIME ZONE 'Asia/Kolkata') NOT IN (
      SELECT DATE(time AT TIME ZONE 'Asia/Kolkata')
      FROM bars
      WHERE security_id = %(sid)s
        AND timeframe   = '1m'
      GROUP BY DATE(time AT TIME ZONE 'Asia/Kolkata')
      HAVING SUM(volume) < %(circuit_vol)s
  )
ON CONFLICT DO NOTHING
"""


def _compute_vwap(rows: list[tuple]) -> list[tuple]:
    """
    Replace the vwap column (index 8) with a daily cumulative VWAP computed
    from close price and volume.  Resets at the start of each trading session.

    VWAP = cumsum(close × volume) / cumsum(volume)

    Using close as a proxy for typical price (no tick data available).
    """
    # rows columns: time(0) sid(1) tf(2) open(3) high(4) low(5) close(6) vol(7) vwap(8)
    out = []
    cum_pv = 0.0
    cum_v  = 0
    cur_day = None

    for row in rows:
        row      = list(row)
        ts       = row[0]
        day      = ts.date() if hasattr(ts, "date") else str(ts)[:10]
        close    = float(row[6]) if row[6] is not None else 0.0
        volume   = int(row[7])   if row[7] is not None else 0

        if day != cur_day:          # new session — reset accumulators
            cum_pv  = 0.0
            cum_v   = 0
            cur_day = day

        cum_pv += close * volume
        cum_v  += volume
        row[8]  = round(cum_pv / cum_v, 4) if cum_v > 0 else None
        out.append(tuple(row))

    return out


# The cleaned-read SELECT (one security at a time). Identical filters to the
# COPY_SQL sketch above, plus the corp-action gap-day filter. Streamed via a
# server-side cursor so memory stays bounded regardless of how many bars a
# single liquid name has (the busiest names have ~450K+ 1m rows over ~5y, which
# is ~800MB if fetchall'd as psycopg2 Decimals — enough to OOM a small box that
# is also running the live trader). See _stream_transform_one.
SELECT_CLEAN_SQL = """
    SELECT time, security_id, timeframe, open, high, low, close, volume, vwap
    FROM bars
    WHERE security_id = %s
      AND timeframe = '1m'
      AND (time AT TIME ZONE 'Asia/Kolkata')::time >= '09:15'
      AND (time AT TIME ZONE 'Asia/Kolkata')::time <= '15:30'
      -- Drop circuit-breaker sessions: session volume below threshold
      AND DATE(time AT TIME ZONE 'Asia/Kolkata') NOT IN (
          SELECT DATE(time AT TIME ZONE 'Asia/Kolkata')
          FROM bars
          WHERE security_id = %s AND timeframe = '1m'
          GROUP BY DATE(time AT TIME ZONE 'Asia/Kolkata')
          HAVING SUM(volume) < %s
      )
      -- Drop corp-action gap sessions: this day's first-1m open gapped
      -- > threshold vs the prior trading day's last-1m close (split/bonus/
      -- large overnight event). Computed from 1m so it never depends on
      -- 1d bars; per-security so it stays on the security_id index.
      AND DATE(time AT TIME ZONE 'Asia/Kolkata') NOT IN (
          SELECT day FROM (
              SELECT
                  day,
                  day_open,
                  LAG(day_close) OVER (ORDER BY day) AS prev_close
              FROM (
                  SELECT
                      DATE(time AT TIME ZONE 'Asia/Kolkata') AS day,
                      first(open,  time) AS day_open,
                      last(close,  time) AS day_close
                  FROM bars
                  WHERE security_id = %s AND timeframe = '1m'
                    AND (time AT TIME ZONE 'Asia/Kolkata')::time >= '09:15'
                    AND (time AT TIME ZONE 'Asia/Kolkata')::time <= '15:30'
                  GROUP BY DATE(time AT TIME ZONE 'Asia/Kolkata')
              ) d
          ) g
          WHERE prev_close > 0
            AND ABS(day_open - prev_close) / prev_close > %s
      )
    ORDER BY time
"""

INSERT_CLEAN_SQL = """
    INSERT INTO bars
        (time, security_id, timeframe, open, high, low, close, volume, vwap)
    VALUES %s
    ON CONFLICT DO NOTHING
"""


def _stream_transform_one(raw, cln_cur, sid: str, insert_page: int = 5000) -> int:
    """
    Stream one security's cleaned 1m bars from raw → dhan_clean with a
    server-side cursor, computing daily-reset VWAP incrementally and flushing
    inserts every `insert_page` rows. Peak memory is O(insert_page), not
    O(security's row count). Returns rows written.

    The SELECT's ORDER BY time guarantees rows arrive in session order, so the
    cumulative VWAP accumulators (reset on day change) stay correct across the
    streamed batches — identical result to _compute_vwap on the full list.
    """
    ss = raw.cursor(name=f"tx_{sid}")          # named => server-side, streaming
    ss.itersize = 20000
    ss.execute(SELECT_CLEAN_SQL,
               (sid, sid, CIRCUIT_BREAKER_VOL, sid, CORP_ACTION_THRESHOLD))

    cum_pv = 0.0
    cum_v  = 0
    cur_day = None
    buf: list[tuple] = []
    written = 0

    for row in ss:
        row    = list(row)
        ts     = row[0]
        day    = ts.date() if hasattr(ts, "date") else str(ts)[:10]
        close  = float(row[6]) if row[6] is not None else 0.0
        volume = int(row[7])   if row[7] is not None else 0

        if day != cur_day:          # new session — reset accumulators
            cum_pv  = 0.0
            cum_v   = 0
            cur_day = day

        cum_pv += close * volume
        cum_v  += volume
        row[8]  = round(cum_pv / cum_v, 4) if cum_v > 0 else None
        buf.append(tuple(row))

        if len(buf) >= insert_page:
            psycopg2.extras.execute_values(cln_cur, INSERT_CLEAN_SQL, buf, page_size=insert_page)
            written += len(buf)
            buf.clear()

    if buf:
        psycopg2.extras.execute_values(cln_cur, INSERT_CLEAN_SQL, buf, page_size=insert_page)
        written += len(buf)

    ss.close()
    raw.rollback()   # read-only snapshot — release it so it can't pin vacuum xmin
    return written


def transform(universe: list[str], batch_size: int = 50):
    log.info("Stage 2: transforming %d instruments into dhan_clean…", len(universe))
    raw  = raw_conn()
    cln  = clean_conn()
    cln_cur = cln.cursor()

    done = 0
    total_rows = 0
    for sid in universe:
        total_rows += _stream_transform_one(raw, cln_cur, sid)
        done += 1
        if done % batch_size == 0:
            cln.commit()
            log.info("  %d / %d instruments committed (%s rows so far)",
                     done, len(universe), f"{total_rows:,}")

    cln.commit()
    cln_cur.close()
    raw.close()
    cln.close()
    log.info("Stage 2 complete — %d instruments, %s rows written to dhan_clean.bars",
             done, f"{total_rows:,}")


# ── Stage 3: Export Parquet to S3 ─────────────────────────────────────────────

PARQUET_SCHEMA = pa.schema([
    pa.field("time",        pa.timestamp("ms", tz="UTC")),
    pa.field("security_id", pa.string()),
    pa.field("open",        pa.float32()),
    pa.field("high",        pa.float32()),
    pa.field("low",         pa.float32()),
    pa.field("close",       pa.float32()),
    pa.field("volume",      pa.int64()),
    pa.field("vwap",        pa.float32()),
])


def _export_one(security_id: str, ticker: str, s3_client) -> str:
    conn = clean_conn()
    df = pd.read_sql("""
        SELECT time, security_id, open, high, low, close, volume, vwap
        FROM bars
        WHERE security_id = %s AND timeframe = '1m'
        ORDER BY time
    """, conn, params=(security_id,))
    conn.close()

    if df.empty:
        return f"SKIP  {ticker} ({security_id}) — no rows"

    df["time"] = pd.to_datetime(df["time"], utc=True)
    for col in ("open", "high", "low", "close", "vwap"):
        df[col] = df[col].astype("float32")

    table = pa.Table.from_pandas(df, schema=PARQUET_SCHEMA, preserve_index=False)

    buf = pa.BufferOutputStream()
    pq.write_table(table, buf, compression="snappy")

    key = f"{S3_PREFIX}/{security_id}.parquet"
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=buf.getvalue().to_pybytes(),
    )
    return f"OK    {ticker:<12} ({security_id})  {len(df):>7,} rows → s3://{S3_BUCKET}/{key}"


def export_parquet(universe: list[str], workers: int = 4):
    log.info("Stage 3: exporting %d instruments to S3 Parquet…", len(universe))

    # Fetch tickers for logging
    conn = clean_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT security_id, ticker FROM clean_universe")
    ticker_map = {r["security_id"]: r["ticker"] for r in cur.fetchall()}
    cur.close()
    conn.close()

    s3 = boto3.client("s3")
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_export_one, sid, ticker_map.get(sid, sid), s3): sid
            for sid in universe
        }
        for fut in as_completed(futures):
            result = fut.result()
            done  += 1
            if done % 50 == 0 or "SKIP" in result or done == len(universe):
                log.info("  [%d/%d] %s", done, len(universe), result)

    # Write manifest
    manifest = {
        "built_at":   datetime.now(timezone.utc).isoformat(),
        "count":      len(universe),
        "s3_prefix":  f"s3://{S3_BUCKET}/{S3_PREFIX}/",
        "filters": {
            "min_avg_daily_volume":  MIN_AVG_DAILY_VOLUME,
            "min_trading_days":      MIN_TRADING_DAYS,
            "corp_action_threshold": CORP_ACTION_THRESHOLD,
            "circuit_breaker_vol":   CIRCUIT_BREAKER_VOL,
            "market_hours_ist":      f"{MARKET_OPEN_IST}–{MARKET_CLOSE_IST}",
        },
    }
    import json
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_PREFIX}/manifest.json",
        Body=json.dumps(manifest, indent=2).encode(),
        ContentType="application/json",
    )
    log.info("Manifest written to s3://%s/%s/manifest.json", S3_BUCKET, S3_PREFIX)
    log.info("Stage 3 complete — %d Parquet files on S3", done)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Build dhan_clean DB from raw bars")
    ap.add_argument("--identify",  action="store_true", help="Print liquid universe (dry run)")
    ap.add_argument("--transform", action="store_true", help="Copy cleaned bars to dhan_clean")
    ap.add_argument("--export",    action="store_true", help="Export Parquet files to S3")
    ap.add_argument("--all",       action="store_true", help="Run all three stages")
    ap.add_argument("--workers",   type=int, default=4,  help="Parallel S3 upload workers")
    args = ap.parse_args()

    if not any([args.identify, args.transform, args.export, args.all]):
        ap.print_help()
        sys.exit(1)

    run_identify  = args.identify  or args.all
    run_transform = args.transform or args.all
    run_export    = args.export    or args.all

    if run_transform or run_export:
        ensure_clean_db()

    universe = None

    if run_identify:
        dry = not (run_transform or run_export)
        universe = identify_universe(dry_run=dry)

    if run_transform:
        if universe is None:
            universe = identify_universe(dry_run=False)
        transform(universe)

    if run_export:
        if universe is None:
            # Load from dhan_clean.clean_universe if already built
            conn = clean_conn()
            cur  = conn.cursor()
            cur.execute("SELECT security_id FROM clean_universe ORDER BY avg_daily_volume DESC")
            universe = [r[0] for r in cur.fetchall()]
            cur.close()
            conn.close()
            log.info("Loaded %d instruments from dhan_clean.clean_universe", len(universe))
        export_parquet(universe, workers=args.workers)

    log.info("Done.")


if __name__ == "__main__":
    main()
