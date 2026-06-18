"""Tests for core/fno_backfill.py — pure helpers + off-hours-guarded orchestration.

No real Dhan creds, no database: the Dhan client is a recording fake and the DB
upsert helpers are monkeypatched to capture rows. Verifies (per the handoff
invariants) that live fetches refuse to run during market hours.
"""
import asyncio
from datetime import date, datetime, timezone

import pytest

from core import fno_backfill as fb

_IST = fb._IST


# ── pure helpers ─────────────────────────────────────────────────────────────────
def test_nifty_atm_strike_rounds_to_step():
    # Standard rounds — below/above mid-point
    assert fb.nifty_atm_strike(23412, 50) == 23400
    assert fb.nifty_atm_strike(23426, 50) == 23450
    # Exactly on a half-step: round-half-UP convention → rounds UP (23450, not 23400)
    assert fb.nifty_atm_strike(23425, 50) == 23450


def test_normalize_iv_percent_vs_fraction():
    # Dhan returns IV in percent; divide by 100 unconditionally.
    assert fb._normalize_iv(13.5) == pytest.approx(0.135)       # 13.5% → 0.135
    assert fb._normalize_iv(0.135) == pytest.approx(0.00135)    # 0.135% → 0.00135
    assert fb._normalize_iv(None) is None
    assert fb._normalize_iv(0) is None
    assert fb._normalize_iv("oops") is None


# ── market-hours guard ───────────────────────────────────────────────────────────
def test_is_market_hours_weekday_inside():
    # Mon 2026-06-15 11:00 IST → open
    assert fb.is_market_hours(datetime(2026, 6, 15, 11, 0, tzinfo=_IST)) is True
    # 09:14 just before open, 15:31 just after close → closed
    assert fb.is_market_hours(datetime(2026, 6, 15, 9, 14, tzinfo=_IST)) is False
    assert fb.is_market_hours(datetime(2026, 6, 15, 15, 31, tzinfo=_IST)) is False


def test_is_market_hours_weekend():
    # Sat 2026-06-20 11:00 IST → closed even though it's "trading time"
    assert fb.is_market_hours(datetime(2026, 6, 20, 11, 0, tzinfo=_IST)) is False


def test_assert_off_hours_raises_in_hours():
    with pytest.raises(RuntimeError, match="market hours"):
        fb._assert_off_hours("x", now=datetime(2026, 6, 15, 11, 0, tzinfo=_IST))
    # off-hours → no raise
    fb._assert_off_hours("x", now=datetime(2026, 6, 15, 18, 0, tzinfo=_IST))


def test_is_market_hours_naive_datetime():
    # A naive datetime (no tzinfo) is assumed to be IST wall-clock by the module.
    # Mon 2026-06-15 11:00 (naive, treated as IST) → inside market hours.
    assert fb.is_market_hours(datetime(2026, 6, 15, 11, 0)) is True
    # Mon 2026-06-15 18:00 (naive, treated as IST) → after close.
    assert fb.is_market_hours(datetime(2026, 6, 15, 18, 0)) is False


def test_is_market_hours_exact_boundaries_inclusive():
    # Exact open 09:15 IST and exact close 15:30 IST are INCLUSIVE.
    assert fb.is_market_hours(datetime(2026, 6, 15, 9, 15, tzinfo=_IST)) is True
    assert fb.is_market_hours(datetime(2026, 6, 15, 15, 30, tzinfo=_IST)) is True


# ── futures history parsing ──────────────────────────────────────────────────────
def test_parse_futures_history_with_oi():
    raw = {
        "data": {
            "timestamp": [1718000000, 1718086400],
            "open": [23000, 23100], "high": [23200, 23250],
            "low": [22950, 23050], "close": [23150, 23200],
            "volume": [1000, 1200], "open_interest": [50000, 51000],
        }
    }
    rows = fb.parse_futures_history(raw, "NIFTY", "1d", date(2026, 6, 26))
    assert len(rows) == 2
    assert rows[0]["symbol"] == "NIFTY" and rows[0]["timeframe"] == "1d"
    assert rows[0]["open_interest"] == 50000
    assert rows[0]["expiry_date"] == date(2026, 6, 26)
    assert rows[0]["time"].tzinfo is timezone.utc


def test_parse_futures_history_empty_and_missing_optional_cols():
    assert fb.parse_futures_history({"data": {"timestamp": []}}, "NIFTY") == []
    raw = {"timestamp": [1718000000], "open": [1], "high": [2], "low": [0.5], "close": [1.5]}
    rows = fb.parse_futures_history(raw, "NIFTY")
    assert rows[0]["volume"] == 0 and rows[0]["open_interest"] is None


# ── ATM IV extraction ────────────────────────────────────────────────────────────
def _chain(spot=23412.0):
    return {
        "data": {
            "last_price": spot,
            "oc": {
                "23350.000000": {"ce": {"implied_volatility": 14.0}, "pe": {"implied_volatility": 15.0}},
                "23400.000000": {"ce": {"implied_volatility": 12.0}, "pe": {"implied_volatility": 13.0}},
                "23450.000000": {"ce": {"implied_volatility": 11.0}, "pe": {"implied_volatility": 12.0}},
            },
        }
    }


def test_extract_atm_iv_picks_atm_and_normalises():
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    row = fb.extract_atm_iv(_chain(23412.0), "NIFTY", date(2026, 6, 26), "weekly", 50, now=now)
    assert row["atm_strike"] == 23400          # round(23412/50)*50
    assert row["call_iv"] == pytest.approx(0.12)   # 12.0% → 0.12
    assert row["put_iv"] == pytest.approx(0.13)    # 13.0% → 0.13
    assert row["straddle_iv"] == pytest.approx(0.125)
    assert row["dte"] == 6                       # 26 - 20
    assert row["spot_ref"] == 23412.0
    assert row["implied_move"] is None           # derived later


def test_extract_atm_iv_missing_atm_node_returns_none():
    chain = {"data": {"last_price": 99999.0, "oc": {"23400.000000": {"ce": {}, "pe": {}}}}}
    assert fb.extract_atm_iv(chain, "NIFTY", date(2026, 6, 26)) is None
    assert fb.extract_atm_iv({"data": {"last_price": None, "oc": {}}}, "NIFTY", date(2026, 6, 26)) is None


def test_extract_atm_iv_single_leg_straddle_iv_is_none():
    """Chain ATM node has only ce.implied_volatility; pe is empty.
    call_iv should be a fraction, put_iv should be None, straddle_iv should be None."""
    chain = {
        "data": {
            "last_price": 23400.0,
            "oc": {
                "23400.000000": {
                    "ce": {"implied_volatility": 12.0},
                    "pe": {},  # no IV on the put leg
                },
            },
        }
    }
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    row = fb.extract_atm_iv(chain, "NIFTY", date(2026, 6, 26), "weekly", 50, now=now)
    assert row is not None
    assert row["call_iv"] == pytest.approx(0.12)   # 12.0% → fraction
    assert row["put_iv"] is None
    assert row["straddle_iv"] is None              # only one leg present → None


def test_extract_atm_iv_non_numeric_oc_key_ignored():
    """A non-numeric key ("N/A") in oc should be silently skipped; the valid
    23400 node should still be found when spot is 23412."""
    chain = {
        "data": {
            "last_price": 23412.0,
            "oc": {
                "N/A": {"ce": {"implied_volatility": 99.0}, "pe": {"implied_volatility": 99.0}},
                "23400.000000": {"ce": {"implied_volatility": 12.0}, "pe": {"implied_volatility": 13.0}},
            },
        }
    }
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    row = fb.extract_atm_iv(chain, "NIFTY", date(2026, 6, 26), "weekly", 50, now=now)
    assert row is not None
    assert row["atm_strike"] == 23400
    assert row["call_iv"] == pytest.approx(0.12)
    assert row["put_iv"] == pytest.approx(0.13)


def test_extract_atm_iv_negative_dte_returns_none():
    """expiry_date strictly before now.date() → dte < 0 → returns None."""
    # expiry 2026-06-10, now 2026-06-20 → dte = -10
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    row = fb.extract_atm_iv(_chain(23400.0), "NIFTY", date(2026, 6, 10), "weekly", 50, now=now)
    assert row is None


# ── India VIX CSV ────────────────────────────────────────────────────────────────
def test_parse_india_vix_csv_variants():
    csv_text = "Date,Open,High,Low,Close\n01-Jan-2026,13.1,14.2,12.9,13.5\n02-Jan-2026,13.5,13.8,13.0,13.2\n"
    rows = fb.parse_india_vix_csv(csv_text)
    assert len(rows) == 2
    assert rows[0]["close"] == 13.5 and rows[0]["high"] == 14.2 and rows[0]["low"] == 12.9
    # ISO date + only Close column
    rows2 = fb.parse_india_vix_csv("date,close\n2026-01-01,13.5\n")
    assert rows2[0]["close"] == 13.5 and rows2[0]["high"] is None


def test_parse_india_vix_csv_missing_required_column():
    with pytest.raises(ValueError):
        fb.parse_india_vix_csv("Date,Open\n01-Jan-2026,13.1\n")


def test_parse_india_vix_csv_time_is_00_00_utc():
    """Daily bar time must be the trading DATE at 00:00 UTC (not 18:30 UTC or midnight IST)."""
    csv_text = "Date,Open,High,Low,Close\n01-Jan-2026,13.1,14.2,12.9,13.5\n"
    rows = fb.parse_india_vix_csv(csv_text)
    assert len(rows) == 1
    assert rows[0]["time"] == datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def test_parse_india_vix_csv_blank_or_non_numeric_close_skipped():
    """Rows with blank or non-numeric Close values are skipped; valid rows still parsed."""
    csv_text = (
        "Date,Open,High,Low,Close\n"
        "01-Jan-2026,13.1,14.2,12.9,13.5\n"
        "02-Jan-2026,,,, \n"          # blank close
        "03-Jan-2026,13.0,13.5,12.8,N/A\n"  # non-numeric close
        "04-Jan-2026,13.2,13.7,12.9,13.3\n"
    )
    rows = fb.parse_india_vix_csv(csv_text)
    assert len(rows) == 2
    assert rows[0]["close"] == 13.5
    assert rows[1]["close"] == 13.3


# ── expiry classification ────────────────────────────────────────────────────────
def test_classify_expiry_monthly_is_last_in_month():
    expiries = [date(2026, 6, 4), date(2026, 6, 11), date(2026, 6, 26), date(2026, 7, 31)]
    assert fb.classify_expiry(date(2026, 6, 4), expiries) == "weekly"
    assert fb.classify_expiry(date(2026, 6, 26), expiries) == "monthly"
    assert fb.classify_expiry(date(2026, 7, 31), expiries) == "monthly"


def _capture(store):
    """Return a fake upsert that records rows in `store` and returns the count."""
    def _fn(rows):
        store["rows"] = rows
        return len(rows)
    return _fn


# ── orchestration: fake client + captured upserts ────────────────────────────────
class _FakeClient:
    def __init__(self):
        self.calls = []

    async def get_daily_historical(self, **kw):
        self.calls.append(("hist", kw))
        return {"data": {"timestamp": [1718000000], "open": [1], "high": [2],
                         "low": [0.5], "close": [1.5], "volume": [10], "open_interest": [5]}}

    async def get_fno_option_chain(self, scrip, expiry, underlying_seg="IDX_I"):
        self.calls.append(("chain", scrip, expiry))
        return _chain(23412.0)

    async def get_fno_expiry_list(self, scrip, underlying_seg="IDX_I"):
        self.calls.append(("expiry", scrip))
        return {"data": ["2026-06-26", "2026-06-04", "2026-07-31"]}


def test_backfill_futures_bars_off_hours_calls_and_upserts(monkeypatch):
    captured = {}
    monkeypatch.setattr(fb, "_upsert_futures_bars", _capture(captured))
    client = _FakeClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    n = asyncio.run(fb.backfill_futures_bars(client, "NIFTY", "54321", "2026-06-01", "2026-06-18", now=now))
    assert n == 1
    assert client.calls[0][0] == "hist"
    assert client.calls[0][1]["exchange_segment"] == "NSE_FNO"
    assert captured["rows"][0]["symbol"] == "NIFTY"


def test_backfill_futures_bars_refuses_in_market_hours(monkeypatch):
    monkeypatch.setattr(fb, "_upsert_futures_bars", lambda rows: 1 / 0)  # must never run
    client = _FakeClient()
    now = datetime(2026, 6, 15, 11, 0, tzinfo=_IST)
    with pytest.raises(RuntimeError, match="market hours"):
        asyncio.run(fb.backfill_futures_bars(client, "NIFTY", "54321", "a", "b", now=now))
    assert client.calls == []   # no live call was made


def test_snapshot_atm_iv_off_hours(monkeypatch):
    captured = {}
    monkeypatch.setattr(fb, "_upsert_atm_iv", _capture(captured))
    client = _FakeClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    n = asyncio.run(fb.snapshot_atm_iv(client, "NIFTY", date(2026, 6, 26), expiry_type="weekly", now=now))
    assert n == 1 and captured["rows"][0]["atm_strike"] == 23400
    assert client.calls[0] == ("chain", 13, "2026-06-26")


def test_snapshot_atm_iv_refuses_in_market_hours():
    client = _FakeClient()
    with pytest.raises(RuntimeError, match="market hours"):
        asyncio.run(fb.snapshot_atm_iv(client, "NIFTY", date(2026, 6, 26),
                                       now=datetime(2026, 6, 15, 11, 0, tzinfo=_IST)))
    assert client.calls == []


def test_build_expiry_calendar_off_hours(monkeypatch):
    captured = {}
    monkeypatch.setattr(fb, "_upsert_expiry_calendar", _capture(captured))
    client = _FakeClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    n = asyncio.run(fb.build_expiry_calendar(client, "NIFTY", now=now))
    assert n == 3
    rows = {r["expiry_date"]: r["expiry_type"] for r in captured["rows"]}
    assert rows[date(2026, 6, 4)] == "weekly"
    assert rows[date(2026, 6, 26)] == "monthly"
    assert rows[date(2026, 7, 31)] == "monthly"


def test_build_expiry_calendar_refuses_in_market_hours():
    """build_expiry_calendar must refuse to run during market hours."""
    client = _FakeClient()
    now = datetime(2026, 6, 15, 11, 0, tzinfo=_IST)
    with pytest.raises(RuntimeError, match="market hours"):
        asyncio.run(fb.build_expiry_calendar(client, "NIFTY", now=now))
    assert client.calls == []


def test_ingest_india_vix_text(monkeypatch):
    captured = {}
    monkeypatch.setattr(fb, "_upsert_india_vix", _capture(captured))
    n = fb.ingest_india_vix("Date,Close\n01-Jan-2026,13.5\n02-Jan-2026,13.2\n")
    assert n == 2 and len(captured["rows"]) == 2


def test_ingest_india_vix_from_file(monkeypatch, tmp_path):
    """ingest_india_vix should read from a file path (string), parse the CSV,
    and call _upsert_india_vix with the resulting rows."""
    csv_content = "Date,Open,High,Low,Close\n01-Jan-2026,13.1,14.2,12.9,13.5\n02-Jan-2026,13.5,13.8,13.0,13.2\n"
    tmp_file = tmp_path / "india_vix.csv"
    tmp_file.write_text(csv_content)

    captured = {}
    monkeypatch.setattr(fb, "_upsert_india_vix", _capture(captured))
    n = fb.ingest_india_vix(str(tmp_file))
    assert n == 2
    assert len(captured["rows"]) == 2
    assert captured["rows"][0]["close"] == 13.5
    assert captured["rows"][1]["close"] == 13.2
