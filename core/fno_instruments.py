"""
F&O Instrument Master Sync
==========================
Downloads Dhan's DETAILED scrip master CSV, filters to derivative rows
(INSTRUMENT starts with "FUT" or "OPT"), and upserts them into the
`fno_instruments` table (migration 010).

Capture-everything rule: every row is stored verbatim in the `raw` JSONB
column; typed columns are projected from it for query convenience.

CSV source:  https://images.dhan.co/api-data/api-scrip-master-detailed.csv
Cache:       .cache/scrip_master_detailed.csv  (refreshed if older than CACHE_TTL_HOURS)
Header (31 cols, comma-sep):
  EXCH_ID, SEGMENT, SECURITY_ID, ISIN, INSTRUMENT, UNDERLYING_SECURITY_ID,
  UNDERLYING_SYMBOL, SYMBOL_NAME, DISPLAY_NAME, INSTRUMENT_TYPE, SERIES,
  LOT_SIZE, SM_EXPIRY_DATE, STRIKE_PRICE, OPTION_TYPE, TICK_SIZE,
  EXPIRY_FLAG, BRACKET_FLAG, COVER_FLAG, ASM_GSM_FLAG, ASM_GSM_CATEGORY,
  BUY_SELL_INDICATOR, BUY_CO_MIN_MARGIN_PER, BUY_CO_SL_RANGE_MAX_PERC,
  BUY_CO_SL_RANGE_MIN_PERC, BUY_BO_MIN_MARGIN_PER, BUY_BO_PROFIT_RANGE_MAX_PERC,
  BUY_BO_PROFIT_RANGE_MIN_PERC, MTF_LEVERAGE, SM_UPPER_LIMIT, SM_LOWER_LIMIT,
  SM_FREEZE_QTY
"""
from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import requests

logger = logging.getLogger("dhan.fno_instruments")

DETAILED_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
CACHE_DIR           = Path(__file__).parent.parent / ".cache"
CACHE_FILE          = CACHE_DIR / "scrip_master_detailed.csv"
CACHE_TTL_HOURS     = 6
BATCH_SIZE          = 500


# ── Safe numeric coercions ────────────────────────────────────────────────────

def _to_float(value: str | None) -> float | None:
    """Convert a CSV string to float; return None on blank, 'NA', or non-numeric."""
    if value is None:
        return None
    v = value.strip()
    if v == "" or v.upper() == "NA":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(value: str | None) -> int | None:
    """Convert a CSV string to int (via float); return None on blank, 'NA', or non-numeric."""
    f = _to_float(value)
    if f is None:
        return None
    return int(f)


def _parse_expiry(value: str | None) -> date | None:
    """
    Parse SM_EXPIRY_DATE in either 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS' form.
    Returns None for blank/'NA'/unparseable values.
    """
    if value is None:
        return None
    v = value.strip()
    if v == "" or v.upper() == "NA":
        return None
    # Accept both 'YYYY-MM-DD' and 'YYYY-MM-DD HH:MM:SS'
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    logger.debug("Could not parse expiry date: %r", v)
    return None


# ── Row parser (pure — no I/O, no DB) ────────────────────────────────────────

def _parse_row(row: dict) -> dict | None:
    """
    Parse one CSV DictReader row into an fno_instruments upsert dict.

    Returns None if the row should be filtered out (INSTRUMENT does not start
    with 'FUT' or 'OPT', or SECURITY_ID is missing).

    Exposed as a top-level function so tests can call it directly.
    """
    security_id = row.get("SECURITY_ID", "").strip()
    if not security_id:
        return None

    instrument = row.get("INSTRUMENT", "").strip()
    if not (instrument.startswith("FUT") or instrument.startswith("OPT")):
        return None

    strike_raw   = _to_float(row.get("STRIKE_PRICE"))
    freeze_raw   = _to_int(row.get("SM_FREEZE_QTY"))
    lot_size_raw = _to_int(row.get("LOT_SIZE"))

    return {
        "security_id":            security_id,
        "exch_id":                row.get("EXCH_ID", "").strip() or None,
        "segment":                row.get("SEGMENT", "").strip() or None,
        "isin":                   row.get("ISIN", "").strip() or None,
        "instrument":             instrument or None,
        "underlying_security_id": row.get("UNDERLYING_SECURITY_ID", "").strip() or None,
        "underlying_symbol":      row.get("UNDERLYING_SYMBOL", "").strip() or None,
        "symbol_name":            row.get("SYMBOL_NAME", "").strip() or None,
        "display_name":           row.get("DISPLAY_NAME", "").strip() or None,
        "instrument_type":        row.get("INSTRUMENT_TYPE", "").strip() or None,
        "series":                 row.get("SERIES", "").strip() or None,
        "lot_size":               lot_size_raw,
        "expiry_date":            _parse_expiry(row.get("SM_EXPIRY_DATE")),
        "strike_price":           strike_raw,
        "option_type":            row.get("OPTION_TYPE", "").strip() or None,
        "tick_size":              _to_float(row.get("TICK_SIZE")),
        "expiry_flag":            row.get("EXPIRY_FLAG", "").strip() or None,
        "bracket_flag":           row.get("BRACKET_FLAG", "").strip() or None,
        "cover_flag":             row.get("COVER_FLAG", "").strip() or None,
        "asm_gsm_flag":           row.get("ASM_GSM_FLAG", "").strip() or None,
        "asm_gsm_category":       row.get("ASM_GSM_CATEGORY", "").strip() or None,
        "buy_sell_indicator":     row.get("BUY_SELL_INDICATOR", "").strip() or None,
        "mtf_leverage":           _to_float(row.get("MTF_LEVERAGE")),
        "upper_circuit":          _to_float(row.get("SM_UPPER_LIMIT")),
        "lower_circuit":          _to_float(row.get("SM_LOWER_LIMIT")),
        "freeze_qty":             freeze_raw,
        "raw":                    dict(row),   # capture-everything: full original row
    }


def _iter_rows(csv_text: str) -> Iterator[dict]:
    """Yield parsed upsert dicts for every FUT/OPT row in the CSV."""
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        parsed = _parse_row(row)
        if parsed is not None:
            yield parsed


# ── CSV download / cache ──────────────────────────────────────────────────────

def _download_csv(force: bool = False) -> str:
    """Return CSV text, downloading from Dhan if cache is missing or stale."""
    CACHE_DIR.mkdir(exist_ok=True)

    if not force and CACHE_FILE.exists():
        age_hours = (time.time() - CACHE_FILE.stat().st_mtime) / 3600
        if age_hours < CACHE_TTL_HOURS:
            logger.info("Using cached detailed scrip master (%.1fh old)", age_hours)
            return CACHE_FILE.read_text(encoding="utf-8-sig")

    logger.info("Downloading detailed scrip master from Dhan...")
    resp = requests.get(DETAILED_MASTER_URL, timeout=60)
    resp.raise_for_status()
    content = resp.content.decode("utf-8-sig")   # strip BOM if present
    CACHE_FILE.write_text(content, encoding="utf-8")
    logger.info("Downloaded %.1f MB, cached to %s", len(content) / 1e6, CACHE_FILE)
    return content


# ── DB upsert via psycopg2 execute_values ─────────────────────────────────────

_UPSERT_SQL = """
    INSERT INTO fno_instruments (
        security_id, exch_id, segment, isin, instrument,
        underlying_security_id, underlying_symbol, symbol_name, display_name,
        instrument_type, series, lot_size, expiry_date, strike_price,
        option_type, tick_size, expiry_flag, bracket_flag, cover_flag,
        asm_gsm_flag, asm_gsm_category, buy_sell_indicator, mtf_leverage,
        upper_circuit, lower_circuit, freeze_qty, raw, updated_at
    )
    VALUES %s
    ON CONFLICT (security_id) DO UPDATE SET
        exch_id                = EXCLUDED.exch_id,
        segment                = EXCLUDED.segment,
        isin                   = EXCLUDED.isin,
        instrument             = EXCLUDED.instrument,
        underlying_security_id = EXCLUDED.underlying_security_id,
        underlying_symbol      = EXCLUDED.underlying_symbol,
        symbol_name            = EXCLUDED.symbol_name,
        display_name           = EXCLUDED.display_name,
        instrument_type        = EXCLUDED.instrument_type,
        series                 = EXCLUDED.series,
        lot_size               = EXCLUDED.lot_size,
        expiry_date            = EXCLUDED.expiry_date,
        strike_price           = EXCLUDED.strike_price,
        option_type            = EXCLUDED.option_type,
        tick_size              = EXCLUDED.tick_size,
        expiry_flag            = EXCLUDED.expiry_flag,
        bracket_flag           = EXCLUDED.bracket_flag,
        cover_flag             = EXCLUDED.cover_flag,
        asm_gsm_flag           = EXCLUDED.asm_gsm_flag,
        asm_gsm_category       = EXCLUDED.asm_gsm_category,
        buy_sell_indicator     = EXCLUDED.buy_sell_indicator,
        mtf_leverage           = EXCLUDED.mtf_leverage,
        upper_circuit          = EXCLUDED.upper_circuit,
        lower_circuit          = EXCLUDED.lower_circuit,
        freeze_qty             = EXCLUDED.freeze_qty,
        raw                    = EXCLUDED.raw,
        updated_at             = now()
"""

_ROW_TEMPLATE = (
    "%(security_id)s, %(exch_id)s, %(segment)s, %(isin)s, %(instrument)s, "
    "%(underlying_security_id)s, %(underlying_symbol)s, %(symbol_name)s, %(display_name)s, "
    "%(instrument_type)s, %(series)s, %(lot_size)s, %(expiry_date)s, %(strike_price)s, "
    "%(option_type)s, %(tick_size)s, %(expiry_flag)s, %(bracket_flag)s, %(cover_flag)s, "
    "%(asm_gsm_flag)s, %(asm_gsm_category)s, %(buy_sell_indicator)s, %(mtf_leverage)s, "
    "%(upper_circuit)s, %(lower_circuit)s, %(freeze_qty)s, %(raw)s::jsonb, now()"
)


def _upsert_batch(session: Any, batch: list[dict]) -> int:
    """
    Upsert a batch of parsed rows into fno_instruments.

    Uses psycopg2 execute_values for efficient bulk insertion, mirroring the
    pattern used in core/fno_backfill.py (raw psycopg2 connection from the
    SQLAlchemy session's underlying connection).
    """
    from psycopg2.extras import execute_values

    # Serialise raw dict to JSON string before passing to psycopg2
    rows = []
    for r in batch:
        row = dict(r)
        row["raw"] = json.dumps(row["raw"])
        rows.append(row)

    raw_conn = session.connection().connection
    with raw_conn.cursor() as cur:
        execute_values(cur, _UPSERT_SQL, rows, template=f"({_ROW_TEMPLATE})")
    return len(rows)


# ── Public entry point ────────────────────────────────────────────────────────

def sync_fno_instruments(force_download: bool = False) -> dict:
    """
    Download the detailed scrip master, filter to FUT/OPT rows, and upsert
    into `fno_instruments`.

    Returns a dict of segment/instrument counts, e.g.:
        {'NSE_FNO': {'FUTIDX': 2, 'OPTIDX': 148, ...}, 'MCX_COMM': {...}}
    """
    from db import get_session   # lazy import — module usable before DB init

    csv_text = _download_csv(force=force_download)

    counts: dict = {}   # segment -> instrument -> count
    batch: list[dict] = []
    total_loaded = 0

    with get_session() as session:
        for row in _iter_rows(csv_text):
            seg  = row.get("segment") or "UNKNOWN"
            inst = row.get("instrument") or "UNKNOWN"
            counts.setdefault(seg, {})
            counts[seg][inst] = counts[seg].get(inst, 0) + 1

            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                total_loaded += _upsert_batch(session, batch)
                batch.clear()
                if total_loaded % 10_000 == 0:
                    logger.info("  Loaded %d rows...", total_loaded)

        if batch:
            total_loaded += _upsert_batch(session, batch)

    logger.info("F&O instrument sync complete — %d rows upserted", total_loaded)
    for seg, inst_counts in sorted(counts.items()):
        for inst, cnt in sorted(inst_counts.items(), key=lambda x: -x[1]):
            logger.info("  %-15s %-12s %d", seg, inst, cnt)

    return counts


# ── DB read helpers (lazy DB import) ─────────────────────────────────────────

def get_contract(security_id: str) -> dict | None:
    """
    Fetch one fno_instruments row by security_id.
    Returns a dict of all columns, or None if not found.
    """
    from db import get_session
    from sqlalchemy import text

    with get_session() as session:
        row = session.execute(
            text("SELECT * FROM fno_instruments WHERE security_id = :sid"),
            {"sid": security_id},
        ).mappings().first()
    return dict(row) if row else None


def nifty_future_contracts() -> list[dict]:
    """
    Return all NIFTY index futures (INSTRUMENT='FUTIDX', UNDERLYING_SYMBOL='NIFTY'),
    ordered by expiry_date ascending (front month first).
    """
    from db import get_session
    from sqlalchemy import text

    with get_session() as session:
        rows = session.execute(
            text("""
                SELECT * FROM fno_instruments
                WHERE instrument = 'FUTIDX'
                  AND underlying_symbol = 'NIFTY'
                ORDER BY expiry_date ASC
            """),
        ).mappings().fetchall()
    return [dict(r) for r in rows]


def nearest_nifty_future() -> dict | None:
    """
    Return the nearest (front-month) NIFTY index future — the row with the
    minimum expiry_date that is today or in the future.
    """
    from db import get_session
    from sqlalchemy import text

    today = date.today().isoformat()
    with get_session() as session:
        row = session.execute(
            text("""
                SELECT * FROM fno_instruments
                WHERE instrument = 'FUTIDX'
                  AND underlying_symbol = 'NIFTY'
                  AND expiry_date >= :today
                ORDER BY expiry_date ASC
                LIMIT 1
            """),
            {"today": today},
        ).mappings().first()
    return dict(row) if row else None
