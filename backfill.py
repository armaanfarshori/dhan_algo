"""
Historical OHLCV Backfill + Instrument Master Sync
===================================================
Endpoints used:
  - /v2/charts/intraday  → 1-min bars (max 90 days per call; tested to 2+ years)
  - /v2/charts/historical → daily bars (tested to 5+ years)

All OHLCV data lands in:
  - bars  (timeframe='1m' or '1d')  — primary
  - ohlcv_1min                       — legacy mirror (1m only)

Usage:
    python backfill.py --instruments                   # load all 224K scrips → instruments table
    python backfill.py --instruments --force-download  # re-download CSV then load

    python backfill.py                                 # 90d intraday, .env watchlist
    python backfill.py --from 2021-06-01 --all         # 5y intraday + 5y daily
    python backfill.py --nse-eq --all --from 2021-06-01  # all 22K NSE equities
    python backfill.py --ids 2885,1333 --dry-run       # specific IDs, no DB write
"""

import argparse
import asyncio
import logging
import re
from datetime import date, timedelta, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

from config import get_config
from db import init_db, get_session
from core.client import DhanClient

_ENV_FILE = Path(__file__).parent / ".env"

import time as _time

class _ISTFormatter(logging.Formatter):
    """Log timestamps in IST (UTC+5:30) for readability."""
    def formatTime(self, record, datefmt=None):
        import datetime
        ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        ct  = datetime.datetime.fromtimestamp(record.created, tz=ist)
        return ct.strftime(datefmt or "%H:%M:%S IST")

_handler = logging.StreamHandler()
_handler.setFormatter(_ISTFormatter(
    fmt="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s"
))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
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

# Instrument type by exchange segment — used in Dhan API intraday calls
_SEGMENT_INSTRUMENT = {
    "NSE_EQ":   "EQUITY",
    "BSE_EQ":   "EQUITY",
    "NSE_FNO":  "FUTIDX",   # default; FUTSTK / OPTIDX / OPTSTK also exist
    "BSE_FNO":  "FUTIDX",
    "NSE_CDS":  "FUTCUR",
    "BSE_CDS":  "FUTCUR",
    "MCX_COMM": "FUTCOM",
    "NSE_IDX":  "INDEX",
    "BSE_IDX":  "INDEX",
}


# ── Live token rotation (multi-day runs) ───────────────────────────────────────
# backfill never generates tokens (main.py owns that). But a multi-day run
# outlives the ~24h token, so main.py rotates dhan_token.json mid-run. The
# running DhanClient holds the token string captured at startup, so we must
# re-read the shared cache and push any rotation into the live client/session.

class _TokenExpired(Exception):
    """Raised when a chunk fails auth and no fresh token is available yet."""


def _is_auth_error(exc: Exception) -> bool:
    s = str(exc)
    return "DH-901" in s or "invalid or expired" in s.lower()


def _reload_token(client: DhanClient) -> bool:
    """Re-read dhan_token.json; if it rotated, push it into the live client.

    Returns True if a *different* valid token was loaded, False otherwise.
    """
    from core.token_manager import read_current_token
    fresh = read_current_token()
    if fresh and fresh != client.access_token:
        client._on_token_refreshed(fresh)   # updates self.access_token + session header
        logger.info("  Token rotation detected — reloaded fresh token into client")
        return True
    return False


async def _wait_for_fresh_token(client: DhanClient, poll_s: int = 30, max_wait_s: int = 1800):
    """Block until main.py rotates dhan_token.json to a token != the current one.

    Backfill cannot mint tokens itself, so on an unrecoverable auth failure we
    wait for main.py's MasterTokenManager to publish a fresh one, then resume
    the SAME security (no checkpoint advance → no data loss)."""
    waited = 0
    while waited < max_wait_s:
        if _reload_token(client):
            return True
        await asyncio.sleep(poll_s)
        waited += poll_s
        logger.warning("  Waiting for main.py to rotate token… (%ds)", waited)
    return False


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
            instrument_type = _SEGMENT_INSTRUMENT.get(exchange_segment, "EQUITY")
            data = await client.get_intraday_historical(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument=instrument_type,
                interval="1",
                from_date=cursor.strftime("%Y-%m-%d"),
                to_date=chunk_end.strftime("%Y-%m-%d"),
            )
        except Exception as exc:
            if _is_auth_error(exc):
                # Token expired mid-run. If main.py already rotated it, reload
                # and retry the SAME chunk; otherwise bubble up so the caller
                # waits for a fresh token (and does NOT checkpoint this id).
                if _reload_token(client):
                    continue
                raise _TokenExpired(str(exc))
            logger.error("  Chunk failed: %s", exc)
            cursor = chunk_end + timedelta(days=1)
            continue

        df = _parse_intraday(data, security_id, exchange_segment)
        # Only log if we actually received data — 0 candles is normal for illiquid/suspended instruments
        if len(df) > 0:
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
            instrument=_SEGMENT_INSTRUMENT.get(exchange_segment, "EQUITY"),
            from_date=from_date.strftime("%Y-%m-%d"),
            to_date=to_date.strftime("%Y-%m-%d"),
        )
    except Exception as exc:
        if _is_auth_error(exc):
            if _reload_token(client):
                return await backfill_daily(
                    client, security_id, exchange_segment, from_date, to_date, dry_run,
                )
            raise _TokenExpired(str(exc))
        logger.error("  Daily fetch failed: %s", exc)
        return 0

    df = _parse_daily(data, security_id, exchange_segment)
    logger.info("  Received %d daily bars", len(df))
    if dry_run or df.empty:
        return len(df)
    return _upsert_bars(df, "1d")


# ── Token refresh callback ────────────────────────────────────────────────────

def _save_token_to_env(new_token: str):
    """Persist refreshed access token to .env so restarts use the latest."""
    if not _ENV_FILE.exists():
        return
    content = _ENV_FILE.read_text()
    updated = re.sub(
        r"^DHAN_ACCESS_TOKEN=.*$",
        f"DHAN_ACCESS_TOKEN={new_token}",
        content,
        flags=re.MULTILINE,
    )
    _ENV_FILE.write_text(updated)
    logger.info("Token refreshed and written to .env")


# ── Orchestrator ───────────────────────────────────────────────────────────────

async def run_backfill(args, cfg):
    """
    Runs the backfill with automatic token refresh.

    If DHAN_PIN and DHAN_TOTP_SECRET are set in .env, DhanAuthManager is
    initialised and runs a background loop that renews the token 30 minutes
    before expiry — critical for multi-day runs.  The fresh token is also
    written back to .env so manual restarts pick it up automatically.

    Falls back to static token if PIN/TOTP are not configured.
    """
    # ── Token: read from shared cache managed by main.py (MasterTokenManager) ──
    # backfill.py never calls generate_token() — only main.py does.
    # This prevents the "two processes competing and invalidating each other" problem.
    from core.token_manager import read_current_token
    cached = read_current_token()
    access_token = cached or cfg.dhan_access_token
    if cached:
        logger.info("Using cached token from dhan_token.json (managed by main.py)")
    else:
        logger.warning("No cached token found — using .env token. Start main.py first for auto-refresh.")

    auth_mgr     = None   # backfill never owns token refresh
    refresh_task = None

    async with DhanClient(cfg.dhan_client_id, access_token, auth_manager=auth_mgr) as client:
        if auth_mgr:
            # Background loop checks every 10 min and refreshes 30 min before expiry
            refresh_task = asyncio.create_task(auth_mgr.run())

        # ── Bulletproof resume: index checkpoint + DB seeding ─────────────────
        # The security_ids list is fixed/sorted. We track the INDEX of the last
        # processed security in a checkpoint file (survives process restarts).
        # On a fresh start with no checkpoint, we SEED the start index from the
        # DB: find the furthest security in the list that already has data.
        # This means a fresh deploy NEVER re-scans from the beginning.
        import json as _json

        ckpt_dir  = Path("/opt/dhan-trading")
        ckpt_dir  = ckpt_dir if ckpt_dir.exists() else Path("/tmp")
        CKPT_FILE = ckpt_dir / f"backfill_ckpt_{args.exchange_segment}.json"
        id_list   = list(args.security_ids)
        n_total   = len(id_list)

        def _save_checkpoint(idx: int, sid: str):
            tmp = CKPT_FILE.with_suffix(".tmp")
            tmp.write_text(_json.dumps({
                "segment":  args.exchange_segment,
                "index":    idx,
                "last_id":  sid,
                "total":    n_total,
                "ts":       __import__("datetime").datetime.now().isoformat(),
            }))
            tmp.replace(CKPT_FILE)   # atomic

        # Determine the start index
        start_idx = 0
        ckpt_idx  = None
        if CKPT_FILE.exists():
            try:
                d = _json.loads(CKPT_FILE.read_text())
                if d.get("segment") == args.exchange_segment and d.get("last_id") in id_list:
                    # Verify the id still matches the index (list could have changed)
                    saved_id = d["last_id"]
                    if id_list[d["index"]] == saved_id:
                        ckpt_idx = d["index"]
                    else:
                        ckpt_idx = id_list.index(saved_id)
            except Exception as exc:
                logger.warning("Checkpoint unreadable (%s) — will seed from DB", exc)

        if ckpt_idx is not None:
            start_idx = ckpt_idx + 1
            logger.info("Checkpoint: resuming at index %d/%d (after security_id=%s)",
                        start_idx, n_total, id_list[ckpt_idx])
        else:
            # No checkpoint — seed from DB: find furthest already-loaded security
            try:
                from db import get_session
                from sqlalchemy import text as _text
                from datetime import date as _date, timedelta as _td
                min_check = args.from_date + _td(days=7)
                max_check = _date.today() - _td(days=3)
                with get_session() as _s:
                    rows = _s.execute(_text("""
                        SELECT security_id FROM bars WHERE timeframe='1m'
                        GROUP BY security_id
                        HAVING MIN(time)::date <= :mn AND MAX(time)::date >= :mx
                    """), {"mn": min_check, "mx": max_check}).fetchall()
                loaded = {r[0] for r in rows}
                # Find the highest index in our list that's already loaded
                last_loaded_idx = -1
                for i, sid in enumerate(id_list):
                    if sid in loaded:
                        last_loaded_idx = i
                start_idx = last_loaded_idx + 1
                logger.info("No checkpoint — seeded from DB: %d loaded, starting at index %d/%d",
                            len(loaded), start_idx, n_total)
            except Exception as exc:
                logger.warning("DB seeding failed (%s) — starting from index 0", exc)
                start_idx = 0

        try:
            idx = start_idx
            while idx < n_total:
                sid = id_list[idx]
                logger.info("═══ [%d/%d] security_id=%s ═══", idx + 1, n_total, sid)

                # Pick up any token main.py rotated since the last security
                # (multi-day runs outlive a single ~24h token).
                _reload_token(client)

                try:
                    if args.do_intraday:
                        n = await backfill_intraday(
                            client, sid, args.exchange_segment,
                            args.from_date, args.to_date, args.dry_run,
                        )
                        if n > 0:
                            logger.info("  1m done — %d rows upserted", n)

                    if args.do_daily:
                        n = await backfill_daily(
                            client, sid, args.exchange_segment,
                            args.daily_from, args.to_date, args.dry_run,
                        )
                except _TokenExpired as exc:
                    # Token died and no fresh one yet. Do NOT checkpoint — wait
                    # for main.py to rotate, then retry the SAME security.
                    logger.error("  Auth token expired at index %d (sid=%s): %s — "
                                 "holding, will retry once token rotates", idx, sid, exc)
                    if not await _wait_for_fresh_token(client):
                        logger.error("  No fresh token after max wait — is main.py alive? "
                                     "Staying on sid=%s without checkpointing.", sid)
                    continue   # retry same idx; checkpoint not advanced

                # Checkpoint only after a security actually completes (data or
                # legitimately empty) — never on an auth failure.
                if not args.dry_run:
                    _save_checkpoint(idx, sid)
                idx += 1

            logger.info("Segment %s complete — %d securities processed", args.exchange_segment, n_total - start_idx)
        finally:
            if refresh_task:
                refresh_task.cancel()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Instrument sync + OHLCV backfill")

    # ── Instrument loader ──────────────────────────────────────────────────────
    parser.add_argument(
        "--instruments", action="store_true",
        help="Download Dhan scrip master and upsert all instruments into DB (run before backfill)",
    )
    parser.add_argument(
        "--force-download", action="store_true",
        help="Force re-download of scrip master CSV even if cached copy is fresh",
    )

    # ── Security ID selection ──────────────────────────────────────────────────
    parser.add_argument("--ids",
        help="Comma-separated security IDs to backfill")
    parser.add_argument("--nse-eq",  action="store_true", help="All NSE equity (NSE_EQ)")
    parser.add_argument("--nse-cds", action="store_true", help="NSE currency derivatives (NSE_CDS) — USD/INR, EUR/INR etc.")
    parser.add_argument("--bse-eq",  action="store_true", help="All BSE equity (BSE_EQ)")
    parser.add_argument("--mcx",     action="store_true", help="MCX commodity futures (MCX_COMM) — Gold, Silver, Crude etc.")

    # ── Date range ─────────────────────────────────────────────────────────────
    parser.add_argument("--from", dest="from_date",
        default=(date.today() - timedelta(days=90)).strftime("%Y-%m-%d"),
        help="Intraday start date (default: 90 days ago)")
    parser.add_argument("--to", dest="to_date",
        default=date.today().strftime("%Y-%m-%d"),
        help="End date for both intraday and daily (default: today)")
    parser.add_argument("--daily-from", dest="daily_from",
        default=(date.today() - timedelta(days=365 * 5)).strftime("%Y-%m-%d"),
        help="Daily bars start date (default: 5 years ago)")

    # ── Timeframe selection ────────────────────────────────────────────────────
    parser.add_argument("--daily",  action="store_true", help="Daily bars only (no 1-min)")
    parser.add_argument("--all",    action="store_true", help="Both intraday 1-min and daily")

    parser.add_argument("--segment", default=None,
        help="Exchange segment override (default: WATCHLIST_EXCHANGE_SEGMENT)")
    parser.add_argument("--dry-run", action="store_true",
        help="Fetch only — no DB writes")

    raw = parser.parse_args()
    cfg = get_config()

    # ── Init DB — always (instruments + --nse-eq both need it; dry-run still reads) ──
    init_db(cfg.db_url)

    # ── Instrument sync mode ───────────────────────────────────────────────────
    if raw.instruments:
        from core.instrument_sync import load_instruments
        counts = load_instruments(force_download=raw.force_download)
        total = sum(counts.values())
        logger.info("Done — %d instruments loaded across %d segments", total, len(counts))
        return

    # ── OHLCV backfill mode ────────────────────────────────────────────────────
    raw.from_date  = datetime.strptime(raw.from_date,  "%Y-%m-%d").date()
    raw.to_date    = datetime.strptime(raw.to_date,    "%Y-%m-%d").date()
    raw.daily_from = datetime.strptime(raw.daily_from, "%Y-%m-%d").date()

    from core.instrument_sync import get_security_ids_by_segment

    if raw.nse_eq:
        raw.security_ids = get_security_ids_by_segment("NSE_EQ")
        raw.exchange_segment = "NSE_EQ"
    elif raw.nse_cds:
        raw.security_ids = get_security_ids_by_segment("NSE_CDS")
        raw.exchange_segment = "NSE_CDS"
    elif raw.bse_eq:
        raw.security_ids = get_security_ids_by_segment("BSE_EQ")
        raw.exchange_segment = "BSE_EQ"
    elif raw.mcx:
        raw.security_ids = get_security_ids_by_segment("MCX_COMM")
        raw.exchange_segment = "MCX_COMM"
    elif raw.ids:
        raw.security_ids = [s.strip() for s in raw.ids.split(",")]
        raw.exchange_segment = raw.segment or cfg.watchlist_exchange_segment
    else:
        logger.error("Specify a segment flag: --nse-eq | --nse-cds | --bse-eq | --mcx | --ids <list>")
        return

    if not raw.security_ids:
        logger.error("No instruments found for segment — run: python backfill.py --instruments first")
        return

    logger.info("Loaded %d security IDs  segment=%s", len(raw.security_ids), raw.exchange_segment)

    raw.do_intraday = not raw.daily
    raw.do_daily    = raw.daily or raw.all

    logger.info(
        "Backfill  intraday=%s (%s→%s)  daily=%s (%s→%s)  ids=%d",
        raw.do_intraday, raw.from_date, raw.to_date,
        raw.do_daily,    raw.daily_from, raw.to_date,
        len(raw.security_ids),
    )

    asyncio.run(run_backfill(raw, cfg))
    logger.info("Backfill complete.")


if __name__ == "__main__":
    main()
