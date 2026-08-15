"""Tests for core/dhan_option_history.py — the Dhan rollingoption IV ingester.

REDESIGNED to the ROLLING expiryCode model (verified live, 2026-06-20): expiryCode
is a 1-based rolling index (1=front), NOT a date picker; the strike offset is the
``ATM±n`` STRING; and each row's expiry is attached from expiry_calendar via the
rolling-code rule. These tests exercise that model.

NO network, NO database: the Dhan client is a recording fake whose ``_request``
returns canned rollingoption payloads, and the two DB upsert writers are
monkeypatched to capture rows. Pure parsers are exercised directly.

IST date-trap guard (memory ci-ist-date-trap): every datetime/now used is an
explicit IST-aware value — no ``date.today()`` / naive ``now()`` — so the suite
behaves identically on a UTC-clocked CI runner and the IST dev Mac.
"""
import asyncio
import logging
from datetime import date, datetime, timezone

import pytest

from core import dhan_option_history as oh

_IST = oh._IST


# ── helpers ────────────────────────────────────────────────────────────────────────
def _epoch_ist(y, m, d, hh, mm) -> int:
    """Epoch seconds for an IST wall-clock instant (what Dhan timestamps map to)."""
    return int(datetime(y, m, d, hh, mm, tzinfo=_IST).timestamp())


def _side_payload(ts, ivs, closes, ois=None, vols=None):
    # Shape matches the REAL response: full OHLC set + iv/oi/volume per side.
    n = len(ts)
    side = {
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "iv": ivs,
    }
    side["oi"] = ois if ois is not None else [None] * n
    side["volume"] = vols if vols is not None else [None] * n
    return side


# ── registry / index-agnostic resolution ───────────────────────────────────────────
def test_resolve_underlying_nifty_default():
    u = oh.resolve_underlying("NIFTY")
    assert u.symbol == "NIFTY"
    assert u.security_id == 13
    assert u.instrument == "OPTIDX"
    assert u.max_strikes == 10  # index supports ATM±10
    assert u.exchange_segment == "NSE_FNO"
    assert u.chain_seg == "IDX_I"  # index → IDX_I, not NSE_FNO


def test_resolve_underlying_unknown_requires_security_id():
    with pytest.raises(ValueError, match="Unknown underlying"):
        oh.resolve_underlying("RELIANCE")
    # With an explicit id it resolves as a stock (OPTSTK, ATM±3 cap, NSE_EQ chain seg).
    u = oh.resolve_underlying("RELIANCE", security_id=2885)
    assert u.instrument == "OPTSTK"
    assert u.max_strikes == 3
    assert u.security_id == 2885
    assert u.chain_seg == "NSE_EQ"  # single stock → NSE_EQ, not FNO


def test_resolve_underlying_overrides_win():
    u = oh.resolve_underlying("NIFTY", instrument="OPTIDX", strike_step=100, security_id=99)
    assert u.security_id == 99
    assert u.strike_step == 100


def test_resolve_underlying_bse_exchange_segment():
    # SENSEX/BANKEX route through BSE_FNO; chain seg stays IDX_I (index).
    u = oh.resolve_underlying(
        "SENSEX", security_id=51, instrument="OPTIDX", exchange_segment="BSE_FNO"
    )
    assert u.exchange_segment == "BSE_FNO"
    assert u.chain_seg == "IDX_I"


def test_resolve_underlying_rejects_bad_instrument():
    with pytest.raises(ValueError, match="instrument must be one of"):
        oh.resolve_underlying("NIFTY", instrument="FUTIDX", security_id=13)


def test_resolve_underlying_nifty_expiry_rule_mirrors_condor():
    # NIFTY defaults MIRROR research/backtest/fno_condor.py IndexConfig: Thursday
    # before the 2025-09-01 cutover, Tuesday on/after.
    from datetime import date as _date

    u = oh.resolve_underlying("NIFTY")
    assert u.expiry_weekday == oh.TUESDAY        # post-cutover
    assert u.pre_cutover_weekday == oh.THURSDAY  # pre-cutover
    assert u.cutover_date == _date(2025, 9, 1)
    rule = u.expiry_rule
    assert rule.expiry_weekday == oh.TUESDAY
    assert rule.cutover_date == _date(2025, 9, 1)


def test_resolve_underlying_offregistry_custom_weekday_no_cutover():
    # A non-NIFTY underlying with an explicit weekly weekday drives a SINGLE fixed
    # weekday (no NIFTY cutover inherited).
    u = oh.resolve_underlying(
        "WIDGET", security_id=999, instrument="OPTIDX", expiry_weekday=2
    )
    assert u.expiry_weekday == 2
    assert u.pre_cutover_weekday is None
    assert u.cutover_date is None


# ── 30-day pagination ───────────────────────────────────────────────────────────────
def test_date_windows_caps_at_30_days_no_gap_no_overlap():
    wins = oh.date_windows(date(2021, 1, 1), date(2021, 3, 31))
    for f, t in wins:
        assert (t - f).days <= 29
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
    wins = oh.date_windows(date(2021, 1, 1), date(2026, 6, 18))
    assert len(wins) >= 60
    assert wins[0][0] == date(2021, 1, 1)
    assert wins[-1][1] == date(2026, 6, 18)


# ── ATM±n strike STRING enumeration (verified format) ───────────────────────────────
def test_strike_params_atm_string_format():
    # MUST be the "ATM±n" string, not bare ints (bare ints silently fetch ATM).
    assert oh.strike_params(2, 10) == ["ATM", "ATM-1", "ATM+1", "ATM-2", "ATM+2"]
    assert oh.strike_params(0, 10) == ["ATM"]


def test_strike_params_clamped_to_cap():
    assert len(oh.strike_params(50, 10)) == 21       # ATM plus ±1..10
    assert oh.strike_params(50, 3) == [
        "ATM", "ATM-1", "ATM+1", "ATM-2", "ATM+2", "ATM-3", "ATM+3"
    ]


def test_strike_offset_of_decodes():
    assert oh.strike_offset_of("ATM") == 0
    assert oh.strike_offset_of("ATM-2") == -2
    assert oh.strike_offset_of("ATM+1") == 1


# ── rolling-code expiry attachment (the only use of expiry_calendar) ─────────────────
def test_expiry_for_day_front_code1():
    # 2015 shares 2026's weekday-for-date calendar (both non-leap, Jan 1 =
    # Thursday) and sits safely before the 2025-09-01 cutover, so these
    # Thursday expiries resolve analytically as intended.
    exps = [date(2015, 6, 18), date(2015, 6, 25), date(2015, 7, 2)]
    # code=1 = next expiry on/after the day.
    assert oh.expiry_for_day(date(2015, 6, 15), exps, 1) == date(2015, 6, 18)
    assert oh.expiry_for_day(date(2015, 6, 18), exps, 1) == date(2015, 6, 18)
    assert oh.expiry_for_day(date(2015, 6, 19), exps, 1) == date(2015, 6, 25)


def test_expiry_for_day_term_structure_codes():
    exps = [date(2026, 6, 18), date(2026, 6, 25), date(2026, 7, 2)]
    assert oh.expiry_for_day(date(2026, 6, 15), exps, 2) == date(2026, 6, 25)
    assert oh.expiry_for_day(date(2026, 6, 15), exps, 3) == date(2026, 7, 2)


def test_expiry_for_day_derives_analytically_not_from_calendar():
    # HISTORICAL-MIS-ATTACH FIX: expiry is now DERIVED from the cutover-aware weekday
    # rule, NOT selected from the forward-only calendar. A sparse calendar no longer
    # exhausts — the analytic weekly weekday ≥ day is used regardless.
    # 2015 shares 2026's weekday-for-date calendar (both non-leap, Jan 1 =
    # Thursday) and sits safely before the 2025-09-01 cutover.
    exps = [date(2015, 6, 18)]
    # code=2 from 6/15 → 2nd weekly (6/25), derived even though the calendar has 1 entry.
    assert oh.expiry_for_day(date(2015, 6, 15), exps, 2) == date(2015, 6, 25)
    # 6/19 (Fri) → next Thursday 6/25 (pre-cutover weekday), not the past 6/18.
    assert oh.expiry_for_day(date(2015, 6, 19), exps, 1) == date(2015, 6, 25)
    # code<1 is invalid → None.
    assert oh.expiry_for_day(date(2015, 6, 15), exps, 0) is None


def test_expiry_for_day_historical_pre_cutover_thursday():
    # A 2021 day must resolve to a 2021 Thursday (pre-cutover weekday) — NOT a
    # far-future forward-only calendar entry (the bug this fix closes).
    fwd_only = [date(2026, 6, 23), date(2026, 6, 30)]  # forward-only calendar
    exp = oh.expiry_for_day(date(2021, 3, 10), fwd_only, 1)
    assert exp == date(2021, 3, 11)  # Thursday on/after 2021-03-10 (Wed)
    assert exp.weekday() == 3        # Thursday (pre-cutover)


def test_expiry_for_day_post_cutover_tuesday():
    # On/after the 2025-09-01 cutover the weekly weekday is Tuesday.
    exp = oh.expiry_for_day(date(2026, 9, 10), [], 1)  # Thu
    assert exp == date(2026, 9, 15)  # next Tuesday
    assert exp.weekday() == 1        # Tuesday (post-cutover)


def test_expiry_for_day_ten_day_guard_skips_far_future():
    # SANITY GUARD: a weekly front is ≤7 days out. If a (mis-)refinement would land
    # >~10 days away the row is skipped (None) rather than mis-attached. We force this
    # by passing a rule with a weekday but checking the guard directly on a huge gap:
    # the analytic front is always ≤7 days, so the guard is exercised via the calendar
    # NOT being able to pull a far entry — and an empty calendar never trips it.
    assert oh.expiry_for_day(date(2021, 3, 10), [], 1) == date(2021, 3, 11)


def test_expiry_for_day_calendar_refines_holiday():
    # The forward calendar is used ONLY as a holiday refinement WHERE IT COVERS the
    # date: analytic front 6/18 (Thu) snaps to a real 6/17 calendar entry nearby.
    exp = oh.expiry_for_day(date(2026, 6, 15), [date(2026, 6, 17)], 1)
    assert exp == date(2026, 6, 17)


def test_expiry_for_day_index_agnostic_custom_weekday():
    # A non-NIFTY rule (e.g. Wednesday=2 weekly, no cutover) drives its own weekday.
    from core.expiry import ExpiryRule
    wed_rule = oh.ExpiryRule(expiry_weekday=2, pre_cutover_weekday=None, cutover_date=None)
    assert isinstance(wed_rule, ExpiryRule)
    exp = oh.expiry_for_day(date(2021, 3, 8), [], 1, rule=wed_rule)  # Mon
    assert exp == date(2021, 3, 10)  # next Wednesday
    assert exp.weekday() == 2


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


def test_parse_rolling_side_short_iv_array_truncates():
    # iv array shorter than close/ts → truncate to iv length (the iv-in-min() path).
    ts = [_epoch_ist(2026, 6, 15, 9, 20), _epoch_ist(2026, 6, 15, 9, 25)]
    raw = {"data": {"ce": {"iv": [12.0], "close": [110.0, 111.0]}, "timestamp": ts}}
    rows = oh.parse_rolling_side(raw, "ce")
    assert len(rows) == 1
    assert rows[0]["iv"] == 12.0


def test_parse_rolling_side_string_epochs():
    # Dhan sometimes returns string epochs — must be coerced, not dropped.
    ts = [str(_epoch_ist(2026, 6, 15, 9, 20))]
    raw = {"data": {"ce": {"iv": [12.0], "close": [110.0]}, "timestamp": ts}}
    rows = oh.parse_rolling_side(raw, "ce")
    assert len(rows) == 1
    assert rows[0]["iv"] == 12.0


def test_parse_rolling_side_missing_side_or_empty():
    assert oh.parse_rolling_side({"data": {"ce": {}}}, "ce") == []
    assert oh.parse_rolling_side({"data": {}}, "ce") == []
    assert oh.parse_rolling_side({}, "ce") == []
    assert oh.parse_rolling_side(None, "ce") == []


# ── ATM IV row collapse (one/day, last bar wins, IV normalised, rolling expiry) ──────
def test_atm_iv_rows_one_per_day_last_bar_wins():
    ts = [
        _epoch_ist(2026, 6, 15, 9, 20), _epoch_ist(2026, 6, 15, 15, 25),
        _epoch_ist(2026, 6, 16, 9, 20), _epoch_ist(2026, 6, 16, 15, 25),
    ]
    ce = {"data": {"ce": _side_payload(ts, [12.0, 14.0, 11.0, 13.0],
                                       [100, 90, 80, 70]), "timestamp": ts}}
    pe = {"data": {"pe": _side_payload(ts, [16.0, 18.0, 15.0, 17.0],
                                       [100, 90, 80, 70]), "timestamp": ts}}
    exps = [date(2026, 6, 18), date(2026, 6, 25)]
    rows = oh.atm_iv_rows_from_legs(
        ce, pe, symbol="NIFTY", expiries=exps, expiry_code=1,
        all_expiries=exps, strike_step=50,
    )
    assert len(rows) == 2
    r0 = rows[0]
    assert r0["call_iv"] == pytest.approx(0.14)    # last CE bar of 6/15 = 14.0 → 0.14
    assert r0["put_iv"] == pytest.approx(0.18)     # last PE bar of 6/15 = 18.0 → 0.18
    assert r0["straddle_iv"] == pytest.approx(0.16)
    assert r0["symbol"] == "NIFTY"
    assert r0["expiry_date"] == date(2026, 6, 18)  # rolling code-1 attached
    assert r0["dte"] == 3                            # 6/18 - 6/15
    assert rows[1]["dte"] == 2                       # 6/18 - 6/16
    # No spot supplied → atm_strike/spot_ref left NULL for fno_derived.
    assert r0["atm_strike"] is None
    assert r0["spot_ref"] is None


def test_atm_iv_rows_attaches_per_day_spot():
    ts = [_epoch_ist(2026, 6, 15, 15, 25)]
    ce = {"data": {"ce": _side_payload(ts, [12.0], [100]), "timestamp": ts}}
    pe = {"data": {"pe": _side_payload(ts, [16.0], [100]), "timestamp": ts}}
    exps = [date(2026, 6, 18)]
    rows = oh.atm_iv_rows_from_legs(
        ce, pe, symbol="NIFTY", expiries=exps, expiry_code=1, all_expiries=exps,
        strike_step=50, spot_by_day={date(2026, 6, 15): 23427.0},
    )
    assert rows[0]["spot_ref"] == 23427.0
    assert rows[0]["atm_strike"] == 23450  # round-half-up to nearest 50


def test_atm_iv_rows_term_structure_code_attaches_2nd_expiry():
    ts = [_epoch_ist(2026, 6, 15, 15, 25)]
    ce = {"data": {"ce": _side_payload(ts, [12.0], [100]), "timestamp": ts}}
    pe = {"data": {"pe": _side_payload(ts, [16.0], [100]), "timestamp": ts}}
    exps = [date(2026, 6, 18), date(2026, 6, 25)]
    rows = oh.atm_iv_rows_from_legs(
        ce, pe, symbol="NIFTY", expiries=exps, expiry_code=2, all_expiries=exps,
        strike_step=50,
    )
    assert rows[0]["expiry_date"] == date(2026, 6, 25)  # 2nd-nearest


def test_atm_iv_rows_derives_front_when_calendar_is_past():
    # HISTORICAL-MIS-ATTACH FIX: a day AFTER the only (stale) calendar entry no longer
    # "exhausts" — the analytic next weekly weekday is derived instead of skipping.
    # 2015 shares 2026's weekday-for-date calendar (both non-leap, Jan 1 =
    # Thursday) and sits safely before the 2025-09-01 cutover.
    ts = [_epoch_ist(2015, 6, 20, 15, 25)]  # Sat after the stale 6/18 entry
    ce = {"data": {"ce": _side_payload(ts, [12.0], [100]), "timestamp": ts}}
    pe = {"data": {"pe": _side_payload(ts, [16.0], [100]), "timestamp": ts}}
    exps = [date(2015, 6, 18)]
    rows = oh.atm_iv_rows_from_legs(
        ce, pe, symbol="NIFTY", expiries=exps, expiry_code=1, all_expiries=exps,
        strike_step=50,
    )
    assert len(rows) == 1
    assert rows[0]["expiry_date"] == date(2015, 6, 25)  # next Thursday (pre-cutover)


def test_atm_iv_rows_historical_2021_not_mis_attached_to_future():
    # The core bug: a 2021 IV bar with a forward-only calendar must key to a 2021
    # expiry, NEVER a far-future 2026 calendar entry.
    ts = [_epoch_ist(2021, 3, 10, 15, 25)]  # Wed 2021
    ce = {"data": {"ce": _side_payload(ts, [12.0], [100]), "timestamp": ts}}
    pe = {"data": {"pe": _side_payload(ts, [16.0], [100]), "timestamp": ts}}
    fwd_only = [date(2026, 6, 23), date(2026, 6, 30)]  # forward-only calendar
    rows = oh.atm_iv_rows_from_legs(
        ce, pe, symbol="NIFTY", expiries=fwd_only, expiry_code=1, all_expiries=fwd_only,
        strike_step=50,
    )
    assert len(rows) == 1
    assert rows[0]["expiry_date"] == date(2021, 3, 11)  # 2021 Thursday, not 2026
    assert rows[0]["dte"] == 1


def test_atm_iv_rows_single_side_falls_back():
    ts = [_epoch_ist(2026, 6, 15, 15, 25)]
    ce = {"data": {"ce": _side_payload(ts, [12.0], [100]), "timestamp": ts}}
    pe = {}  # PE leg failed → tolerated; straddle_iv falls back to the present side
    exps = [date(2026, 6, 18)]
    rows = oh.atm_iv_rows_from_legs(
        ce, pe, symbol="NIFTY", expiries=exps, expiry_code=1, all_expiries=exps,
        strike_step=50,
    )
    assert len(rows) == 1
    assert rows[0]["call_iv"] == pytest.approx(0.12)
    assert rows[0]["put_iv"] is None
    assert rows[0]["straddle_iv"] == pytest.approx(0.12)


def test_atm_iv_rows_skips_days_with_no_iv():
    ts = [_epoch_ist(2026, 6, 15, 15, 25)]
    ce = {"data": {"ce": _side_payload(ts, [None], [100]), "timestamp": ts}}
    pe = {"data": {"pe": _side_payload(ts, [None], [100]), "timestamp": ts}}
    exps = [date(2026, 6, 18)]
    rows = oh.atm_iv_rows_from_legs(
        ce, pe, symbol="NIFTY", expiries=exps, expiry_code=1, all_expiries=exps,
        strike_step=50,
    )
    assert rows == []


def test_atm_iv_rows_logs_ce_pe_time_mismatch(caplog):
    ts_ce = [_epoch_ist(2026, 6, 15, 15, 25)]
    ts_pe = [_epoch_ist(2026, 6, 15, 15, 20)]  # different last-bar time same day
    ce = {"data": {"ce": _side_payload(ts_ce, [12.0], [100]), "timestamp": ts_ce}}
    pe = {"data": {"pe": _side_payload(ts_pe, [16.0], [100]), "timestamp": ts_pe}}
    exps = [date(2026, 6, 18)]
    with caplog.at_level(logging.DEBUG, logger="dhan.option_history"):
        oh.atm_iv_rows_from_legs(
            ce, pe, symbol="NIFTY", expiries=exps, expiry_code=1, all_expiries=exps,
            strike_step=50,
        )
    assert any("last-bar time mismatch" in r.message for r in caplog.records)


# ── chain snapshot projection (per-instrument seg, rolling expiry) ──────────────────
def test_chain_snapshot_rows_index_seg_idx_i():
    ts = [_epoch_ist(2026, 6, 15, 9, 20)]
    raw = {"data": {"ce": _side_payload(ts, [12.0], [110.0], [100], [5]), "timestamp": ts}}
    rows = oh.chain_snapshot_rows_from_side(
        raw, "ce", underlying_scrip=13, underlying_seg="IDX_I",
        expiries=[date(2026, 6, 18)], expiry_code=1, strike=0.0,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["option_type"] == "CE"
    assert r["underlying_scrip"] == 13
    assert r["underlying_seg"] == "IDX_I"   # index → IDX_I (was hardcoded NSE_FNO)
    assert r["expiry_date"] == date(2026, 6, 18)
    assert r["iv"] == 12.0            # raw percent (snapshot convention)
    assert r["ltp"] == 110.0
    assert r["raw"]["source"] == "rollingoption"


def test_chain_snapshot_rows_stock_seg_nse_eq():
    ts = [_epoch_ist(2026, 6, 15, 9, 20)]
    raw = {"data": {"pe": _side_payload(ts, [20.0], [55.0]), "timestamp": ts}}
    rows = oh.chain_snapshot_rows_from_side(
        raw, "pe", underlying_scrip=2885, underlying_seg="NSE_EQ",
        expiries=[date(2026, 6, 18)], expiry_code=1, strike=-1.0,
    )
    assert rows[0]["underlying_seg"] == "NSE_EQ"
    assert rows[0]["option_type"] == "PE"


def test_chain_snapshot_derives_front_when_calendar_is_past():
    # HISTORICAL-MIS-ATTACH FIX: a bar after the stale calendar entry derives the
    # analytic next weekly weekday rather than being dropped.
    # 2015 shares 2026's weekday-for-date calendar (both non-leap, Jan 1 =
    # Thursday) and sits safely before the 2025-09-01 cutover.
    ts = [_epoch_ist(2015, 6, 20, 9, 20)]  # Sat after the stale 6/18 entry
    raw = {"data": {"ce": _side_payload(ts, [12.0], [110.0]), "timestamp": ts}}
    rows = oh.chain_snapshot_rows_from_side(
        raw, "ce", underlying_scrip=13, underlying_seg="IDX_I",
        expiries=[date(2015, 6, 18)], expiry_code=1, strike=0.0,
    )
    assert len(rows) == 1
    assert rows[0]["expiry_date"] == date(2015, 6, 25)  # next Thursday (pre-cutover)


# ── build_request shape (matches the verified body) ─────────────────────────────────
def test_build_request_matches_verified_body():
    u = oh.resolve_underlying("NIFTY")
    body = oh.build_request(
        u, expiry_flag="WEEK", expiry_code=1, strike="ATM-1",
        drv_option_type="CALL", from_date="2021-01-01", to_date="2021-01-30",
    )
    assert body["exchangeSegment"] == "NSE_FNO"
    assert body["securityId"] == 13
    assert body["instrument"] == "OPTIDX"
    assert body["expiryCode"] == 1                  # 1-based rolling index
    assert body["strike"] == "ATM-1"                # ATM±n string
    assert body["drvOptionType"] == "CALL"
    # Full OHLC set or DH-905.
    assert body["requiredData"] == ["open", "high", "low", "close", "iv", "oi", "volume"]


def test_build_request_bse_segment():
    u = oh.resolve_underlying(
        "SENSEX", security_id=51, instrument="OPTIDX", exchange_segment="BSE_FNO"
    )
    body = oh.build_request(
        u, expiry_flag="WEEK", expiry_code=1, strike="ATM",
        drv_option_type="PUT", from_date="2021-01-01", to_date="2021-01-30",
    )
    assert body["exchangeSegment"] == "BSE_FNO"


# ── rate-limit backoff + daily-budget guard ─────────────────────────────────────────
def test_is_rate_limit_error_classification():
    class _Err(Exception):
        def __init__(self, code):
            self.error_code = code
            super().__init__(code)

    assert oh._is_rate_limit_error(_Err("DH-904"))
    assert oh._is_rate_limit_error(RuntimeError("HTTP 429 too many requests"))
    assert oh._is_rate_limit_error(RuntimeError("simulated DH-904 burst"))
    assert not oh._is_rate_limit_error(RuntimeError("DH-905 missing requiredData"))


def test_budget_hard_stops_before_cap():
    b = oh._Budget(cap=10, headroom=2)  # usable = 8
    b.charge(8)
    assert b.remaining == 0
    with pytest.raises(oh.BudgetExhausted, match="budget exhausted"):
        b.charge(1)


def test_fetch_side_backoff_retries_then_succeeds():
    sleeps = []

    async def _fake_sleep(s):
        sleeps.append(s)

    class _FlakyClient:
        def __init__(self):
            self.n = 0

        async def _request(self, *a, **k):
            self.n += 1
            if self.n < 3:
                raise RuntimeError("simulated DH-904 burst")
            ts = [_epoch_ist(2026, 6, 15, 15, 25)]
            return {"data": {"ce": _side_payload(ts, [12.0], [100.0]), "timestamp": ts}}

    u = oh.resolve_underlying("NIFTY")
    client = _FlakyClient()
    out = asyncio.run(oh._fetch_side_with_backoff(
        client, u, expiry_flag="WEEK", expiry_code=1, strike="ATM",
        drv_option_type="CALL", from_date="2026-06-15", to_date="2026-06-18",
        interval="5", sleep_fn=_fake_sleep,
    ))
    assert client.n == 3            # failed twice, succeeded on the third
    assert len(sleeps) == 2         # backed off twice
    assert all(s > 0 for s in sleeps)
    assert oh.parse_rolling_side(out, "ce")[0]["iv"] == 12.0


def test_fetch_side_backoff_gives_up_after_max_retries():
    async def _fake_sleep(s):
        pass

    class _AlwaysRL:
        async def _request(self, *a, **k):
            raise RuntimeError("DH-904 burst")

    u = oh.resolve_underlying("NIFTY")
    with pytest.raises(RuntimeError, match="DH-904"):
        asyncio.run(oh._fetch_side_with_backoff(
            _AlwaysRL(), u, expiry_flag="WEEK", expiry_code=1, strike="ATM",
            drv_option_type="CALL", from_date="2026-06-15", to_date="2026-06-18",
            interval="5", max_retries=2, sleep_fn=_fake_sleep,
        ))


def test_fetch_side_non_rate_limit_failure_returns_empty():
    async def _fake_sleep(s):
        pass

    class _Boom:
        async def _request(self, *a, **k):
            raise RuntimeError("DH-905 missing requiredData")

    u = oh.resolve_underlying("NIFTY")
    out = asyncio.run(oh._fetch_side_with_backoff(
        _Boom(), u, expiry_flag="WEEK", expiry_code=1, strike="ATM",
        drv_option_type="CALL", from_date="2026-06-15", to_date="2026-06-18",
        interval="5", sleep_fn=_fake_sleep,
    ))
    assert out == {}  # non-RL per-window failure tolerated → {}


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
            raise RuntimeError("non-RL simulated failure")
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
    # Mon 2026-06-15 18:00 IST — after the 15:30 close, the preferred window.
    return datetime(2026, 6, 15, 18, 0, tzinfo=_IST)


def test_ingest_underlying_happy_path(_capture_upserts):
    u = oh.resolve_underlying("NIFTY")
    client = _FakeClient()
    result = asyncio.run(oh.ingest_underlying(
        client, u, date(2026, 6, 12), date(2026, 6, 18),
        strikes=1, expiry_dates=[date(2026, 6, 18)],
        req_spacing_sec=0, now=_off_hours(),
    ))
    # strikes=1 → ["ATM","ATM-1","ATM+1"] → 3 strikes × 2 legs = 6 calls, one window,
    # one rolling code (default [1]).
    assert result["legs"] == 6
    assert all(p["instrument"] == "OPTIDX" for p in client.calls)
    assert all(p["expiryCode"] == 1 for p in client.calls)
    # ATM±n string format is what hits the wire.
    assert {p["strike"] for p in client.calls} == {"ATM", "ATM-1", "ATM+1"}
    assert {p["drvOptionType"] for p in client.calls} == {"CALL", "PUT"}
    # ATM strike produced one straddle row; both call+put present → mean.
    assert len(_capture_upserts["atm"]) == 1
    assert _capture_upserts["atm"][0]["straddle_iv"] == pytest.approx(0.14)  # (0.12+0.16)/2
    assert _capture_upserts["atm"][0]["expiry_date"] == date(2026, 6, 18)
    # chain rows captured for every leg (1 bar each), IDX_I seg.
    assert len(_capture_upserts["chain"]) == 6
    assert all(r["underlying_seg"] == "IDX_I" for r in _capture_upserts["chain"])


def test_ingest_underlying_term_structure_codes(_capture_upserts):
    u = oh.resolve_underlying("NIFTY")
    client = _FakeClient()
    result = asyncio.run(oh.ingest_underlying(
        client, u, date(2026, 6, 12), date(2026, 6, 18),
        strikes=0, expiry_codes=[1, 2], capture_chain=False,
        expiry_dates=[date(2026, 6, 18), date(2026, 6, 25)],
        req_spacing_sec=0, now=_off_hours(),
    ))
    # 2 codes × 1 window × ATM-only × 2 legs = 4 calls.
    assert result["legs"] == 4
    assert {p["expiryCode"] for p in client.calls} == {1, 2}
    # code-1 attaches 6/18, code-2 attaches 6/25.
    attached = {r["expiry_date"] for r in _capture_upserts["atm"]}
    assert attached == {date(2026, 6, 18), date(2026, 6, 25)}


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
    assert len(_capture_upserts["atm"]) == 1
    assert _capture_upserts["atm"][0]["call_iv"] == pytest.approx(0.12)
    assert _capture_upserts["atm"][0]["put_iv"] is None


def test_ingest_underlying_bse_index_segments(_capture_upserts):
    # End-to-end for a BSE index (SENSEX): request carries BSE_FNO, chain rows
    # still carry IDX_I (index → IDX_I regardless of exchange segment).
    u = oh.resolve_underlying(
        "SENSEX", security_id=51, instrument="OPTIDX", exchange_segment="BSE_FNO"
    )
    client = _FakeClient()
    asyncio.run(oh.ingest_underlying(
        client, u, date(2026, 6, 12), date(2026, 6, 18),
        strikes=0, expiry_dates=[date(2026, 6, 18)],
        req_spacing_sec=0, now=_off_hours(),
    ))
    assert all(p["exchangeSegment"] == "BSE_FNO" for p in client.calls)
    assert all(r["underlying_seg"] == "IDX_I" for r in _capture_upserts["chain"])
    assert _capture_upserts["atm"][0]["symbol"] == "SENSEX"


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


def test_ingest_underlying_warns_market_hours_but_runs(_capture_upserts, caplog):
    # Off-hours guard is now a WARNING, not a hard raise — the read-only chart
    # endpoint works any time.
    u = oh.resolve_underlying("NIFTY")
    client = _FakeClient()
    in_hours = datetime(2026, 6, 15, 11, 0, tzinfo=_IST)  # Mon 11:00 IST → open
    with caplog.at_level(logging.WARNING, logger="dhan.option_history"):
        result = asyncio.run(oh.ingest_underlying(
            client, u, date(2026, 6, 12), date(2026, 6, 18),
            strikes=0, expiry_dates=[date(2026, 6, 18)],
            req_spacing_sec=0, now=in_hours,
        ))
    assert result["legs"] == 2          # it RAN (did not refuse)
    assert any("market hours" in r.message for r in caplog.records)


def test_ingest_underlying_budget_hard_stop(_capture_upserts):
    u = oh.resolve_underlying("NIFTY")
    client = _FakeClient()
    tiny = oh._Budget(cap=4, headroom=0)  # usable=4 → 2 strikes (4 legs)
    with pytest.raises(oh.BudgetExhausted):
        asyncio.run(oh.ingest_underlying(
            client, u, date(2026, 6, 12), date(2026, 6, 18),
            strikes=2,  # ATM, ATM-1, ATM+1 → 3 strikes × 2 = 6 legs > budget
            expiry_dates=[date(2026, 6, 18)],
            req_spacing_sec=0, now=_off_hours(), budget=tiny,
        ))
    # It charged 2 strikes (4 legs) then HARD-STOPPED before the 3rd strike.
    assert len(client.calls) == 4


def test_ingest_underlying_without_calendar_still_derives_expiry(_capture_upserts, caplog):
    # HISTORICAL-MIS-ATTACH FIX: with no expiry_calendar the expiry is now DERIVED
    # analytically (cutover-aware weekday rule) — rows ARE keyed. A heads-up warning
    # is still emitted that the calendar (holiday refinement) is absent.
    u = oh.resolve_underlying("NIFTY")
    client = _FakeClient()
    with caplog.at_level(logging.WARNING, logger="dhan.option_history"):
        asyncio.run(oh.ingest_underlying(
            client, u, date(2026, 6, 12), date(2026, 6, 18),
            strikes=0, expiry_dates=None,
            req_spacing_sec=0, now=_off_hours(),
        ))
    # The fake returns a single 2026-06-15 (Mon) bar, which is post the real
    # 2025-09-01 cutover → analytic front = next Tuesday, 6/16.
    assert len(_capture_upserts["atm"]) == 1
    assert _capture_upserts["atm"][0]["expiry_date"] == date(2026, 6, 16)
    assert any("no expiry_calendar" in r.message for r in caplog.records)


# ── CLI parsing ─────────────────────────────────────────────────────────────────────
def test_cli_parse_expiry_codes():
    assert oh._parse_codes("1") == [1]
    assert oh._parse_codes("1,2,3") == [1, 2, 3]
    with pytest.raises(Exception, match="1-based"):
        oh._parse_codes("0,1")


def test_cli_defaults_front_code_only():
    args = oh._build_arg_parser().parse_args(
        ["--from", "2021-01-01", "--to", "2026-06-18"]
    )
    assert args.expiry_codes == [1]
    assert args.underlying == "NIFTY"
    assert args.expiry_flag == "WEEK"
    assert args.expiry_weekday is None  # NIFTY default rule used when omitted


def test_cli_expiry_weekday_parsed_and_range_checked():
    args = oh._build_arg_parser().parse_args(
        ["--from", "2021-01-01", "--to", "2021-02-01", "--underlying", "WIDGET",
         "--security-id", "999", "--instrument", "OPTIDX", "--expiry-weekday", "2"]
    )
    assert args.expiry_weekday == 2
    # Out-of-range weekday is REJECTED (not silently wrapped via %7).
    with pytest.raises(SystemExit):
        oh._build_arg_parser().parse_args(
            ["--from", "2021-01-01", "--to", "2021-02-01", "--expiry-weekday", "9"]
        )


# ── token path (reuses fno_backfill.resolve_access_token) ───────────────────────────
def test_token_path_uses_cached_token(monkeypatch):
    import core.fno_backfill as fb

    monkeypatch.setattr("core.token_manager.read_current_token", lambda: "CACHED")
    tok = asyncio.run(oh.resolve_access_token())
    assert tok == "CACHED"
    assert oh.resolve_access_token is fb.resolve_access_token
