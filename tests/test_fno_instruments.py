"""
Tests for core/fno_instruments.py

No DB, no network: _download_csv is monkeypatched to return a small inline CSV
with the real 31-column header plus 3 rows:
  1. NIFTY FUTIDX row  → must be kept; mapped correctly
  2. NIFTY OPTIDX CE row → must be kept; mapped correctly
  3. EQUITY row          → must be filtered out

The upsert helper is also monkeypatched to capture rows so sync_fno_instruments()
can be smoke-tested end-to-end without a database.
"""

from datetime import date

import pytest

from core.fno_instruments import (
    _iter_rows,
    _parse_expiry,
    _parse_row,
    _to_float,
    _to_int,
    sync_fno_instruments,
)

# ── Inline test CSV ───────────────────────────────────────────────────────────

_HEADER = (
    "EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,UNDERLYING_SECURITY_ID,"
    "UNDERLYING_SYMBOL,SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,"
    "LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,TICK_SIZE,EXPIRY_FLAG,"
    "BRACKET_FLAG,COVER_FLAG,ASM_GSM_FLAG,ASM_GSM_CATEGORY,BUY_SELL_INDICATOR,"
    "BUY_CO_MIN_MARGIN_PER,BUY_CO_SL_RANGE_MAX_PERC,BUY_CO_SL_RANGE_MIN_PERC,"
    "BUY_BO_MIN_MARGIN_PER,BUY_BO_PROFIT_RANGE_MAX_PERC,BUY_BO_PROFIT_RANGE_MIN_PERC,"
    "MTF_LEVERAGE,SM_UPPER_LIMIT,SM_LOWER_LIMIT,SM_FREEZE_QTY"
)

# Row 1 — NIFTY FUTIDX (future, no real strike → -0.01 sentinel)
_FUTIDX_ROW = (
    "NSE,NSE_FNO,49081,NA,FUTIDX,13,NIFTY,NIFTY JUN FUT,NIFTY JUN FUT,"
    "FUT,NA,50,2026-06-26,-0.01,XX,0.05,N,N,N,N,NA,0,"
    "0,0,0,0,0,0,"
    "0,24000.00,22000.00,1800"
)

# Row 2 — NIFTY OPTIDX CE (option)
_OPTIDX_ROW = (
    "NSE,NSE_FNO,49082,NA,OPTIDX,13,NIFTY,NIFTY 24000 CE,NIFTY 24000 CE JUN,"
    "OP,NA,50,2026-06-26 00:00:00,24000.0,CE,0.05,N,N,N,N,NA,0,"
    "0,0,0,0,0,0,"
    "0,26000.00,22000.00,2700"
)

# Row 3 — EQUITY (must be filtered out)
_EQUITY_ROW = (
    "NSE,NSE_EQ,1333,INE040A01034,EQUITY,NA,HDFCBANK,HDFC BANK LTD,HDFC BANK,"
    "EQ,EQ,1,NA,NA,NA,0.05,NA,N,N,N,NA,0,"
    "0,0,0,0,0,0,"
    "0,1800.00,1500.00,NA"
)

_TEST_CSV = "\n".join([_HEADER, _FUTIDX_ROW, _OPTIDX_ROW, _EQUITY_ROW])

_ALL_HEADERS = [h.strip() for h in _HEADER.split(",")]


# ── Coercion helpers ──────────────────────────────────────────────────────────

def test_to_float_normal():
    assert _to_float("24000.0") == pytest.approx(24000.0)


def test_to_float_blank_and_na():
    assert _to_float("") is None
    assert _to_float("NA") is None
    assert _to_float("  NA  ") is None
    assert _to_float(None) is None


def test_to_float_non_numeric():
    assert _to_float("N/A") is None
    assert _to_float("abc") is None


def test_to_int_normal():
    assert _to_int("1800") == 1800
    assert _to_int("50.0") == 50


def test_to_int_blank():
    assert _to_int("") is None
    assert _to_int("NA") is None


def test_parse_expiry_date_only():
    assert _parse_expiry("2026-06-26") == date(2026, 6, 26)


def test_parse_expiry_datetime():
    assert _parse_expiry("2026-06-26 00:00:00") == date(2026, 6, 26)


def test_parse_expiry_blank_and_na():
    assert _parse_expiry("") is None
    assert _parse_expiry("NA") is None
    assert _parse_expiry(None) is None


# ── _parse_row filtering ──────────────────────────────────────────────────────

def _csv_row(line: str) -> dict:
    """Build a dict from a single CSV data row using the known header."""
    import csv, io
    text = _HEADER + "\n" + line
    reader = csv.DictReader(io.StringIO(text))
    return next(reader)


def test_equity_row_filtered():
    assert _parse_row(_csv_row(_EQUITY_ROW)) is None


def test_futidx_row_kept():
    result = _parse_row(_csv_row(_FUTIDX_ROW))
    assert result is not None


def test_optidx_row_kept():
    result = _parse_row(_csv_row(_OPTIDX_ROW))
    assert result is not None


# ── FUTIDX row field mapping ──────────────────────────────────────────────────

@pytest.fixture()
def fut_row():
    return _parse_row(_csv_row(_FUTIDX_ROW))


def test_fut_security_id(fut_row):
    assert fut_row["security_id"] == "49081"


def test_fut_instrument(fut_row):
    assert fut_row["instrument"] == "FUTIDX"


def test_fut_option_type_xx(fut_row):
    """Futures use 'XX' as the option_type sentinel."""
    assert fut_row["option_type"] == "XX"


def test_fut_strike_sentinel(fut_row):
    """Futures carry -0.01 as strike sentinel — stored as-is (not coerced to None)."""
    assert fut_row["strike_price"] == pytest.approx(-0.01)


def test_fut_lot_size_int(fut_row):
    assert fut_row["lot_size"] == 50
    assert isinstance(fut_row["lot_size"], int)


def test_fut_expiry_parsed(fut_row):
    assert fut_row["expiry_date"] == date(2026, 6, 26)


def test_fut_freeze_qty_int(fut_row):
    assert fut_row["freeze_qty"] == 1800
    assert isinstance(fut_row["freeze_qty"], int)


def test_fut_upper_lower_circuit(fut_row):
    assert fut_row["upper_circuit"] == pytest.approx(24000.0)
    assert fut_row["lower_circuit"] == pytest.approx(22000.0)


def test_fut_underlying_symbol(fut_row):
    assert fut_row["underlying_symbol"] == "NIFTY"


# ── OPTIDX row field mapping ──────────────────────────────────────────────────

@pytest.fixture()
def opt_row():
    return _parse_row(_csv_row(_OPTIDX_ROW))


def test_opt_security_id(opt_row):
    assert opt_row["security_id"] == "49082"


def test_opt_instrument(opt_row):
    assert opt_row["instrument"] == "OPTIDX"


def test_opt_option_type_ce(opt_row):
    assert opt_row["option_type"] == "CE"


def test_opt_strike_float(opt_row):
    assert opt_row["strike_price"] == pytest.approx(24000.0)
    assert isinstance(opt_row["strike_price"], float)


def test_opt_lot_size_int(opt_row):
    assert opt_row["lot_size"] == 50
    assert isinstance(opt_row["lot_size"], int)


def test_opt_expiry_datetime_string(opt_row):
    """SM_EXPIRY_DATE with 'YYYY-MM-DD HH:MM:SS' format is parsed correctly."""
    assert opt_row["expiry_date"] == date(2026, 6, 26)


def test_opt_freeze_qty_int(opt_row):
    assert opt_row["freeze_qty"] == 2700
    assert isinstance(opt_row["freeze_qty"], int)


# ── raw dict contains all 31 header keys ─────────────────────────────────────

def test_fut_raw_contains_all_31_keys(fut_row):
    raw = fut_row["raw"]
    assert isinstance(raw, dict)
    for key in _ALL_HEADERS:
        assert key in raw, f"Missing key in raw: {key!r}"


def test_opt_raw_contains_all_31_keys(opt_row):
    raw = opt_row["raw"]
    assert isinstance(raw, dict)
    for key in _ALL_HEADERS:
        assert key in raw, f"Missing key in raw: {key!r}"


# ── _iter_rows — full CSV filtering ──────────────────────────────────────────

def test_iter_rows_filters_equity():
    rows = list(_iter_rows(_TEST_CSV))
    # Only 2 of the 3 rows should survive (FUT + OPT, not EQUITY)
    assert len(rows) == 2


def test_iter_rows_security_ids():
    rows = list(_iter_rows(_TEST_CSV))
    ids = {r["security_id"] for r in rows}
    assert ids == {"49081", "49082"}


# ── sync_fno_instruments smoke test (monkeypatched) ──────────────────────────

def test_sync_smoke(monkeypatch):
    """
    End-to-end sync without a real DB or network.
    Monkeypatches _download_csv to return the inline CSV and _upsert_batch to
    capture rows instead of hitting PostgreSQL.
    """
    import core.fno_instruments as fi

    captured: list[dict] = []

    def fake_download(force=False):
        return _TEST_CSV

    def fake_upsert(session, batch):
        captured.extend(batch)
        return len(batch)

    # Patch at module level (the names used inside sync_fno_instruments)
    monkeypatch.setattr(fi, "_download_csv", fake_download)
    monkeypatch.setattr(fi, "_upsert_batch", fake_upsert)

    # get_session is imported lazily inside sync_fno_instruments — patch it on db module
    import contextlib
    import db as _db_mod

    @contextlib.contextmanager
    def fake_session():
        yield object()   # session object is never used (upsert is monkeypatched)

    monkeypatch.setattr(_db_mod, "get_session", fake_session)

    counts = sync_fno_instruments()

    # 2 derivative rows were upserted
    assert len(captured) == 2
    # Both are in NSE_FNO segment
    assert "NSE_FNO" in counts
    nse = counts["NSE_FNO"]
    assert nse.get("FUTIDX", 0) == 1
    assert nse.get("OPTIDX", 0) == 1
