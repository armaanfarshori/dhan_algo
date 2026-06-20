"""Tests for core/fno_equity_universe.py — pure parse + JSON round-trip.

No network, no DB: the parser runs on an inline detailed-scrip-master CSV slice
with the real 31-column header. Covers:
  • FUTSTK rows are selected per distinct underlying; near-month chosen on min expiry.
  • Non-NSE / non-FUTSTK / index / equity / OPTSTK rows are excluded.
  • Subset projection (symbol, underlying_security_id, future_security_id,
    near_expiry, lot_size) is correct.
  • write_universe → load_universe round-trips (near_expiry parses back to a date).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import core.fno_equity_universe as eu

# Real 31-column detailed-master header (mirrors core/fno_instruments).
_HEADER = (
    "EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,UNDERLYING_SECURITY_ID,"
    "UNDERLYING_SYMBOL,SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,"
    "LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,TICK_SIZE,EXPIRY_FLAG,"
    "BRACKET_FLAG,COVER_FLAG,ASM_GSM_FLAG,ASM_GSM_CATEGORY,BUY_SELL_INDICATOR,"
    "BUY_CO_MIN_MARGIN_PER,BUY_CO_SL_RANGE_MAX_PERC,BUY_CO_SL_RANGE_MIN_PERC,"
    "BUY_BO_MIN_MARGIN_PER,BUY_BO_PROFIT_RANGE_MAX_PERC,BUY_BO_PROFIT_RANGE_MIN_PERC,"
    "MTF_LEVERAGE,SM_UPPER_LIMIT,SM_LOWER_LIMIT,SM_FREEZE_QTY"
)


def _row(exch, instr, sec_id, u_sec_id, sym, lot, expiry, opt="XX", strike="-0.01"):
    cols = [
        exch, "NSE_FNO" if exch == "NSE" else "NSE_EQ", sec_id, "NA", instr, u_sec_id,
        sym, f"{sym} FUT", f"{sym} FUT", "FUT", "NA",
        lot, expiry, strike, opt, "0.05", "N",
        "N", "N", "N", "NA", "0",
        "0", "0", "0",
        "0", "0", "0",
        "0", "100.0", "50.0", "1000",
    ]
    return ",".join(cols)


# RELIANCE: two FUTSTK contracts (near + far month) → near-month must win.
_RELIANCE_NEAR = _row("NSE", "FUTSTK", "61001", "2885", "RELIANCE", "500", "2026-06-26")
_RELIANCE_FAR = _row("NSE", "FUTSTK", "61002", "2885", "RELIANCE", "500", "2026-07-31")
# TCS: single FUTSTK.
_TCS = _row("NSE", "FUTSTK", "61010", "11536", "TCS", "175", "2026-06-26 00:00:00")
# NIFTY index future (FUTIDX) — must be excluded (index, not stock).
_NIFTY_FUT = _row("NSE", "FUTIDX", "49081", "13", "NIFTY", "50", "2026-06-26")
# RELIANCE stock OPTION (OPTSTK) — must be excluded (we key on FUTSTK).
_RELIANCE_OPT = _row("NSE", "OPTSTK", "62000", "2885", "RELIANCE", "500", "2026-06-26", opt="CE", strike="2900")
# A BSE/MCX-ish row (non-NSE exch) — must be excluded.
_BSE_FUT = _row("BSE", "FUTSTK", "70000", "9999", "FOOBAR", "100", "2026-06-26")

_CSV = "\n".join([_HEADER, _RELIANCE_FAR, _RELIANCE_NEAR, _TCS, _NIFTY_FUT, _RELIANCE_OPT, _BSE_FUT])


def test_parse_selects_distinct_stock_underlyings():
    universe = eu.parse_stock_fno_universe(_CSV)
    syms = [u["underlying_symbol"] for u in universe]
    # Only the two NSE FUTSTK underlyings, sorted.
    assert syms == ["RELIANCE", "TCS"]


def test_parse_picks_near_month_future():
    universe = eu.parse_stock_fno_universe(_CSV)
    reliance = next(u for u in universe if u["underlying_symbol"] == "RELIANCE")
    # Near-month (2026-06-26 / id 61001) must win over far (2026-07-31 / id 61002).
    assert reliance["future_security_id"] == "61001"
    assert reliance["near_expiry"] == date(2026, 6, 26)
    assert reliance["underlying_security_id"] == "2885"
    assert reliance["lot_size"] == 500


def test_parse_handles_datetime_expiry_format():
    universe = eu.parse_stock_fno_universe(_CSV)
    tcs = next(u for u in universe if u["underlying_symbol"] == "TCS")
    assert tcs["near_expiry"] == date(2026, 6, 26)
    assert tcs["lot_size"] == 175


def test_parse_excludes_index_options_and_non_nse():
    universe = eu.parse_stock_fno_universe(_CSV)
    syms = {u["underlying_symbol"] for u in universe}
    assert "NIFTY" not in syms      # FUTIDX excluded
    assert "FOOBAR" not in syms     # non-NSE excluded


def test_write_and_load_roundtrip(tmp_path: Path):
    universe = eu.parse_stock_fno_universe(_CSV)
    path = tmp_path / "uni.json"
    eu.write_universe(universe, path)
    assert path.exists()

    loaded = eu.load_universe(path)
    assert [u["underlying_symbol"] for u in loaded] == ["RELIANCE", "TCS"]
    reliance = next(u for u in loaded if u["underlying_symbol"] == "RELIANCE")
    assert reliance["near_expiry"] == date(2026, 6, 26)   # parsed back to a date
    assert reliance["future_security_id"] == "61001"
    assert reliance["lot_size"] == 500


def test_refresh_universe_uses_cached_downloader(monkeypatch, tmp_path: Path):
    """refresh_universe must reuse fno_instruments._download_csv (no direct network)."""
    import core.fno_instruments as fi

    monkeypatch.setattr(fi, "_download_csv", lambda force=False: _CSV)
    path = tmp_path / "uni.json"
    universe = eu.refresh_universe(path=path)
    assert [u["underlying_symbol"] for u in universe] == ["RELIANCE", "TCS"]
    assert path.exists()
