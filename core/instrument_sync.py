"""
Instrument Master Sync
======================
Downloads Dhan's scrip master CSV, parses all instruments across all
segments, and upserts them into the `instruments` table in TimescaleDB.

Called by:  python backfill.py --instruments

CSV source: https://images.dhan.co/api-data/api-scrip-master.csv
Cache:      .cache/scrip_master.csv  (refreshed if older than CACHE_TTL_HOURS)
Total rows: ~224,000 across NSE_EQ, NSE_FNO, NSE_CDS, NSE_IDX, BSE

The `instruments` table is the join key for everything else — every
security_id in bars/ticks/orders/signals links back here.
"""

import csv
import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import requests
from sqlalchemy import text

logger = logging.getLogger("dhan.instrument_sync")

SCRIP_MASTER_URL  = "https://images.dhan.co/api-data/api-scrip-master.csv"
CACHE_DIR         = Path(__file__).parent.parent / ".cache"
CACHE_FILE        = CACHE_DIR / "scrip_master.csv"
CACHE_TTL_HOURS   = 6
BATCH_SIZE        = 500   # rows per DB batch


# ── Dhan segment code → exchange_segment string ───────────────────────────────
# SEM_SEGMENT values seen in the wild: E, D, C, M, I
# SEM_EXM_EXCH_ID is NSE or BSE and refines the M segment

def _exchange_segment(seg: str, exchange: str, instrument: str) -> str:
    if seg == "E":
        return "NSE_EQ"
    if seg == "D":
        return "NSE_FNO"
    if seg == "C":
        return "NSE_CDS"
    if seg == "I":
        return "NSE_IDX"
    if seg == "M":
        # BSE segment — equity vs derivatives
        return "BSE_EQ" if instrument == "EQUITY" else "BSE_FNO"
    return f"{exchange}_{seg}"   # fallback — preserves unknown segments


# ── Row dataclass ──────────────────────────────────────────────────────────────

@dataclass(slots=True)
class Instrument:
    security_id:      str
    ticker:           str
    symbol:           str
    name:             str
    exchange_segment: str
    instrument_type:  str
    lot_size:         int
    tick_size:        float
    expiry_date:      str   # empty string for non-expiring instruments
    strike_price:     float
    option_type:      str   # CE / PE / XX (not an option)


def _parse_row(row: dict) -> Instrument | None:
    sec_id = row["SEM_SMST_SECURITY_ID"].strip()
    if not sec_id:
        return None

    exchange   = row["SEM_EXM_EXCH_ID"].strip()
    seg        = row["SEM_SEGMENT"].strip()
    instrument = row["SEM_INSTRUMENT_NAME"].strip()

    try:
        lot_size  = max(1, int(float(row["SEM_LOT_UNITS"] or 1)))
    except (ValueError, TypeError):
        lot_size = 1

    try:
        tick_size = float(row["SEM_TICK_SIZE"] or 0.05)
    except (ValueError, TypeError):
        tick_size = 0.05

    try:
        strike = float(row["SEM_STRIKE_PRICE"] or 0)
    except (ValueError, TypeError):
        strike = 0.0

    return Instrument(
        security_id      = sec_id,
        ticker           = row["SEM_TRADING_SYMBOL"].strip(),
        symbol           = row["SM_SYMBOL_NAME"].strip(),
        name             = row["SEM_CUSTOM_SYMBOL"].strip(),
        exchange_segment = _exchange_segment(seg, exchange, instrument),
        instrument_type  = instrument,
        lot_size         = lot_size,
        tick_size        = tick_size,
        expiry_date      = row["SEM_EXPIRY_DATE"].strip(),
        strike_price     = strike,
        option_type      = row["SEM_OPTION_TYPE"].strip(),
    )


# ── CSV download / cache ───────────────────────────────────────────────────────

def _download_csv(force: bool = False) -> str:
    CACHE_DIR.mkdir(exist_ok=True)

    if not force and CACHE_FILE.exists():
        age_hours = (time.time() - CACHE_FILE.stat().st_mtime) / 3600
        if age_hours < CACHE_TTL_HOURS:
            logger.info("Using cached scrip master (%.1fh old)", age_hours)
            return CACHE_FILE.read_text(encoding="utf-8-sig")

    logger.info("Downloading scrip master from Dhan…")
    resp = requests.get(SCRIP_MASTER_URL, timeout=60)
    resp.raise_for_status()
    content = resp.content.decode("utf-8-sig")   # strip BOM if present
    CACHE_FILE.write_text(content, encoding="utf-8")
    logger.info("Downloaded %.1f MB, cached to %s", len(content) / 1e6, CACHE_FILE)
    return content


def _iter_instruments(csv_text: str) -> Iterator[Instrument]:
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        inst = _parse_row(row)
        if inst is not None:
            yield inst


# ── DB upsert ─────────────────────────────────────────────────────────────────

_UPSERT_SQL = text("""
    INSERT INTO instruments
        (security_id, ticker, symbol, name, exchange_segment,
         instrument_type, lot_size, tick_size)
    VALUES
        (:security_id, :ticker, :symbol, :name, :exchange_segment,
         :instrument_type, :lot_size, :tick_size)
    ON CONFLICT (security_id) DO UPDATE SET
        ticker           = EXCLUDED.ticker,
        symbol           = EXCLUDED.symbol,
        name             = EXCLUDED.name,
        exchange_segment = EXCLUDED.exchange_segment,
        instrument_type  = EXCLUDED.instrument_type,
        lot_size         = EXCLUDED.lot_size,
        tick_size        = EXCLUDED.tick_size
""")


def _upsert_batch(session, batch: list[dict]) -> int:
    session.execute(_UPSERT_SQL, batch)
    return len(batch)


# ── Public entry point ─────────────────────────────────────────────────────────

def load_instruments(force_download: bool = False) -> dict[str, int]:
    """
    Download + parse scrip master and upsert all instruments into TimescaleDB.
    Returns a dict of segment → count, e.g. {'NSE_EQ': 22724, 'NSE_FNO': ...}
    """
    from db import get_session   # imported here to keep module importable before DB init

    csv_text = _download_csv(force=force_download)

    segment_counts: dict[str, int] = {}
    batch: list[dict] = []
    total_loaded = 0
    total_parsed = 0

    with get_session() as session:
        for inst in _iter_instruments(csv_text):
            total_parsed += 1
            batch.append({
                "security_id":      inst.security_id,
                "ticker":           inst.ticker[:20] if inst.ticker else "",
                "symbol":           inst.symbol[:50] if inst.symbol else "",
                "name":             inst.name[:200] if inst.name else "",
                "exchange_segment": inst.exchange_segment,
                "instrument_type":  inst.instrument_type[:20],
                "lot_size":         inst.lot_size,
                "tick_size":        inst.tick_size,
            })
            segment_counts[inst.exchange_segment] = segment_counts.get(inst.exchange_segment, 0) + 1

            if len(batch) >= BATCH_SIZE:
                total_loaded += _upsert_batch(session, batch)
                batch.clear()
                if total_loaded % 10_000 == 0:
                    logger.info("  Loaded %d / %d rows…", total_loaded, total_parsed)

        if batch:
            total_loaded += _upsert_batch(session, batch)

    logger.info("Instrument sync complete — %d rows upserted", total_loaded)
    for seg, cnt in sorted(segment_counts.items(), key=lambda x: -x[1]):
        logger.info("  %-15s %d", seg, cnt)

    return segment_counts


def get_security_ids_by_segment(segment: str) -> list[str]:
    """Return all security_ids for a given exchange_segment from the DB."""
    from db import get_session
    with get_session() as session:
        rows = session.execute(
            text("SELECT security_id FROM instruments WHERE exchange_segment = :seg ORDER BY security_id"),
            {"seg": segment},
        ).fetchall()
    return [r[0] for r in rows]


def get_equity_security_ids() -> list[str]:
    """All NSE_EQ equity security IDs — the main backfill target."""
    return get_security_ids_by_segment("NSE_EQ")
