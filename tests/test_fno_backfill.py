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
    # 09:14 just before open, 15:41 just after the post-CAS close → closed;
    # 15:31 is now INSIDE the guarded window (stock F&O trades to ~15:40).
    assert fb.is_market_hours(datetime(2026, 6, 15, 9, 14, tzinfo=_IST)) is False
    assert fb.is_market_hours(datetime(2026, 6, 15, 15, 31, tzinfo=_IST)) is True
    assert fb.is_market_hours(datetime(2026, 6, 15, 15, 41, tzinfo=_IST)) is False


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
    # Exact open 09:15 IST and exact post-CAS close 15:40 IST are INCLUSIVE.
    assert fb.is_market_hours(datetime(2026, 6, 15, 9, 15, tzinfo=_IST)) is True
    assert fb.is_market_hours(datetime(2026, 6, 15, 15, 40, tzinfo=_IST)) is True


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


# ── index history parsing ────────────────────────────────────────────────────────
def test_parse_index_history_basic():
    raw = {
        "data": {
            "timestamp": [1718000000, 1718086400],
            "open": [23000, 23100], "high": [23200, 23250],
            "low": [22950, 23050], "close": [23150, 23200],
            "volume": [500, 600],
        }
    }
    rows = fb.parse_index_history(raw, "13", "NIFTY", "1d")
    assert len(rows) == 2
    r = rows[0]
    assert r["security_id"] == "13"
    assert r["symbol"] == "NIFTY"
    assert r["timeframe"] == "1d"
    assert r["open"] == 23000.0
    assert r["high"] == 23200.0
    assert r["low"] == 22950.0
    assert r["close"] == 23150.0
    assert r["volume"] == 500
    assert r["time"].tzinfo is timezone.utc
    # realized_vol_20d must NOT be present (derived later by fno_derived)
    assert "realized_vol_20d" not in r


def test_parse_index_history_without_volume():
    """Missing volume key → defaults to 0 (same as parse_futures_history)."""
    raw = {
        "data": {
            "timestamp": [1718000000],
            "open": [21.5], "high": [22.0], "low": [21.0], "close": [21.8],
        }
    }
    rows = fb.parse_index_history(raw, "21", "INDIAVIX", "1d")
    assert len(rows) == 1
    assert rows[0]["volume"] == 0


def test_parse_index_history_empty():
    assert fb.parse_index_history({"data": {"timestamp": []}}, "13", "NIFTY") == []
    assert fb.parse_index_history({}, "13", "NIFTY") == []


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


# ── option chain parsing ─────────────────────────────────────────────────────────
def _full_chain(spot=23412.0):
    """A two-strike chain with all optional fields filled."""
    return {
        "data": {
            "last_price": spot,
            "oc": {
                "23400.000000": {
                    "ce": {
                        "security_id": "CE_SEC_1",
                        "last_price": 120.5,
                        "previous_close_price": 115.0,
                        "volume": 10000,
                        "oi": 50000,
                        "previous_oi": 48000,
                        "previous_volume": 9500,
                        "top_bid_price": 120.0,
                        "top_ask_price": 121.0,
                        "top_bid_quantity": 75,
                        "top_ask_quantity": 100,
                        "implied_volatility": 12.0,
                        "greeks": {"delta": 0.5, "theta": -5.2, "gamma": 0.001, "vega": 12.3},
                    },
                    "pe": {
                        "security_id": "PE_SEC_1",
                        "last_price": 85.0,
                        "previous_close_price": 90.0,
                        "volume": 8000,
                        "oi": 45000,
                        "previous_oi": 43000,
                        "previous_volume": 7500,
                        "top_bid_price": 84.5,
                        "top_ask_price": 85.5,
                        "top_bid_quantity": 50,
                        "top_ask_quantity": 80,
                        "implied_volatility": 13.0,
                        "greeks": {"delta": -0.5, "theta": -4.8, "gamma": 0.001, "vega": 11.9},
                    },
                },
                "23450.000000": {
                    "ce": {
                        "security_id": "CE_SEC_2",
                        "last_price": 90.0,
                        "previous_close_price": 88.0,
                        "volume": 5000,
                        "oi": 30000,
                        "previous_oi": 28000,
                        "previous_volume": 4800,
                        "top_bid_price": 89.5,
                        "top_ask_price": 90.5,
                        "top_bid_quantity": 40,
                        "top_ask_quantity": 60,
                        "implied_volatility": 11.0,
                        "greeks": {"delta": 0.45, "theta": -4.5, "gamma": 0.0009, "vega": 10.5},
                    },
                    "pe": {
                        "security_id": "PE_SEC_2",
                        "last_price": 110.0,
                        "previous_close_price": 112.0,
                        "volume": 6000,
                        "oi": 35000,
                        "previous_oi": 33000,
                        "previous_volume": 5500,
                        "top_bid_price": 109.5,
                        "top_ask_price": 110.5,
                        "top_bid_quantity": 55,
                        "top_ask_quantity": 70,
                        "implied_volatility": 12.5,
                        "greeks": {"delta": -0.55, "theta": -5.1, "gamma": 0.0009, "vega": 11.2},
                    },
                },
            },
        }
    }


def test_parse_option_chain_all_rows_and_fields():
    """Two strikes × two sides = four rows; all fields captured; raw present."""
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    rows = fb.parse_option_chain(
        _full_chain(23412.0), 13, "IDX_I", date(2026, 6, 26), now=now
    )
    assert len(rows) == 4

    # Every row must have the key fields
    for r in rows:
        assert r["underlying_scrip"] == 13
        assert r["underlying_seg"] == "IDX_I"
        assert r["expiry_date"] == date(2026, 6, 26)
        assert r["option_type"] in ("CE", "PE")
        assert r["snapshot_time"].tzinfo is timezone.utc
        assert r["spot"] == pytest.approx(23412.0)
        assert r["raw"] is not None and isinstance(r["raw"], dict)

    # Check the 23400 CE row specifically
    ce_row = next(r for r in rows if r["strike"] == 23400.0 and r["option_type"] == "CE")
    assert ce_row["security_id"] == "CE_SEC_1"
    assert ce_row["ltp"] == pytest.approx(120.5)
    assert ce_row["prev_close"] == pytest.approx(115.0)
    assert ce_row["volume"] == 10000
    assert ce_row["oi"] == 50000
    assert ce_row["prev_oi"] == 48000
    assert ce_row["prev_volume"] == 9500
    assert ce_row["top_bid_price"] == pytest.approx(120.0)
    assert ce_row["top_ask_price"] == pytest.approx(121.0)
    assert ce_row["top_bid_qty"] == 75
    assert ce_row["top_ask_qty"] == 100
    # IV stored RAW (percent, not normalised)
    assert ce_row["iv"] == pytest.approx(12.0)
    assert ce_row["delta"] == pytest.approx(0.5)
    assert ce_row["theta"] == pytest.approx(-5.2)
    assert ce_row["gamma"] == pytest.approx(0.001)
    assert ce_row["vega"] == pytest.approx(12.3)


def test_parse_option_chain_iv_is_raw_not_normalised():
    """IV must be stored as raw percent (12.0), NOT as a fraction (0.12)."""
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    rows = fb.parse_option_chain(
        _full_chain(23412.0), 13, "IDX_I", date(2026, 6, 26), now=now
    )
    # All IVs must be > 1 (percent scale), not < 0.2 (fraction scale)
    for r in rows:
        if r["iv"] is not None:
            assert r["iv"] > 1.0, f"IV {r['iv']} looks normalised (should be percent)"


def test_parse_option_chain_missing_fields_become_none():
    """A partial node with only a few fields — missing ones → None, no exception."""
    sparse_chain = {
        "data": {
            "last_price": 23400.0,
            "oc": {
                "23400.000000": {
                    "ce": {"last_price": 120.0},   # only ltp; everything else absent
                }
            },
        }
    }
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    rows = fb.parse_option_chain(sparse_chain, 13, "IDX_I", date(2026, 6, 26), now=now)
    assert len(rows) == 1
    r = rows[0]
    assert r["option_type"] == "CE"
    assert r["ltp"] == pytest.approx(120.0)
    assert r["prev_close"] is None
    assert r["volume"] is None
    assert r["oi"] is None
    assert r["iv"] is None
    assert r["delta"] is None


def test_parse_option_chain_error_envelope_returns_empty():
    """If data is null or missing (error envelope from Dhan), return []."""
    assert fb.parse_option_chain({"status": "error", "data": None}, 13, "IDX_I", date(2026, 6, 26)) == []
    assert fb.parse_option_chain({}, 13, "IDX_I", date(2026, 6, 26)) == []
    assert fb.parse_option_chain({"data": {}}, 13, "IDX_I", date(2026, 6, 26)) == []


def test_parse_option_chain_only_ce_side_present():
    """If only CE is in the node (pe key absent), emit one row per strike."""
    chain = {
        "data": {
            "last_price": 23400.0,
            "oc": {
                "23400.000000": {
                    "ce": {"last_price": 120.0, "implied_volatility": 12.0},
                    # pe absent
                }
            },
        }
    }
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    rows = fb.parse_option_chain(chain, 13, "IDX_I", date(2026, 6, 26), now=now)
    assert len(rows) == 1
    assert rows[0]["option_type"] == "CE"


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
        self.calls.append(("chain", scrip, expiry, underlying_seg))
        return _full_chain(23412.0)

    async def get_fno_expiry_list(self, scrip, underlying_seg="IDX_I"):
        self.calls.append(("expiry", scrip, underlying_seg))
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


def test_backfill_futures_bars_stock_instrument_futstk(monkeypatch):
    """instrument='FUTSTK' must be forwarded to the charts endpoint (stock futures)."""
    captured = {}
    monkeypatch.setattr(fb, "_upsert_futures_bars", _capture(captured))
    client = _FakeClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    n = asyncio.run(fb.backfill_futures_bars(
        client, "RELIANCE-FUT", "61001", "2026-06-01", "2026-06-18",
        instrument="FUTSTK", now=now,
    ))
    assert n == 1
    assert client.calls[0][1]["instrument"] == "FUTSTK"
    assert client.calls[0][1]["exchange_segment"] == "NSE_FNO"
    assert captured["rows"][0]["symbol"] == "RELIANCE-FUT"


def test_extract_atm_iv_nearest_fallback_for_stock_strikes():
    """With nearest=True (stock options), an absent computed-ATM strike snaps to the
    chain strike nearest spot instead of returning None. Default (False) is strict."""
    # Stock chain with a 25-step grid; spot 2912 → computed step-50 ATM (2900) absent
    # only if the chain lacks it. Use a chain whose strikes are off the step-50 grid.
    chain = {
        "data": {
            "last_price": 2912.0,
            "oc": {
                "2880.000000": {"ce": {"implied_volatility": 30.0}, "pe": {"implied_volatility": 31.0}},
                "2910.000000": {"ce": {"implied_volatility": 28.0}, "pe": {"implied_volatility": 29.0}},
                "2940.000000": {"ce": {"implied_volatility": 27.0}, "pe": {"implied_volatility": 28.0}},
            },
        }
    }
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    # Strict (default): computed ATM 2900 (round 2912/50*50) is absent → None.
    assert fb.extract_atm_iv(chain, "RELIANCE", date(2026, 6, 26), "monthly", 50, now=now) is None
    # nearest=True: snap to 2910 (nearest to spot 2912).
    row = fb.extract_atm_iv(chain, "RELIANCE", date(2026, 6, 26), "monthly", 50, now=now, nearest=True)
    assert row is not None
    assert row["atm_strike"] == 2910
    assert row["call_iv"] == pytest.approx(0.28)


def test_backfill_futures_bars_refuses_in_market_hours(monkeypatch):
    monkeypatch.setattr(fb, "_upsert_futures_bars", lambda rows: 1 / 0)  # must never run
    client = _FakeClient()
    now = datetime(2026, 6, 15, 11, 0, tzinfo=_IST)
    with pytest.raises(RuntimeError, match="market hours"):
        asyncio.run(fb.backfill_futures_bars(client, "NIFTY", "54321", "a", "b", now=now))
    assert client.calls == []   # no live call was made


def test_backfill_index_bars_off_hours_calls_and_upserts(monkeypatch):
    """backfill_index_bars: off-hours → calls get_daily_historical with IDX_I/INDEX,
    parses via parse_index_history, upserts via _upsert_index_bars."""
    captured = {}
    monkeypatch.setattr(fb, "_upsert_index_bars", _capture(captured))
    client = _FakeClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    n = asyncio.run(
        fb.backfill_index_bars(client, "13", "NIFTY", "2024-06-01", "2026-06-18", now=now)
    )
    assert n == 1
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call[0] == "hist"
    assert call[1]["exchange_segment"] == "IDX_I"
    assert call[1]["instrument"] == "INDEX"
    assert call[1]["security_id"] == "13"
    row = captured["rows"][0]
    assert row["security_id"] == "13"
    assert row["symbol"] == "NIFTY"
    assert row["timeframe"] == "1d"
    assert row["time"].tzinfo is timezone.utc


def test_backfill_index_bars_refuses_in_market_hours(monkeypatch):
    monkeypatch.setattr(fb, "_upsert_index_bars", lambda rows: 1 / 0)  # must never run
    client = _FakeClient()
    now = datetime(2026, 6, 15, 11, 0, tzinfo=_IST)
    with pytest.raises(RuntimeError, match="market hours"):
        asyncio.run(fb.backfill_index_bars(client, "13", "NIFTY", "a", "b", now=now))
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


def test_snapshot_option_chain_off_hours_full_capture(monkeypatch):
    """snapshot_option_chain: off-hours → full chain upserted + ATM projected."""
    chain_captured = {}
    atm_captured = {}
    monkeypatch.setattr(fb, "_upsert_option_chain_snapshot", _capture(chain_captured))
    monkeypatch.setattr(fb, "_upsert_atm_iv", _capture(atm_captured))

    client = _FakeClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    result = asyncio.run(
        fb.snapshot_option_chain(
            client, "NIFTY",
            expiry_date=date(2026, 6, 26),
            expiry_type="weekly",
            now=now,
        )
    )
    # Two strikes × two sides = 4 chain rows
    assert result["chain_rows"] == 4
    assert result["atm"] == 1

    # chain rows contain all expected fields
    assert len(chain_captured["rows"]) == 4
    for r in chain_captured["rows"]:
        assert r["underlying_scrip"] == 13
        assert r["expiry_date"] == date(2026, 6, 26)
        assert r["raw"] is not None

    # ATM row projected
    assert len(atm_captured["rows"]) == 1
    assert atm_captured["rows"][0]["atm_strike"] == 23400

    # Client called get_fno_option_chain (NOT get_fno_expiry_list — expiry_date was given)
    assert any(c[0] == "chain" for c in client.calls)
    assert not any(c[0] == "expiry" for c in client.calls)


def test_snapshot_option_chain_picks_nearest_expiry_when_none(monkeypatch):
    """If expiry_date is None, snapshot_option_chain calls get_fno_expiry_list
    and picks the MINIMUM FUTURE expiry (past expiries are filtered out).
    Fake client returns ["2026-06-26", "2026-06-04", "2026-07-31"]; now=2026-06-20
    so 2026-06-04 is in the past → nearest future = 2026-06-26."""
    chain_captured = {}
    atm_captured = {}
    monkeypatch.setattr(fb, "_upsert_option_chain_snapshot", _capture(chain_captured))
    monkeypatch.setattr(fb, "_upsert_atm_iv", _capture(atm_captured))

    client = _FakeClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    result = asyncio.run(fb.snapshot_option_chain(client, "NIFTY", now=now))

    expiry_call = next(c for c in client.calls if c[0] == "expiry")
    chain_call = next(c for c in client.calls if c[0] == "chain")
    assert expiry_call is not None
    # 2026-06-04 is in the past relative to now=2026-06-20; nearest FUTURE = 2026-06-26
    assert chain_call[2] == "2026-06-26"
    assert result["chain_rows"] == 4


def test_snapshot_option_chain_refuses_in_market_hours(monkeypatch):
    monkeypatch.setattr(fb, "_upsert_option_chain_snapshot", lambda rows: 1 / 0)
    client = _FakeClient()
    now = datetime(2026, 6, 15, 11, 0, tzinfo=_IST)
    with pytest.raises(RuntimeError, match="market hours"):
        asyncio.run(
            fb.snapshot_option_chain(client, "NIFTY", expiry_date=date(2026, 6, 26), now=now)
        )
    assert client.calls == []


# ── new QA-driven tests ──────────────────────────────────────────────────────────


def test_snapshot_option_chain_picks_nearest_FUTURE_expiry_not_past(monkeypatch):
    """Nearest-expiry auto-pick must exclude past dates.

    Expiry list mixes a past date (2026-06-10), today (2026-06-20, boundary
    included), and future dates (2026-06-26, 2026-07-31).  now = 2026-06-20 so
    'today' is 2026-06-20.  Dates >= today = [2026-06-20, 2026-06-26, 2026-07-31];
    nearest = 2026-06-20.  The chain call must use 2026-06-20, NOT 2026-06-10.
    """
    chain_captured = {}
    atm_captured = {}
    monkeypatch.setattr(fb, "_upsert_option_chain_snapshot", _capture(chain_captured))
    monkeypatch.setattr(fb, "_upsert_atm_iv", _capture(atm_captured))

    class _FakeClientMixed(_FakeClient):
        async def get_fno_expiry_list(self, scrip, underlying_seg="IDX_I"):
            self.calls.append(("expiry", scrip, underlying_seg))
            # deliberately has a past date first so naive min() would pick it
            return {"data": ["2026-06-10", "2026-06-20", "2026-06-26", "2026-07-31"]}

    client = _FakeClientMixed()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    result = asyncio.run(fb.snapshot_option_chain(client, "NIFTY", now=now))

    chain_call = next(c for c in client.calls if c[0] == "chain")
    # 2026-06-10 is in the past; nearest future (>= today 2026-06-20) = 2026-06-20
    assert chain_call[2] == "2026-06-20"
    assert result["chain_rows"] == 4


def test_snapshot_option_chain_all_past_expiries_returns_empty(monkeypatch):
    """If ALL expiries are in the past, return {"chain_rows": 0, "atm": 0}
    without calling get_fno_option_chain."""
    monkeypatch.setattr(fb, "_upsert_option_chain_snapshot", lambda rows: 1 / 0)
    monkeypatch.setattr(fb, "_upsert_atm_iv", lambda rows: 1 / 0)

    class _FakeClientAllPast(_FakeClient):
        async def get_fno_expiry_list(self, scrip, underlying_seg="IDX_I"):
            self.calls.append(("expiry", scrip, underlying_seg))
            return {"data": ["2026-06-01", "2026-06-10", "2026-06-15"]}

    client = _FakeClientAllPast()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    result = asyncio.run(fb.snapshot_option_chain(client, "NIFTY", now=now))

    assert result == {"chain_rows": 0, "atm": 0}
    # get_fno_option_chain must NOT have been called
    assert not any(c[0] == "chain" for c in client.calls)


def test_parse_option_chain_only_pe_node_present():
    """A node that has only 'pe' (no 'ce' key) emits exactly one PE row."""
    chain = {
        "data": {
            "last_price": 23400.0,
            "oc": {
                "23400.000000": {
                    "pe": {"last_price": 85.0, "implied_volatility": 13.0},
                    # ce key absent
                }
            },
        }
    }
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    rows = fb.parse_option_chain(chain, 13, "IDX_I", date(2026, 6, 26), now=now)
    assert len(rows) == 1
    assert rows[0]["option_type"] == "PE"
    assert rows[0]["ltp"] == pytest.approx(85.0)
    assert rows[0]["iv"] == pytest.approx(13.0)


def test_parse_option_chain_non_dict_side_is_skipped():
    """If a side value (ce or pe) is a non-dict (e.g. a string), it must be
    silently skipped — no crash, no partial row emitted."""
    chain = {
        "data": {
            "last_price": 23400.0,
            "oc": {
                "23400.000000": {
                    "ce": "bad",          # non-dict CE — must be skipped
                    "pe": {"last_price": 85.0},  # valid PE — must produce one row
                }
            },
        }
    }
    now = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)
    rows = fb.parse_option_chain(chain, 13, "IDX_I", date(2026, 6, 26), now=now)
    assert len(rows) == 1
    assert rows[0]["option_type"] == "PE"


def test_parse_index_history_skips_bar_with_none_close():
    """A bar where close is None or '' must be skipped entirely — no crash,
    row count is reduced by one for each bad bar."""
    raw = {
        "data": {
            "timestamp": [1718000000, 1718086400, 1718172800],
            "open":  [23000, 23100, 23200],
            "high":  [23200, 23250, 23300],
            "low":   [22950, 23050, 23150],
            "close": [23150, None, 23250],   # middle bar has None close
            "volume": [500, 600, 700],
        }
    }
    rows = fb.parse_index_history(raw, "13", "NIFTY", "1d")
    # The second bar (None close) must be skipped → only 2 rows
    assert len(rows) == 2
    closes = [r["close"] for r in rows]
    assert 23150.0 in closes
    assert 23250.0 in closes


# ── auth: token via the token manager (NOT the static .env token) ───────────────
# Regression for a DH-901 caused by _amain building DhanClient from the STATIC
# cfg.dhan_access_token (which expires). The F&O path must source the token from
# the token manager: read_current_token() first, MasterTokenManager().load_or_generate()
# as fallback — mirroring apps/trader.py.

def test_resolve_access_token_prefers_cache():
    """When the live cache has a valid token, resolve_access_token returns it and
    NEVER constructs a MasterTokenManager (no PIN/TOTP generation)."""
    from unittest.mock import MagicMock, patch

    mock_read = MagicMock(return_value="cached-tok")
    mock_mgr_cls = MagicMock()  # if constructed at all, the test fails below
    with patch("core.token_manager.read_current_token", mock_read), \
         patch("core.token_manager.MasterTokenManager", mock_mgr_cls):
        tok = asyncio.run(fb.resolve_access_token())
    assert tok == "cached-tok"
    mock_read.assert_called_once()
    mock_mgr_cls.assert_not_called()


def test_resolve_access_token_falls_back_to_generate():
    """When the cache is empty/expired, resolve_access_token falls back to
    MasterTokenManager().load_or_generate() (PIN + TOTP)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_read = MagicMock(return_value=None)
    mock_mgr = MagicMock()
    mock_mgr.load_or_generate = AsyncMock(return_value="fresh-tok")
    mock_mgr_cls = MagicMock(return_value=mock_mgr)
    with patch("core.token_manager.read_current_token", mock_read), \
         patch("core.token_manager.MasterTokenManager", mock_mgr_cls):
        tok = asyncio.run(fb.resolve_access_token())
    assert tok == "fresh-tok"
    mock_read.assert_called_once()
    mock_mgr_cls.assert_called_once()
    mock_mgr.load_or_generate.assert_awaited_once()


def test_amain_uses_token_manager_not_static_env_token():
    """_amain must build DhanClient with the token-manager token, NOT
    cfg.dhan_access_token. Asserts resolve_access_token is awaited and the static
    env token is never passed to DhanClient."""
    import argparse
    import sys
    from unittest.mock import AsyncMock, MagicMock, patch

    args = argparse.Namespace(
        symbol="NIFTY", futures=False, security_id=None, from_date=None,
        to_date=None, index=False, expiry_calendar=True, chain=False,
        atm_iv=False, expiry=None,
    )

    mock_cfg = MagicMock()
    mock_cfg.db_url = "postgresql://fake/db"
    mock_cfg.dhan_client_id = "CLIENT1"
    mock_cfg.dhan_access_token = "STATIC-ENV-TOKEN-EXPIRED"

    mock_client_cls = MagicMock()
    mock_client_instance = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    mock_resolve = AsyncMock(return_value="MANAGED-TOKEN")
    mock_build_calendar = AsyncMock(return_value=7)

    with patch.dict(sys.modules, {
        "config": MagicMock(get_config=MagicMock(return_value=mock_cfg)),
        "db": MagicMock(init_db=MagicMock()),
        "core.client": MagicMock(DhanClient=mock_client_cls),
    }), \
        patch.object(fb, "resolve_access_token", mock_resolve), \
        patch.object(fb, "build_expiry_calendar", mock_build_calendar):
        asyncio.run(fb._amain(args))

    mock_resolve.assert_awaited_once()
    # DhanClient built with the MANAGED token, never the static env token.
    pos, kw = mock_client_cls.call_args
    passed = list(pos) + list(kw.values())
    assert "MANAGED-TOKEN" in passed
    assert "STATIC-ENV-TOKEN-EXPIRED" not in passed


def test_amain_does_not_fall_back_to_static_token_when_resolve_empty():
    """If resolve_access_token returns an empty string (cache miss + generation
    yielding nothing), _amain must NOT silently fall back to cfg.dhan_access_token.
    Catches a `resolved or cfg.dhan_access_token` regression: DhanClient must be
    built with the empty resolved value (so the failure is visible/propagates),
    never with the stale static env token.
    """
    import argparse
    import sys
    from unittest.mock import AsyncMock, MagicMock, patch

    args = argparse.Namespace(
        symbol="NIFTY", futures=False, security_id=None, from_date=None,
        to_date=None, index=False, expiry_calendar=True, chain=False,
        atm_iv=False, expiry=None,
    )

    mock_cfg = MagicMock()
    mock_cfg.db_url = "postgresql://fake/db"
    mock_cfg.dhan_client_id = "CLIENT1"
    mock_cfg.dhan_access_token = "STATIC-ENV-TOKEN-EXPIRED"

    mock_client_cls = MagicMock()
    mock_client_instance = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    mock_resolve = AsyncMock(return_value="")          # cache miss → empty token
    mock_build_calendar = AsyncMock(return_value=7)

    with patch.dict(sys.modules, {
        "config": MagicMock(get_config=MagicMock(return_value=mock_cfg)),
        "db": MagicMock(init_db=MagicMock()),
        "core.client": MagicMock(DhanClient=mock_client_cls),
    }), \
        patch.object(fb, "resolve_access_token", mock_resolve), \
        patch.object(fb, "build_expiry_calendar", mock_build_calendar):
        asyncio.run(fb._amain(args))

    mock_resolve.assert_awaited_once()
    # The static env token must NEVER be passed to DhanClient — no `or` fallback.
    pos, kw = mock_client_cls.call_args
    passed = list(pos) + list(kw.values())
    assert "STATIC-ENV-TOKEN-EXPIRED" not in passed
    assert "" in passed   # the (empty) resolved value is what was used


def test_amain_propagates_when_resolve_raises():
    """If resolve_access_token raises (no cache + generation fails), _amain must
    propagate the error and NEVER fall back to cfg.dhan_access_token (DhanClient
    must not be constructed at all)."""
    import argparse
    import sys
    from unittest.mock import AsyncMock, MagicMock, patch

    args = argparse.Namespace(
        symbol="NIFTY", futures=False, security_id=None, from_date=None,
        to_date=None, index=False, expiry_calendar=True, chain=False,
        atm_iv=False, expiry=None,
    )

    mock_cfg = MagicMock()
    mock_cfg.db_url = "postgresql://fake/db"
    mock_cfg.dhan_client_id = "CLIENT1"
    mock_cfg.dhan_access_token = "STATIC-ENV-TOKEN-EXPIRED"

    mock_client_cls = MagicMock()
    mock_resolve = AsyncMock(side_effect=RuntimeError("no creds, generation failed"))

    with patch.dict(sys.modules, {
        "config": MagicMock(get_config=MagicMock(return_value=mock_cfg)),
        "db": MagicMock(init_db=MagicMock()),
        "core.client": MagicMock(DhanClient=mock_client_cls),
    }), \
        patch.object(fb, "resolve_access_token", mock_resolve):
        with pytest.raises(RuntimeError, match="generation failed"):
            asyncio.run(fb._amain(args))

    mock_resolve.assert_awaited_once()
    mock_client_cls.assert_not_called()   # never fell back to the static token
