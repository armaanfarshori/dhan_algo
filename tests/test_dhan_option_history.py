"""Tests for core/dhan_option_history.py — the Dhan rollingoption IV ingester.

NO network, NO database: the Dhan client is a recording fake whose ``_request``
returns canned rollingoption payloads, and the two DB upsert writers are
monkeypatched to capture rows. Pure parsers are exercised directly.

IST date-trap guard (memory ci-ist-date-trap): every datetime/now used is an
explicit IST-aware value — no ``date.today()`` / naive ``now()`` — so the suite
behaves identically on a UTC-clocked CI runner and the IST dev Mac.
"""
import asyncio
from datetime import date, datetime, timezone

import pytest

from core import dhan_option_history as oh

_IST = oh._IST


# ── helpers ────────────────────────────────────────────────────────────────────────
def _epoch_ist(y, m, d, hh, mm) -> int:
    """Epoch seconds for an IST wall-clock instant (what Dhan timestamps map to)."""
    return int(datetime(y, m, d, hh, mm, tzinfo=_IST).timestamp())


def _side_payload(ts, ivs, closes, ois=None, vols=None):
    side = {"iv": ivs, "close": closes}
    side["oi"] = ois if ois is not None else [None] * len(ts)
    side["volume"] = vols if vols is not None else [None] * len(ts)
    return side


# ── registry / index-agnostic resolution ───────────────────────────────────────────
def test_resolve_underlying_nifty_default():
    u = oh.resolve_underlying("NIFTY")
    assert u.symbol == "NIFTY"
    assert u.security_id == 13
    assert u.instrument == "OPTIDX"
    assert u.max_strikes == 10  # index supports ATM±10


def test_resolve_underlying_unknown_requires_security_id():
    with pytest.raises(ValueError, match="Unknown underlying"):
        oh.resolve_underlying("RELIANCE")
    # With an explicit id it resolves as a stock (OPTSTK, ATM±3 cap).
    u = oh.resolve_underlying("RELIANCE", security_id=2885)
    assert u.instrument == "OPTSTK"
    assert u.max_strikes == 3
    assert u.security_id == 2885


def test_resolve_underlying_overrides_win():
    u = oh.resolve_underlying("NIFTY", instrument="OPTIDX", strike_step=100, security_id=99)
    assert u.security_id == 99
    assert u.strike_step == 100


# ── 30-day pagination ───────────────────────────────────────────────────────────────
def test_date_windows_caps_at_30_days_no_gap_no_overlap():
    wins = oh.date_windows(date(2021, 1, 1), date(2021, 3, 31))
    # Each window ≤30 days inclusive.
    for f, t in wins:
        assert (t - f).days <= 29
    # Contiguous: next.from == prev.to + 1 day; full coverage of the range.
    assert wins[0][0] == date(2021, 1, 1)
    assert wins[-1][1] == date(2021, 3, 31)
    for (f0, t0), (f1, _t1) in zip(wins, wins[1:]):
        assert (f1 - t0).days == 1


def test_date_windows_single_short_range():
    wins = oh.date_windows(date(2026, 6, 1), date(2026, 6, 10))
    assert wins == [(date(2026, 6, 1), date(2026, 6, 10))]


def test_date_windows_rejects_reversed_range():
    with pytest.raises(ValueError, match="precedes"):
        oh.date_windows(date(2026, 6, 10), date(2026, 6, 1))


def test_five_year_pull_window_count():
    # The real 5yr NIFTY pull paginates into many ≤30-day windows.
    wins = oh.date_windows(date(2021, 1, 1), date(2026, 6, 18))
    assert len(wins) >= 60
    assert wins[0][0] == date(2021, 1, 1)
    assert wins[-1][1] == date(2026, 6, 18)


# ── ATM±n strike enumeration ────────────────────────────────────────────────────────
def test_strike_offsets_atm_first_then_outward():
    assert oh.strike_offsets(2, 10) == [0, -1, 1, -2, 2]
    assert oh.strike_offsets(0, 10) == [0]


def test_strike_offsets_clamped_to_cap():
    # Index cap 10, stock cap 3.
    assert len(oh.strike_offsets(50, 10)) == 21       # 0 plus ±1..10
    assert oh.strike_offsets(50, 3) == [0, -1, 1, -2, 2, -3, 3]


# ── side parsing → IV arrays ────────────────────────────────────────────────────────
def test_parse_rolling_side_basic():
    ts = [_epoch_ist(2026, 6, 15, 9, 20), _epoch_ist(2026, 6, 15, 15, 25)]
    raw = {"data": {"ce": _side_payload(ts, [12.0, 13.5], [110.0, 95.0], [100, 90], [5, 7]),
                    "timestamp": ts}}
    rows = oh.parse_rolling_side(raw, "ce")
    assert len(rows) == 2
    assert rows[0]["iv"] == 12.0           # RAW percent (not normalised here)
    assert rows[0]["close"] == 110.0
    assert rows[0]["oi"] == 100
    assert rows[1]["volume"] == 7
    assert rows[0]["time"].tzinfo is timezone.utc


def test_parse_rolling_side_timestamp_inside_side():
    ts = [_epoch_ist(2026, 6, 15, 11, 0)]
    raw = {"data": {"pe": {"iv": [20.0], "close": [88.0], "start_Time": ts}}}
    rows = oh.parse_rolling_side(raw, "pe")
    assert len(rows) == 1 and rows[0]["iv"] == 20.0


def test_parse_rolling_side_truncates_mismatched_arrays():
    ts = [_epoch_ist(2026, 6, 15, 9, 20), _epoch_ist(2026, 6, 15, 9, 25)]
    # close shorter than ts → truncate to shortest (1 row).
    raw = {"data": {"ce": {"iv": [12.0, 13.0], "close": [110.0]}, "timestamp": ts}}
    rows = oh.parse_rolling_side(raw, "ce")
    assert len(rows) == 1


def test_parse_rolling_side_missing_side_or_empty():
    assert oh.parse_rolling_side({"data": {"ce": {}}}, "ce") == []
    assert oh.parse_rolling_side({"data": {}}, "ce") == []
    assert oh.parse_rolling_side({}, "ce") == []
    assert oh.parse_rolling_side(None, "ce") == []


# ── ATM IV row collapse (one row/day, last bar wins, IV normalised) ─────────────────
def test_atm_iv_rows_one_per_day_last_bar_wins():
    # Two days, two bars each. Last bar of each day should win; IV normalised /100.
    ts = [
        _epoch_ist(2026, 6, 15, 9, 20), _epoch_ist(2026, 6, 15, 15, 25),
        _epoch_ist(2026, 6, 16, 9, 20), _epoch_ist(2026, 6, 16, 15, 25),
    ]
    ce = {"data": {"ce": _side_payload(ts, [12.0, 14.0, 11.0, 13.0],
                                       [100, 90, 80, 70]), "timestamp": ts}}
    pe = {"data": {"pe": _side_payload(ts, [16.0, 18.0, 15.0, 17.0],
                                       [100, 90, 80, 70]), "timestamp": ts}}
    rows = oh.atm_iv_rows_from_legs(
        ce, pe, symbol="NIFTY", expiry_date=date(2026, 6, 18),
        expiry_type="weekly", strike_step=50,
    )
    assert len(rows) == 2
    r0 = rows[0]
    assert r0["call_iv"] == pytest.approx(0.14)    # last CE bar of 6/15 = 14.0 → 0.14
    assert r0["put_iv"] == pytest.approx(0.18)     # last PE bar of 6/15 = 18.0 → 0.18
    assert r0["straddle_iv"] == pytest.approx(0.16)
    assert r0["symbol"] == "NIFTY"
    assert r0["expiry_type"] == "weekly"
    assert r0["dte"] == 3                            # 6/18 - 6/15
    assert rows[1]["dte"] == 2                       # 6/18 - 6/16


def test_atm_iv_rows_single_side_falls_back():
    ts = [_epoch_ist(2026, 6, 15, 15, 25)]
    ce = {"data": {"ce": _side_payload(ts, [12.0], [100])}, }
    ce["data"]["timestamp"] = ts
    pe = {}  # PE leg failed → tolerated; straddle_iv falls back to the present side
    rows = oh.atm_iv_rows_from_legs(
        ce, pe, symbol="NIFTY", expiry_date=date(2026, 6, 18),
        expiry_type="weekly", strike_step=50,
    )
    assert len(rows) == 1
    assert rows[0]["call_iv"] == pytest.approx(0.12)
    assert rows[0]["put_iv"] is None
    assert rows[0]["straddle_iv"] == pytest.approx(0.12)


def test_atm_iv_rows_skips_days_with_no_iv():
    ts = [_epoch_ist(2026, 6, 15, 15, 25)]
    ce = {"data": {"ce": _side_payload(ts, [None], [100]), "timestamp": ts}}
    pe = {"data": {"pe": _side_payload(ts, [None], [100]), "timestamp": ts}}
    rows = oh.atm_iv_rows_from_legs(
        ce, pe, symbol="NIFTY", expiry_date=date(2026, 6, 18),
        expiry_type="weekly", strike_step=50,
    )
    assert rows == []


# ── chain snapshot projection ───────────────────────────────────────────────────────
def test_chain_snapshot_rows_from_side():
    ts = [_epoch_ist(2026, 6, 15, 9, 20)]
    raw = {"data": {"ce": _side_payload(ts, [12.0], [110.0], [100], [5]), "timestamp": ts}}
    rows = oh.chain_snapshot_rows_from_side(
        raw, "ce", underlying_scrip=13, expiry_date=date(2026, 6, 18), strike=0.0
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["option_type"] == "CE"
    assert r["underlying_scrip"] == 13
    assert r["underlying_seg"] == "NSE_FNO"
    assert r["iv"] == 12.0            # raw percent (snapshot convention)
    assert r["ltp"] == 110.0
    assert r["raw"]["source"] == "rollingoption"


# ── expiry enumeration (offline fallback) ───────────────────────────────────────────
def test_enumerate_expiry_codes_weekly_thursdays():
    pairs = oh.enumerate_expiry_codes(date(2021, 1, 1), date(2021, 1, 31), "WEEK")
    dates = [d for _, d in pairs]
    # Jan 2021 Thursdays: 7, 14, 21, 28
    assert dates == [date(2021, 1, 7), date(2021, 1, 14),
                     date(2021, 1, 21), date(2021, 1, 28)]
    assert [c for c, _ in pairs] == [0, 1, 2, 3]   # 0-based codes


def test_enumerate_expiry_codes_monthly_last_thursday():
    pairs = oh.enumerate_expiry_codes(date(2021, 1, 1), date(2021, 1, 31), "MONTH")
    dates = [d for _, d in pairs]
    assert dates == [date(2021, 1, 28)]            # last Thursday of Jan


# ── build_request shape (matches the verified body) ─────────────────────────────────
def test_build_request_matches_verified_body():
    u = oh.resolve_underlying("NIFTY")
    body = oh.build_request(
        u, expiry_flag="WEEK", expiry_code=0, strike="ATM",
        drv_option_type="CALL", from_date="2021-01-01", to_date="2021-01-30",
    )
    assert body["exchangeSegment"] == "NSE_FNO"
    assert body["securityId"] == 13
    assert body["instrument"] == "OPTIDX"
    assert body["strike"] == "ATM"
    assert body["drvOptionType"] == "CALL"
    assert body["requiredData"] == ["open", "high", "low", "close", "iv", "oi", "volume"]


# ── orchestration: recording fake client + monkeypatched writers ────────────────────
class _FakeClient:
    """Records every _request payload and returns a canned side payload. If
    ``fail_predicate`` returns True for a payload, it raises (per-window failure)."""

    def __init__(self, fail_predicate=None):
        self.calls = []
        self.fail_predicate = fail_predicate

    async def _request(self, method, endpoint, rate_category, payload):
        self.calls.append(payload)
        if self.fail_predicate and self.fail_predicate(payload):
            raise RuntimeError("simulated DH-904 burst")
        ts = [_epoch_ist(2026, 6, 15, 15, 25)]
        side = "ce" if payload["drvOptionType"] == "CALL" else "pe"
        iv = 12.0 if side == "ce" else 16.0
        return {"data": {side: _side_payload(ts, [iv], [100.0], [50], [3]), "timestamp": ts}}


@pytest.fixture
def _capture_upserts(monkeypatch):
    captured = {"atm": [], "chain": []}
    monkeypatch.setattr(oh, "_upsert_atm_iv",
                        lambda rows: (captured["atm"].extend(rows), len(rows))[1])
    monkeypatch.setattr(oh, "_upsert_option_chain_snapshot",
                        lambda rows: (captured["chain"].extend(rows), len(rows))[1])
    return captured


def _off_hours():
    # Mon 2026-06-15 18:00 IST — after the 15:30 close, safe for live fetches.
    return datetime(2026, 6, 15, 18, 0, tzinfo=_IST)


def test_ingest_underlying_happy_path(_capture_upserts):
    u = oh.resolve_underlying("NIFTY")
    client = _FakeClient()
    result = asyncio.run(oh.ingest_underlying(
        client, u, date(2026, 6, 12), date(2026, 6, 18),
        strikes=1, expiry_dates=[date(2026, 6, 18)],
        req_spacing_sec=0, now=_off_hours(),
    ))
    # strikes=1 → offsets [0,-1,1] → 3 strikes × 2 legs = 6 calls, one window, one expiry.
    assert result["legs"] == 6
    assert all(p["instrument"] == "OPTIDX" for p in client.calls)
    assert {p["drvOptionType"] for p in client.calls} == {"CALL", "PUT"}
    # ATM (off==0) produced one straddle row; both call+put present → mean.
    assert len(_capture_upserts["atm"]) == 1
    assert _capture_upserts["atm"][0]["straddle_iv"] == pytest.approx(0.14)  # (0.12+0.16)/2
    # chain rows captured for every leg (1 bar each).
    assert len(_capture_upserts["chain"]) == 6


def test_ingest_underlying_per_window_failure_tolerated(_capture_upserts):
    u = oh.resolve_underlying("NIFTY")
    # Fail only the PUT legs → CALL legs still ingest; no exception bubbles up.
    client = _FakeClient(fail_predicate=lambda p: p["drvOptionType"] == "PUT")
    result = asyncio.run(oh.ingest_underlying(
        client, u, date(2026, 6, 12), date(2026, 6, 18),
        strikes=0, expiry_dates=[date(2026, 6, 18)],
        req_spacing_sec=0, now=_off_hours(),
    ))
    assert result["legs"] == 2  # one ATM strike × CALL+PUT (PUT raised but tolerated)
    # ATM row still written from the surviving CALL leg.
    assert len(_capture_upserts["atm"]) == 1
    assert _capture_upserts["atm"][0]["call_iv"] == pytest.approx(0.12)
    assert _capture_upserts["atm"][0]["put_iv"] is None


def test_ingest_underlying_no_chain_flag(_capture_upserts):
    u = oh.resolve_underlying("NIFTY")
    client = _FakeClient()
    asyncio.run(oh.ingest_underlying(
        client, u, date(2026, 6, 12), date(2026, 6, 18),
        strikes=0, capture_chain=False, expiry_dates=[date(2026, 6, 18)],
        req_spacing_sec=0, now=_off_hours(),
    ))
    assert len(_capture_upserts["chain"]) == 0
    assert len(_capture_upserts["atm"]) == 1


def test_ingest_underlying_refuses_market_hours(_capture_upserts):
    u = oh.resolve_underlying("NIFTY")
    client = _FakeClient()
    in_hours = datetime(2026, 6, 15, 11, 0, tzinfo=_IST)  # Mon 11:00 IST → open
    with pytest.raises(RuntimeError, match="market hours"):
        asyncio.run(oh.ingest_underlying(
            client, u, date(2026, 6, 12), date(2026, 6, 18),
            expiry_dates=[date(2026, 6, 18)], req_spacing_sec=0, now=in_hours,
        ))
    assert client.calls == []  # refused before any fetch


# ── token path (reuses fno_backfill.resolve_access_token) ───────────────────────────
def test_token_path_uses_cached_token(monkeypatch):
    import core.fno_backfill as fb

    # Cached token present → returned without touching the PIN/TOTP manager.
    monkeypatch.setattr("core.token_manager.read_current_token", lambda: "CACHED")
    tok = asyncio.run(oh.resolve_access_token())
    assert tok == "CACHED"
    # resolve_access_token is the SAME object imported from fno_backfill.
    assert oh.resolve_access_token is fb.resolve_access_token
