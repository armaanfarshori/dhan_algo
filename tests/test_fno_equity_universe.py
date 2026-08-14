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

# Frozen 'today' predating every fixture expiry above — the parser drops expired
# contracts, so tests that ride on wall-clock time started failing once
# 2026-06-26 passed. Always pin `today` when parsing the shared fixture CSV.
_FROZEN_TODAY = date(2026, 6, 20)


def test_parse_selects_distinct_stock_underlyings():
    universe = eu.parse_stock_fno_universe(_CSV, today=_FROZEN_TODAY)
    syms = [u["underlying_symbol"] for u in universe]
    # Only the two NSE FUTSTK underlyings, sorted.
    assert syms == ["RELIANCE", "TCS"]


def test_parse_picks_near_month_future():
    universe = eu.parse_stock_fno_universe(_CSV, today=_FROZEN_TODAY)
    reliance = next(u for u in universe if u["underlying_symbol"] == "RELIANCE")
    # Near-month (2026-06-26 / id 61001) must win over far (2026-07-31 / id 61002).
    assert reliance["future_security_id"] == "61001"
    assert reliance["near_expiry"] == date(2026, 6, 26)
    assert reliance["underlying_security_id"] == "2885"
    assert reliance["lot_size"] == 500


def test_parse_handles_datetime_expiry_format():
    universe = eu.parse_stock_fno_universe(_CSV, today=_FROZEN_TODAY)
    tcs = next(u for u in universe if u["underlying_symbol"] == "TCS")
    assert tcs["near_expiry"] == date(2026, 6, 26)
    assert tcs["lot_size"] == 175


def test_parse_excludes_index_options_and_non_nse():
    universe = eu.parse_stock_fno_universe(_CSV, today=_FROZEN_TODAY)
    syms = {u["underlying_symbol"] for u in universe}
    assert "NIFTY" not in syms      # FUTIDX excluded
    assert "FOOBAR" not in syms     # non-NSE excluded


def test_write_and_load_roundtrip(tmp_path: Path):
    universe = eu.parse_stock_fno_universe(_CSV, today=_FROZEN_TODAY)
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
    universe = eu.refresh_universe(path=path, today=_FROZEN_TODAY)
    assert [u["underlying_symbol"] for u in universe] == ["RELIANCE", "TCS"]
    assert path.exists()


# ── new QA-residual tests ─────────────────────────────────────────────────────────

def test_parse_expired_contract_skipped_live_wins():
    """An expired FUTSTK row must be skipped; a live row for the same symbol wins.

    Fixes the QA residual: without a '>= today' guard, a lingering expired contract
    could win the min-expiry selection and yield a dead future_security_id.
    Uses IST-safe 'today' kwarg to avoid the IST/UTC CI trap.
    """
    # INFY: one expired (2020-01-30, sec_id 99001) and one live (2026-07-31, sec_id 99002).
    expired = _row("NSE", "FUTSTK", "99001", "1594", "INFY", "300", "2020-01-30")
    live = _row("NSE", "FUTSTK", "99002", "1594", "INFY", "300", "2026-07-31")
    csv_text = "\n".join([_HEADER, expired, live])

    today = date(2026, 6, 20)
    universe = eu.parse_stock_fno_universe(csv_text, today=today)

    assert len(universe) == 1
    infy = universe[0]
    assert infy["underlying_symbol"] == "INFY"
    # The LIVE contract must win, not the expired one.
    assert infy["future_security_id"] == "99002"
    assert infy["near_expiry"] == date(2026, 7, 31)


def test_parse_all_expired_yields_empty():
    """When all FUTSTK rows for a symbol are expired, that symbol is excluded."""
    expired1 = _row("NSE", "FUTSTK", "99001", "1594", "INFY", "300", "2020-01-30")
    expired2 = _row("NSE", "FUTSTK", "99002", "1594", "INFY", "300", "2021-02-25")
    csv_text = "\n".join([_HEADER, expired1, expired2])

    today = date(2026, 6, 20)
    universe = eu.parse_stock_fno_universe(csv_text, today=today)
    assert universe == []


def test_parse_missing_underlying_security_id_skipped(caplog):
    """Rows with blank UNDERLYING_SECURITY_ID must be skipped with a warning.

    Fixes the QA residual: previously these rows passed through as underlying_security_id=None,
    producing a null UnderlyingScrip that would break the option-chain API call.
    """
    import logging

    # A FUTSTK row with no u_sec_id — should be warned + skipped.
    no_u_sec = _row("NSE", "FUTSTK", "88001", "", "WIPRO", "100", "2026-07-31")
    # A normal valid row.
    valid = _row("NSE", "FUTSTK", "88002", "3456", "WIPRO2", "200", "2026-07-31")
    csv_text = "\n".join([_HEADER, no_u_sec, valid])

    today = date(2026, 6, 20)
    with caplog.at_level(logging.WARNING, logger="dhan.fno_equity_universe"):
        universe = eu.parse_stock_fno_universe(csv_text, today=today)

    syms = [u["underlying_symbol"] for u in universe]
    # WIPRO must be excluded (no u_sec_id), WIPRO2 included.
    assert "WIPRO" not in syms
    assert "WIPRO2" in syms
    # A warning must have been emitted for the skipped row.
    assert any("WIPRO" in r.message and "UNDERLYING_SECURITY_ID" in r.message for r in caplog.records)
