"""Dhan rollingoption historical option-IV ingester — 5 years of REAL implied vol.

Closes the single biggest gap in the F&O research track: until now the condor
backtest used ``straddle_iv = India VIX close / 100`` as a *proxy* (VIX is a 30-day
constant-maturity index, NOT the weekly ATM straddle IV we actually trade). Dhan's
``POST /v2/charts/rollingoption`` exposes up to ~5 years of MINUTE-LEVEL option
history *including real implied volatility* — so we can backfill the true ATM
straddle IV per expiry/day and let
``research/backtest/fno_condor.resolve_iv_source`` read it.

ROLLING expiryCode MODEL (verified against the live API, 2026-06-20)
-------------------------------------------------------------------
``expiryCode`` is a **1-based ROLLING index, NOT a date picker**:

  • expiryCode=1 → the continuous FRONT (nearest) weekly/monthly series — its ATM
    IV *rolled* across the whole requested window (each trading day reflects that
    day's then-front contract).
  • expiryCode=2 → the 2nd-nearest series; 3 → 3rd; … (verified: codes 1–4 return
    the SAME window with a rising IV term structure, confirming the rolling model).

So there is NO offline "enumerate Thursdays / hardcode the expiry weekday" step —
that machinery (the old ``enumerate_expiry_codes``) was the source of two HIGH bugs
and a 405K-call blowout and is GONE. Instead we pull ``expiryCode=1`` (front), and
optionally 2..N for term structure, paginating 30-day windows over the date range.

To store into ``option_atm_iv`` (keyed by ``expiry_date``) we ATTACH each trading
day's expiry by DERIVING the **k-th weekly expiry ≥ d** ANALYTICALLY from the
cutover-aware, index-agnostic weekday rule (``core/expiry``) — for a row dated ``d``
under expiryCode ``k``. The forward-only ``expiry_calendar`` is used ONLY as a
holiday refinement where it actually covers ``d``; it is NEVER allowed to pick a
far-future entry for a historical day.

HISTORICAL-MIS-ATTACH FIX (2026-06-20): the previous design selected the k-th entry
of the forward-only ``expiry_calendar`` (which currently starts 2026-06-23). For any
pre-2026 day that picked a far-future 2026 expiry, mis-keying 2021–2025 IV rows onto
2026 expiries (the condor joins ``option_atm_iv`` by ``(date, expiry)`` → wrong
backtests). A >~10-day sanity guard (a weekly front is ≤7 days out) now skips/flags
any implausibly-distant resolution rather than mis-attaching.

Verified request body (smoke-tested → HTTP 200, real IV returned)::

    POST https://api.dhan.co/v2/charts/rollingoption
    {"exchangeSegment":"NSE_FNO","interval":"5","securityId":13,
     "instrument":"OPTIDX","expiryFlag":"WEEK"|"MONTH","expiryCode":1,
     "strike":"ATM"|"ATM-1"|"ATM+1",           # ATM±n STRING — verified
     "drvOptionType":"CALL"|"PUT",
     "requiredData":["open","high","low","close","iv","oi","volume"],  # full OHLC or DH-905
     "fromDate":"YYYY-MM-DD","toDate":"YYYY-MM-DD"}   # max 30-day window/call

Two verified gotchas baked in below:
  • The strike offset is the STRING ``"ATM±n"`` ("ATM-1","ATM+1"). A bare signed
    int ("-1") is SILENTLY treated as ATM (returns identical data) — so the old
    ``str(off)`` format mis-fetched ATM every time. Fixed to ``"ATM"/"ATM-{n}"/"ATM+{n}"``.
  • ``requiredData`` MUST carry the full ``open/high/low/close`` set or the server
    answers ``DH-905``. Kept in full.

Response shape::

    {"data": {"ce": {"iv":[..],"open":[..],"high":[..],"low":[..],"close":[..],
                     "oi":[..],"volume":[..]},
              "pe": {...}, "timestamp":[<epoch>,...]}}

(Some payloads carry the epoch array under ``data.timestamp``; others under each
side. String epochs are also tolerated. Both shapes handled.)

Hard invariants (CLAUDE.md + handoff):
  • Historical reads only — never touches an order path; ADDITIVE module.
  • Live Dhan fetches WARN (not raise) during market hours — the rollingoption
    chart endpoint is read-only and works any time; we only nudge toward off-hours.
  • Exponential backoff + jitter on rate-limit (DH-904 / 429), so a transient burst
    pauses-and-retries instead of silently dropping a 30-day window.
  • A daily-budget guard hard-STOPS the run as it approaches the 100K calls/day cap
    rather than silently no-op'ing the remainder of the range.
  • Index-agnostic by construction (memory ``index-agnostic-fno``): the underlying,
    exchange segment, strike fan-out and ATM step are all registry/parameter driven,
    never hardcoded into the fetch/parse path.
  • Pure parse helpers are deterministic + DB-free so they unit-test without creds
    or a database.

Run live from the trusted machine, off-hours — budget-safe default (front
expiryCode=1, index ATM±10) is a few thousand calls for 5 years::

    python -m core.dhan_option_history --underlying NIFTY \
        --from 2021-01-01 --to 2026-06-18
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

# Reuse the proven F&O helpers — token resolution, off-hours guard, IV
# normalisation, numeric coercion, ATM-strike rounding, expiry classification.
from core.fno_backfill import (
    _IST,
    _normalize_iv,
    _parse_date,
    _safe_float,
    classify_expiry,
    is_market_hours,
    nifty_atm_strike,
    resolve_access_token,
)
# Cutover-aware, index-agnostic front-expiry derivation. We attach each historical
# bar's expiry ANALYTICALLY (the k-th weekly weekday ≥ day, holiday-refined where
# the calendar covers it) — NOT by picking the nearest forward-only expiry_calendar
# entry (which mis-attached 2021–2025 IV rows to far-future 2026 expiries). See
# core/expiry.py for the full rationale.
from core.expiry import (
    NIFTY_EXPIRY_RULE,
    NIFTY_TUESDAY_EXPIRY_CUTOVER,
    THURSDAY,
    TUESDAY,
    WEEKLY_EXPIRY_MAX_DAYS,
    ExpiryRule,
    derive_front_expiry,
)

logger = logging.getLogger("dhan.option_history")

# Max history window Dhan accepts per rollingoption call.
_MAX_WINDOW_DAYS = 30
# Polite spacing between calls (charts endpoints tolerate ~1 req/s — DH-904 on bursts).
_REQ_SPACING_SEC = 1.0
# Dhan account ceiling is 100K API calls/day; hard-stop with headroom so a long run
# never trips the cap (which would start failing every subsequent call).
_DAILY_BUDGET = 100_000
_DAILY_BUDGET_HEADROOM = 2_000
# Rate-limit backoff (DH-904 / HTTP 429): exponential with jitter, capped attempts.
_RL_MAX_RETRIES = 6
_RL_BASE_SLEEP_SEC = 2.0
_RL_MAX_SLEEP_SEC = 60.0
# Dhan rate-limit error codes seen on burst (DH-904) plus the generic 429 marker.
_RATE_LIMIT_CODES = {"DH-904", "DH-905-RL", "429"}

_VALID_INSTRUMENTS = {"OPTIDX", "OPTSTK"}
_FULL_OHLC = ["open", "high", "low", "close", "iv", "oi", "volume"]


class BudgetExhausted(RuntimeError):
    """Raised to HARD-STOP a run when the daily API budget is (about to be)
    exhausted — surfaced to the caller rather than silently dropping windows."""


# ── underlying registry (index-agnostic; NIFTY default, never hardcoded) ──────────
@dataclass(frozen=True)
class Underlying:
    """Static descriptor for one option underlying.

    ``security_id`` is the Dhan rollingoption security id (13 = NIFTY OPTIDX);
    ``instrument`` is OPTIDX (index) or OPTSTK (single stock); ``strike_step`` is
    the ATM rounding step (₹); ``max_strikes`` caps the ATM±n fan-out (index 10,
    stock 3 per the API); ``exchange_segment`` is the rollingoption exchangeSegment
    (NSE_FNO for NSE; BSE_FNO for SENSEX/BANKEX); ``chain_seg`` is the per-instrument
    underlying segment recorded on option_chain_snapshot rows (IDX_I for an index,
    NSE_EQ for a single stock — NOT the FNO segment)."""

    symbol: str
    security_id: int
    instrument: str  # OPTIDX | OPTSTK
    strike_step: int
    max_strikes: int
    exchange_segment: str = "NSE_FNO"
    chain_seg: str = "IDX_I"
    # Expiry-weekday rule (cutover-aware, index-agnostic). Defaults to NIFTY —
    # Tuesday on/after the 2026-09-01 cutover, Thursday before — MIRRORING the
    # IndexConfig values in research/backtest/fno_condor.py (replicated in core to
    # avoid a research→core import). A non-NIFTY underlying overrides these.
    expiry_weekday: int = TUESDAY
    pre_cutover_weekday: Optional[int] = THURSDAY
    cutover_date: Optional[date] = NIFTY_TUESDAY_EXPIRY_CUTOVER

    @property
    def expiry_rule(self) -> ExpiryRule:
        """The cutover-aware ExpiryRule derived from this underlying's fields."""
        return ExpiryRule(
            expiry_weekday=self.expiry_weekday,
            pre_cutover_weekday=self.pre_cutover_weekday,
            cutover_date=self.cutover_date,
        )


# Index defaults — index supports ATM±10. Extend per new underlying (data-blocked
# multi-index expansion lands here). Stocks (OPTSTK) get ATM±3. BSE indices
# (SENSEX/BANKEX) route through BSE_FNO; their chain segment is still IDX_I.
# NOTE (multi-index): an OFF-REGISTRY symbol with NO --expiry-weekday silently
# inherits NIFTY's Thursday→Tuesday cutover rule. That is fine for NIFTY today, but
# each new index MUST be added here with its OWN expiry weekday/cutover (or pass
# --expiry-weekday) so it does not borrow NIFTY's expiry calendar by accident.
#
# BANKNIFTY (security_id=25, IDX_I — verified against core/instruments.INDEX_CONFIGS
# ("BANKNIFTY" → underlying_id "25", strike_step 100, lot 30) and the index-config
# notes in research/backtest/fno_condor.py). Its expiry weekday is its OWN rule, NOT
# NIFTY's: BANKNIFTY weeklies were discontinued (monthly-only) and it never adopted
# NIFTY's Thursday→Tuesday 2026-09-01 weekly cutover. It is pinned with
# expiry_weekday=THURSDAY, pre_cutover_weekday=None, cutover_date=None so the
# derivation uses a SINGLE fixed weekly/monthly weekday and never borrows NIFTY's
# calendar (the OFF-cutover off-registry footgun the registry doc warns about). The
# index ATM fan-out cap stays 10; strike step 100. (Refine the exact current NSE
# BANKNIFTY monthly-expiry weekday before any live go/no-go — research-only, flagged.)
UNDERLYINGS: dict[str, Underlying] = {
    "NIFTY": Underlying("NIFTY", 13, "OPTIDX", 50, 10, "NSE_FNO", "IDX_I"),
    "BANKNIFTY": Underlying(
        "BANKNIFTY", 25, "OPTIDX", 100, 10, "NSE_FNO", "IDX_I",
        expiry_weekday=THURSDAY, pre_cutover_weekday=None, cutover_date=None,
    ),
}


def resolve_underlying(
    symbol: str,
    *,
    security_id: Optional[int] = None,
    instrument: Optional[str] = None,
    strike_step: Optional[int] = None,
    exchange_segment: Optional[str] = None,
    expiry_weekday: Optional[int] = None,
    pre_cutover_weekday: Optional[int] = None,
    cutover_date: Optional[date] = None,
) -> Underlying:
    """Resolve an Underlying for ``symbol`` from the registry, with explicit
    overrides for off-registry underlyings (e.g. a single stock not yet listed).

    A registry hit is returned as-is unless overridden. For an unknown symbol the
    caller MUST supply at least ``security_id`` (and ``instrument`` — defaults to
    OPTSTK for unknowns, since indices are expected to be pre-registered).

    ``exchange_segment`` defaults to the registry value (NSE_FNO); pass BSE_FNO
    for SENSEX/BANKEX. The per-instrument chain segment is derived from the
    resolved instrument: IDX_I for an index, NSE_EQ for a single stock."""
    base = UNDERLYINGS.get(symbol.upper())
    if base is None and security_id is None:
        raise ValueError(
            f"Unknown underlying {symbol!r} and no --security-id given. "
            f"Known: {sorted(UNDERLYINGS)}"
        )
    inst = instrument or (base.instrument if base else "OPTSTK")
    if inst not in _VALID_INSTRUMENTS:
        raise ValueError(f"instrument must be one of {sorted(_VALID_INSTRUMENTS)}, got {inst!r}")
    is_index = inst == "OPTIDX"
    seg = exchange_segment or (base.exchange_segment if base else "NSE_FNO")
    # Chain (snapshot) underlying segment is per-instrument: index → IDX_I,
    # single stock → NSE_EQ. NEVER the FNO segment (that was a hardcoded bug).
    chain_seg = "IDX_I" if is_index else "NSE_EQ"
    # Expiry-weekday rule (index-agnostic). Precedence per field:
    #   explicit override > registry base value > sensible default.
    # The default depends on whether the caller pinned a custom weekly weekday for an
    # OFF-REGISTRY underlying: if so it gets a SINGLE fixed weekday (no NIFTY cutover);
    # otherwise (NIFTY / no custom weekday) it reproduces fno_condor.NIFTY's cutover.
    custom_offreg = base is None and expiry_weekday is not None
    e_wd = expiry_weekday if expiry_weekday is not None else (base.expiry_weekday if base else TUESDAY)
    if pre_cutover_weekday is not None:
        e_pre: Optional[int] = pre_cutover_weekday
    elif base is not None:
        e_pre = base.pre_cutover_weekday
    else:
        e_pre = None if custom_offreg else THURSDAY
    if cutover_date is not None:
        e_cut: Optional[date] = cutover_date
    elif base is not None:
        e_cut = base.cutover_date
    else:
        e_cut = None if custom_offreg else NIFTY_TUESDAY_EXPIRY_CUTOVER
    return Underlying(
        symbol=symbol.upper(),
        security_id=security_id if security_id is not None else base.security_id,  # type: ignore[union-attr]
        instrument=inst,
        strike_step=strike_step if strike_step is not None else (base.strike_step if base else 50),
        max_strikes=(base.max_strikes if base else (10 if is_index else 3)),
        exchange_segment=seg,
        chain_seg=chain_seg,
        expiry_weekday=e_wd,
        pre_cutover_weekday=e_pre,
        cutover_date=e_cut,
    )


# ── pagination ───────────────────────────────────────────────────────────────────
def date_windows(
    from_date: date, to_date: date, *, max_days: int = _MAX_WINDOW_DAYS
) -> list[tuple[date, date]]:
    """Split [from_date, to_date] into consecutive ≤``max_days``-day inclusive
    windows (Dhan rollingoption caps each call at 30 days). Windows are
    stitched so no day is fetched twice and none is skipped."""
    if to_date < from_date:
        raise ValueError(f"to_date {to_date} precedes from_date {from_date}")
    windows: list[tuple[date, date]] = []
    cur = from_date
    span = timedelta(days=max_days - 1)
    while cur <= to_date:
        end = min(cur + span, to_date)
        windows.append((cur, end))
        cur = end + timedelta(days=1)
    return windows


def strike_params(n: int, cap: int) -> list[str]:
    """ATM±``n`` strike strings clamped to the per-instrument ``cap`` (index 10,
    stock 3), ATM-first then outward: ["ATM","ATM-1","ATM+1","ATM-2","ATM+2",…].

    VERIFIED FORMAT: Dhan expects the literal ``"ATM"``/``"ATM-{k}"``/``"ATM+{k}"``
    string. A bare signed int ("-1") is silently treated as ATM and returns
    identical data, so we MUST emit the ``ATM±n`` spelling. ``n``≤0 → ["ATM"]."""
    n = min(max(int(n), 0), int(cap))
    out = ["ATM"]
    for i in range(1, n + 1):
        out.append(f"ATM-{i}")
        out.append(f"ATM+{i}")
    return out


def strike_offset_of(param: str) -> int:
    """Numeric ATM offset encoded in an ``ATM±n`` strike string ("ATM"→0,
    "ATM-2"→-2, "ATM+1"→+1). Used only for distinct pseudo-strike provenance on
    chain-snapshot rows (the true numeric strike is resolved server-side)."""
    if param == "ATM":
        return 0
    try:
        return int(param[3:])  # "ATM-2" → "-2", "ATM+1" → "+1"
    except ValueError:
        return 0


# ── expiry attachment (cutover-aware analytic derivation, NOT forward-only calendar) ─
def expiry_for_day(
    day: date,
    expiries: list[date],
    code: int,
    *,
    rule: ExpiryRule = NIFTY_EXPIRY_RULE,
) -> Optional[date]:
    """Return the expiry to ATTACH to a row dated ``day`` under rolling expiryCode
    ``code`` (1-based): the ``code``-th WEEKLY expiry ≥ ``day``.

    HISTORICAL-MIS-ATTACH FIX (2026-06-20). The expiry is derived ANALYTICALLY from
    the cutover-aware, index-agnostic weekday rule (``core/expiry.derive_front_expiry``)
    — NOT by selecting the ``code``-th entry of the forward-only ``expiry_calendar``.
    The old calendar-selection picked the nearest *available* calendar expiry, which
    for any pre-2026 day was a far-future 2026 entry (the calendar starts 2026-06-23),
    so a 5-year pull mis-keyed 2021–2025 IV rows onto 2026 expiries.

    ``expiries`` (the expiry_calendar list) is now used ONLY as a REFINEMENT where it
    actually covers the date (snapping the analytic expiry to a nearby real/holiday-
    adjusted entry); it can NEVER pull a historical day to a far-future expiry.

    A >~10-day SANITY GUARD (a weekly front is ≤7 days out) returns ``None`` when the
    resolved expiry is implausibly far from ``day`` — the row is then SKIPPED/flagged
    rather than mis-attached. Returns ``None`` for ``code`` < 1 as well."""
    if code < 1:
        return None
    return derive_front_expiry(
        day, code, rule=rule, calendar=expiries, max_days=WEEKLY_EXPIRY_MAX_DAYS
    )


# ── request builder + epoch helpers ──────────────────────────────────────────────
def build_request(
    u: Underlying,
    *,
    expiry_flag: str,
    expiry_code: int,
    strike: str,
    drv_option_type: str,
    from_date: str,
    to_date: str,
    interval: str = "5",
) -> dict[str, Any]:
    """Assemble the verified rollingoption request body for one CE-or-PE leg.

    ``strike`` is the ``ATM±n`` string ("ATM","ATM-1","ATM+1"); ``drv_option_type``
    is "CALL"|"PUT"; ``expiry_flag`` is "WEEK"|"MONTH"; ``expiry_code`` is the
    1-based ROLLING index (1 = front). ``requiredData`` carries the full OHLC set
    (open/high/low/close) — omitting it returns DH-905."""
    return {
        "exchangeSegment": u.exchange_segment,
        "interval": interval,
        "securityId": u.security_id,
        "instrument": u.instrument,
        "expiryFlag": expiry_flag,
        "expiryCode": expiry_code,
        "strike": strike,
        "drvOptionType": drv_option_type,
        "requiredData": list(_FULL_OHLC),
        "fromDate": from_date,
        "toDate": to_date,
    }


def _extract_timestamps(side: dict[str, Any], outer: dict[str, Any]) -> list[Any]:
    """Find the epoch-timestamp array. Dhan puts it under ``data.timestamp`` in
    some payloads and inside each side in others; accept several key spellings."""
    for key in ("timestamp", "start_Time", "start_time", "time", "epoch"):
        arr = side.get(key) or outer.get(key)
        if arr:
            return arr
    return []


def _coerce_epoch(v: Any) -> Optional[int]:
    """Coerce a timestamp element (int, float, or string epoch) to int seconds.
    Returns None for anything unparseable (the bar is then skipped)."""
    if v is None:
        return None
    try:
        return int(float(v))  # tolerates "1623750300" and 1623750300.0
    except (TypeError, ValueError):
        return None


# ── pure parsers (deterministic, DB-free) ─────────────────────────────────────────
def parse_rolling_side(raw: dict[str, Any], side: str) -> list[dict[str, Any]]:
    """Parse one CE/PE side of a rollingoption response into per-bar dicts.

    ``side`` is "ce" or "pe". Returns rows with keys time(UTC datetime),
    open/high/low/close (floats, may be None), iv (RAW percent as Dhan returns,
    via _safe_float — NOT normalised here), oi/volume (ints). Truncates to the
    shortest of {timestamp, close, iv} so a partial/mismatched payload (incl. a
    short iv array) skips its tail silently. Missing side or empty arrays → []."""
    data = (raw.get("data") or raw) if isinstance(raw, dict) else {}
    node = data.get(side)
    if not isinstance(node, dict):
        return []
    ts = _extract_timestamps(node, data)
    closes = node.get("close") or []
    ivs = node.get("iv") or []
    # Include iv in the truncation floor: if Dhan returns a short iv array we
    # must not index past it (and we drop the un-IV'd tail rather than emit junk).
    n = min(len(ts), len(closes), len(ivs)) if (ts and ivs) else 0
    if n == 0:
        # No usable IV/close/timestamp triple → nothing to store for this side.
        return []
    opens = node.get("open") or [None] * n
    highs = node.get("high") or [None] * n
    lows = node.get("low") or [None] * n
    ois = node.get("oi") or node.get("open_interest") or [None] * n
    vols = node.get("volume") or [None] * n
    rows: list[dict[str, Any]] = []
    for i in range(n):
        epoch = _coerce_epoch(ts[i])
        if epoch is None:
            continue
        try:
            t = datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        oi = _safe_float(ois[i]) if i < len(ois) else None
        vol = _safe_float(vols[i]) if i < len(vols) else None
        rows.append(
            {
                "time": t,
                "open": _safe_float(opens[i]) if i < len(opens) else None,
                "high": _safe_float(highs[i]) if i < len(highs) else None,
                "low": _safe_float(lows[i]) if i < len(lows) else None,
                "close": _safe_float(closes[i]),
                "iv": _safe_float(ivs[i]) if i < len(ivs) else None,  # raw percent
                "oi": int(oi) if oi is not None else None,
                "volume": int(vol) if vol is not None else None,
            }
        )
    return rows


def atm_iv_rows_from_legs(
    ce_raw: dict[str, Any],
    pe_raw: dict[str, Any],
    *,
    symbol: str,
    expiries: list[date],
    expiry_code: int,
    all_expiries: list[date],
    strike_step: int,
    spot_by_day: Optional[dict[date, float]] = None,
    rule: ExpiryRule = NIFTY_EXPIRY_RULE,
) -> list[dict[str, Any]]:
    """Collapse CE+PE minute bars for the ATM strike into one option_atm_iv row
    PER TRADING DAY (the table is one ATM sample/day/expiry — alembic 009).

    ROLLING model: the bars span many trading days under one ``expiry_code``, so
    each day's expiry is ATTACHED from ``expiries`` (the expiry_calendar list) via
    ``expiry_for_day`` — the ``code``-th weekly/monthly expiry ≥ that day. Days for
    which no such expiry exists are skipped (not mis-attached).

    For each day we take the LAST intraday bar (closest to the cash close — the
    entry reference the condor prices off), normalise its IV to a fraction, and
    compute ``straddle_iv = mean(call_iv, put_iv)``. ``atm_strike``/``spot_ref`` are
    attached PER DAY from ``spot_by_day`` (the index/underlying close that day) when
    supplied; otherwise left NULL for ``core/fno_derived`` to backfill — we never
    stamp a single window-open spot across 30 days.

    Returns rows ready for ``_upsert_atm_iv`` (keys match fno_backfill's upsert)."""
    ce = parse_rolling_side(ce_raw, "ce")
    pe = parse_rolling_side(pe_raw, "pe")

    def _by_day_last(rows: list[dict[str, Any]]) -> dict[date, dict[str, Any]]:
        # rows are chronological; later bar overwrites → last-of-day wins.
        out: dict[date, dict[str, Any]] = {}
        for r in rows:
            out[r["time"].astimezone(_IST).date()] = r
        return out

    ce_day = _by_day_last(ce)
    pe_day = _by_day_last(pe)
    days = sorted(set(ce_day) | set(pe_day))
    spot_by_day = spot_by_day or {}

    rows: list[dict[str, Any]] = []
    for d in days:
        expiry_date = expiry_for_day(d, expiries, expiry_code, rule=rule)
        if expiry_date is None:
            # No plausible expiry for this day under this rolling code (sanity guard
            # tripped or code<1) → cannot key the option_atm_iv row; skip rather than
            # attach a wrong/far-future expiry.
            logger.debug(
                "atm_iv: no plausible expiry for %s under code=%d (guard/skip) — skipping",
                d, expiry_code,
            )
            continue
        c = ce_day.get(d)
        p = pe_day.get(d)
        # Debug visibility: CE/PE last-bar wall-clock mismatch on the same day.
        if c is not None and p is not None and c["time"] != p["time"]:
            logger.debug(
                "atm_iv: CE/PE last-bar time mismatch on %s: ce=%s pe=%s",
                d, c["time"].isoformat(), p["time"].isoformat(),
            )
        call_iv = _normalize_iv(c["iv"]) if c else None
        put_iv = _normalize_iv(p["iv"]) if p else None
        if call_iv is None and put_iv is None:
            continue  # nothing real to store for this day
        straddle_iv = (
            (call_iv + put_iv) / 2
            if (call_iv is not None and put_iv is not None)
            else (call_iv if call_iv is not None else put_iv)
        )
        spot = spot_by_day.get(d)
        atm_strike = nifty_atm_strike(float(spot), strike_step) if spot is not None else None
        expiry_type = classify_expiry(expiry_date, all_expiries) if all_expiries else None
        # Sample time = last bar's wall-clock (UTC), so option_atm_iv.time is the
        # genuine intraday capture instant, not midnight.
        sample = c or p
        rows.append(
            {
                "time": sample["time"],
                "symbol": symbol,
                "expiry_date": expiry_date,
                "expiry_type": expiry_type,
                "atm_strike": atm_strike,  # NULL → fno_derived backfills from close
                "call_iv": call_iv,
                "put_iv": put_iv,
                "straddle_iv": straddle_iv,
                "dte": max((expiry_date - d).days, 0),
                "spot_ref": float(spot) if spot is not None else None,  # NULL → fno_derived
                "implied_move": None,  # derived later by core/fno_derived
            }
        )
    return rows


def chain_snapshot_rows_from_side(
    raw: dict[str, Any],
    side: str,
    *,
    underlying_scrip: int,
    underlying_seg: str,
    expiries: list[date],
    expiry_code: int,
    strike: float,
    rule: ExpiryRule = NIFTY_EXPIRY_RULE,
) -> list[dict[str, Any]]:
    """Project one CE/PE rollingoption side into option_chain_snapshot rows
    (one per minute bar). IV stored RAW (percent) to match the snapshot table's
    convention. option_type is CE for the call side, PE for the put.

    ``underlying_seg`` is per-instrument (IDX_I for an index, NSE_EQ for a single
    stock) — NOT the FNO segment. Each bar's ``expiry_date`` is DERIVED analytically
    via ``expiry_for_day`` (the cutover-aware weekly rule, with ``expiries`` only
    refining to holiday-adjusted dates) so snapshot rows carry the right expiry
    across the multi-day window; bars whose expiry trips the sanity guard are dropped."""
    opt_type = "CE" if side == "ce" else "PE"
    bars = parse_rolling_side(raw, side)
    rows: list[dict[str, Any]] = []
    for b in bars:
        d = b["time"].astimezone(_IST).date()
        expiry_date = expiry_for_day(d, expiries, expiry_code, rule=rule)
        if expiry_date is None:
            continue
        rows.append(
            {
                "snapshot_time": b["time"],
                "underlying_scrip": underlying_scrip,
                "underlying_seg": underlying_seg,
                "expiry_date": expiry_date,
                "strike": float(strike),
                "option_type": opt_type,
                "security_id": None,
                "ltp": b["close"],
                "prev_close": None,
                "volume": b["volume"],
                "oi": b["oi"],
                "prev_oi": None,
                "prev_volume": None,
                "top_bid_price": None,
                "top_ask_price": None,
                "top_bid_qty": None,
                "top_ask_qty": None,
                "iv": b["iv"],  # raw percent (snapshot convention)
                "delta": None,
                "theta": None,
                "gamma": None,
                "vega": None,
                "spot": None,
                "raw": {
                    "open": b["open"], "high": b["high"], "low": b["low"],
                    "close": b["close"], "iv": b["iv"], "oi": b["oi"],
                    "volume": b["volume"], "source": "rollingoption",
                },
            }
        )
    return rows


# ── DB upserts (reuse fno_backfill's writers verbatim) ────────────────────────────
def _upsert_atm_iv(rows: list[dict[str, Any]]) -> int:
    from core.fno_backfill import _upsert_atm_iv as _w  # lazy: keep parsers DB-free

    return _w(rows)


def _upsert_option_chain_snapshot(rows: list[dict[str, Any]]) -> int:
    from core.fno_backfill import _upsert_option_chain_snapshot as _w

    return _w(rows)


# ── rate-limit backoff + daily-budget guard ───────────────────────────────────────
def _is_rate_limit_error(exc: Exception) -> bool:
    """True if ``exc`` is a Dhan rate-limit / DH-904 / HTTP-429 style error that we
    should back off and retry rather than drop the window for."""
    code = getattr(exc, "error_code", None) or getattr(exc, "category", None)
    if code is not None and str(code) in _RATE_LIMIT_CODES:
        return True
    msg = str(exc).lower()
    return (
        "dh-904" in msg
        or "rate limit" in msg
        or "too many requests" in msg
        or "429" in msg
    )


class _Budget:
    """Daily API-call budget guard. Counts every leg attempt and HARD-STOPS the
    run (raises ``BudgetExhausted``) before the next call would cross the cap, so a
    long range never silently runs into the 100K/day ceiling and starts failing."""

    def __init__(self, cap: int = _DAILY_BUDGET, headroom: int = _DAILY_BUDGET_HEADROOM):
        self.cap = cap
        self.headroom = headroom
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.cap - self.headroom - self.used

    def charge(self, n: int = 1) -> None:
        if self.remaining < n:
            raise BudgetExhausted(
                f"daily API budget exhausted: used={self.used}, cap={self.cap}, "
                f"headroom={self.headroom}; stopping before the next call would "
                f"cross the limit (resume tomorrow / narrow the date range)."
            )
        self.used += n


# ── live orchestration (off-hours preferred; rollingoption is read-only) ───────────
async def _fetch_side_with_backoff(
    client: Any,
    u: Underlying,
    *,
    expiry_flag: str,
    expiry_code: int,
    strike: str,
    drv_option_type: str,
    from_date: str,
    to_date: str,
    interval: str,
    max_retries: int = _RL_MAX_RETRIES,
    sleep_fn: Any = asyncio.sleep,
) -> dict[str, Any]:
    """POST one rollingoption leg with exponential backoff + jitter on rate-limit
    (DH-904 / 429). Returns {} only on a NON-rate-limit per-window failure (those
    are tolerated/skipped). Rate-limit errors are retried up to ``max_retries``;
    if they persist the exception propagates (do NOT silently drop the window)."""
    payload = build_request(
        u,
        expiry_flag=expiry_flag,
        expiry_code=expiry_code,
        strike=strike,
        drv_option_type=drv_option_type,
        from_date=from_date,
        to_date=to_date,
        interval=interval,
    )
    attempt = 0
    while True:
        try:
            return await client._request("POST", "charts/rollingoption", "data", payload)
        except Exception as exc:  # noqa: BLE001 — classify then re-raise/skip
            if _is_rate_limit_error(exc):
                attempt += 1
                if attempt > max_retries:
                    logger.error(
                        "rollingoption rate-limited %d× (%s code=%s strike=%s %s→%s) — "
                        "giving up on this window (not silently dropped)",
                        max_retries, u.symbol, expiry_code, strike, from_date, to_date,
                    )
                    raise
                wait = min(_RL_BASE_SLEEP_SEC * (2 ** (attempt - 1)), _RL_MAX_SLEEP_SEC)
                wait += random.uniform(0, wait * 0.25)  # jitter to de-correlate bursts
                logger.warning(
                    "rollingoption rate-limited (attempt %d/%d) — backing off %.1fs: %s",
                    attempt, max_retries, wait, exc,
                )
                await sleep_fn(wait)
                continue
            logger.warning(
                "rollingoption fetch failed (%s code=%s strike=%s %s→%s): %s — skipping window",
                u.symbol, expiry_code, strike, from_date, to_date, exc,
            )
            return {}


async def ingest_underlying(
    client: Any,
    u: Underlying,
    from_date: date,
    to_date: date,
    *,
    strikes: Optional[int] = None,
    interval: str = "5",
    capture_chain: bool = True,
    expiry_dates: Optional[Iterable[date]] = None,
    expiry_codes: Optional[Iterable[int]] = None,
    expiry_flag: str = "WEEK",
    spot_by_day: Optional[dict[date, float]] = None,
    req_spacing_sec: float = _REQ_SPACING_SEC,
    budget: Optional["_Budget"] = None,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """Backfill one underlying's option IV over [from_date, to_date] using the
    ROLLING expiryCode model.

    For every (expiry_code, ≤30-day window, ATM±n strike) it pulls the CE+PE legs
    (with rate-limit backoff), writes the per-day ATM straddle into option_atm_iv
    — DERIVING each day's expiry analytically from the underlying's cutover-aware
    weekday rule (``expiry_dates`` only refines it to holiday-adjusted dates) —
    and (when ``capture_chain``) the per-strike bars into option_chain_snapshot.
    Non-rate-limit per-window failures are tolerated (logged, skipped). A daily
    budget guard hard-stops near the 100K cap.

    ``strikes`` overrides the per-instrument ATM±n fan-out (clamped to the cap).
    ``expiry_codes`` is the rolling indices to pull (default ``[1]`` = front; pass
    e.g. ``[1,2,3]`` for term structure). ``expiry_dates`` (the expiry_calendar
    list) is now OPTIONAL — expiries are DERIVED analytically from the underlying's
    cutover-aware weekday rule; the calendar, when supplied, only refines them to
    holiday-adjusted real dates where it covers the bar.
    ``spot_by_day`` (index/underlying close per day) supplies atm_strike/spot_ref
    per day; absent → left NULL for fno_derived. Off-hours preferred (warns only).
    Returns a counts dict.
    """
    # Read-only chart endpoint works any time; nudge toward off-hours, do not block.
    if is_market_hours(now):
        logger.warning(
            "ingest_underlying running during market hours (09:15–15:30 IST). The "
            "rollingoption endpoint is read-only so this is allowed, but prefer "
            "off-hours to avoid contending with live data calls."
        )

    n_off = strikes if strikes is not None else u.max_strikes
    strikes_list = strike_params(n_off, u.max_strikes)
    windows = date_windows(from_date, to_date)
    codes = sorted({int(c) for c in (expiry_codes or [1]) if int(c) >= 1}) or [1]

    expiries = sorted(set(expiry_dates)) if expiry_dates is not None else []
    if not expiries:
        logger.warning(
            "ingest_underlying[%s]: no expiry_calendar dates supplied — expiries will "
            "be DERIVED analytically from the cutover-aware weekday rule (correct, but "
            "without the holiday refinement the calendar provides). Build expiry_calendar "
            "(core.fno_backfill build_expiry_calendar) for holiday-adjusted snapping.",
            u.symbol,
        )

    budget = budget if budget is not None else _Budget()
    spot_by_day = spot_by_day or {}

    counts = {"atm_rows": 0, "chain_rows": 0, "legs": 0, "windows": 0}

    for expiry_code in codes:
        for w_from, w_to in windows:
            counts["windows"] += 1
            for strike in strikes_list:
                budget.charge(2)  # CALL + PUT (hard-stops before crossing the cap)
                ce_raw = await _fetch_side_with_backoff(
                    client, u, expiry_flag=expiry_flag, expiry_code=expiry_code,
                    strike=strike, drv_option_type="CALL",
                    from_date=w_from.isoformat(), to_date=w_to.isoformat(),
                    interval=interval,
                )
                await asyncio.sleep(req_spacing_sec)
                pe_raw = await _fetch_side_with_backoff(
                    client, u, expiry_flag=expiry_flag, expiry_code=expiry_code,
                    strike=strike, drv_option_type="PUT",
                    from_date=w_from.isoformat(), to_date=w_to.isoformat(),
                    interval=interval,
                )
                await asyncio.sleep(req_spacing_sec)
                counts["legs"] += 2

                if strike == "ATM":
                    atm_rows = atm_iv_rows_from_legs(
                        ce_raw, pe_raw, symbol=u.symbol, expiries=expiries,
                        expiry_code=expiry_code, all_expiries=expiries,
                        strike_step=u.strike_step, spot_by_day=spot_by_day,
                        rule=u.expiry_rule,
                    )
                    counts["atm_rows"] += _upsert_atm_iv(atm_rows)

                if capture_chain:
                    # The actual numeric strike is resolved server-side from ATM±k;
                    # record the offset-encoded pseudo-strike so rows are distinct
                    # per leg. The condor reads option_atm_iv, not this table, so a
                    # pseudo-strike here is harmless provenance.
                    pseudo_strike = float(strike_offset_of(strike))
                    rows = chain_snapshot_rows_from_side(
                        ce_raw, "ce", underlying_scrip=u.security_id,
                        underlying_seg=u.chain_seg, expiries=expiries,
                        expiry_code=expiry_code, strike=pseudo_strike,
                        rule=u.expiry_rule,
                    )
                    rows += chain_snapshot_rows_from_side(
                        pe_raw, "pe", underlying_scrip=u.security_id,
                        underlying_seg=u.chain_seg, expiries=expiries,
                        expiry_code=expiry_code, strike=pseudo_strike,
                        rule=u.expiry_rule,
                    )
                    counts["chain_rows"] += _upsert_option_chain_snapshot(rows)

    logger.info(
        "ingest_underlying[%s]: %s (%s→%s, codes=%s, %d strikes, budget_used=%d)",
        u.symbol, counts, from_date, to_date, codes, len(strikes_list), budget.used,
    )
    return counts


async def _load_expiry_dates(symbol: str, from_date: date, to_date: date) -> list[date]:
    """Read expiry_calendar for ``symbol`` covering the range. Pads the upper bound
    out by ~90 days so the rolling code N can still attach an expiry to days near
    ``to_date`` (code-N needs N expiries on/after the last day). Returns [] if the
    table is empty/unavailable — the caller then warns and skips ATM keying.
    DB-only — never called by the pure tests."""
    try:
        from sqlalchemy import text  # noqa: PLC0415

        from db import get_session  # noqa: PLC0415

        upper = to_date + timedelta(days=90)
        with get_session() as session:
            rows = session.execute(
                text(
                    "SELECT expiry_date FROM expiry_calendar "
                    "WHERE symbol = :s AND expiry_date BETWEEN :f AND :t "
                    "ORDER BY expiry_date"
                ),
                {"s": symbol, "f": from_date, "t": upper},
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.info("expiry_calendar unavailable (%s) — ATM rows will be skipped", exc)
        return []


async def _load_spot_by_day(
    security_id: int, from_date: date, to_date: date
) -> dict[date, float]:
    """Read the underlying's daily close from index_bars to attach atm_strike/
    spot_ref per day. Returns {} if unavailable (then those columns stay NULL for
    fno_derived to backfill). DB-only — never called by the pure tests."""
    try:
        from sqlalchemy import text  # noqa: PLC0415

        from db import get_session  # noqa: PLC0415

        with get_session() as session:
            rows = session.execute(
                text(
                    "SELECT time, close FROM index_bars "
                    "WHERE security_id = :sid AND timeframe = '1d' "
                    "AND time::date BETWEEN :f AND :t"
                ),
                {"sid": str(security_id), "f": from_date, "t": to_date},
            ).fetchall()
        out: dict[date, float] = {}
        for t, close in rows:
            d = t.date() if hasattr(t, "date") else t
            if close is not None:
                out[d] = float(close)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.info("index_bars spot unavailable (%s) — atm_strike/spot_ref left NULL", exc)
        return {}


# ── CLI ────────────────────────────────────────────────────────────────────────────
def _parse_codes(s: str) -> list[int]:
    """Parse a comma-separated rolling-code list ("1" or "1,2,3") into ints ≥1."""
    out = [int(p) for p in str(s).split(",") if p.strip()]
    bad = [c for c in out if c < 1]
    if bad:
        raise argparse.ArgumentTypeError(f"expiry codes are 1-based (≥1); got {bad}")
    return out or [1]


def _parse_weekday(s: str) -> int:
    """Parse a Python weekday int in range 0..6 (Mon=0..Sun=6). Rejects out-of-range
    values rather than silently wrapping them (the derivation uses %7, so an
    un-validated 9 would silently become Wednesday — a footgun)."""
    val = int(s)
    if not 0 <= val <= 6:
        raise argparse.ArgumentTypeError(
            f"--expiry-weekday must be 0..6 (Mon=0..Sun=6); got {val}"
        )
    return val


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Dhan rollingoption historical option-IV ingester (rolling expiryCode model)"
    )
    p.add_argument("--underlying", default="NIFTY", help="underlying symbol (default NIFTY)")
    p.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    p.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    p.add_argument(
        "--instrument", default=None,
        help="OPTIDX (index) or OPTSTK (stock); inferred from registry if omitted",
    )
    p.add_argument("--security-id", type=int, default=None, help="override Dhan security id")
    p.add_argument("--strike-step", type=int, default=None, help="override ATM strike step (₹)")
    p.add_argument(
        "--exchange-segment", default=None,
        help="rollingoption exchangeSegment (NSE_FNO default; BSE_FNO for SENSEX/BANKEX)",
    )
    p.add_argument(
        "--expiry-weekday", type=_parse_weekday, default=None,
        help="weekly-expiry weekday (Mon=0..Sun=6) for a non-NIFTY underlying "
             "(default = NIFTY rule: Tuesday on/after the 2026-09-01 cutover)",
    )
    p.add_argument(
        "--strikes", type=int, default=None,
        help="ATM±n strikes to capture (clamped: index ≤10, stock ≤3; default = max)",
    )
    p.add_argument(
        "--expiry-codes", type=_parse_codes, default=[1],
        help="rolling expiry codes (1-based; default 1=front; e.g. 1,2,3 for term structure)",
    )
    p.add_argument(
        "--expiry-flag", default="WEEK", choices=["WEEK", "MONTH"],
        help="WEEK (weekly series) or MONTH (monthly series); default WEEK",
    )
    p.add_argument("--interval", default="5", help="bar interval minutes (default 5)")
    p.add_argument(
        "--no-chain", action="store_true",
        help="skip per-strike option_chain_snapshot writes (ATM IV only)",
    )
    return p


async def _amain(args: argparse.Namespace) -> None:
    from config import get_config
    from core.client import DhanClient
    from db import init_db

    cfg = get_config()
    init_db(cfg.db_url)

    u = resolve_underlying(
        args.underlying,
        security_id=args.security_id,
        instrument=args.instrument,
        strike_step=args.strike_step,
        exchange_segment=args.exchange_segment,
        expiry_weekday=args.expiry_weekday,
    )
    from_d = _parse_date(args.from_date)
    to_d = _parse_date(args.to_date)
    if from_d is None or to_d is None:
        raise SystemExit("--from / --to must be YYYY-MM-DD")

    expiry_dates = await _load_expiry_dates(u.symbol, from_d, to_d)
    spot_by_day = await _load_spot_by_day(u.security_id, from_d, to_d)

    # Token via the manager (live cache → PIN/TOTP fallback), NOT the static
    # .env token — mirrors core/fno_backfill (avoids DH-901 on long runs).
    access_token = await resolve_access_token()
    async with DhanClient(
        cfg.dhan_client_id, access_token,
        proxy_url=cfg.dhan_proxy_url or None,
        proxy_categories=cfg.dhan_proxy_categories_set,
    ) as client:
        result = await ingest_underlying(
            client, u, from_d, to_d,
            strikes=args.strikes,
            interval=args.interval,
            capture_chain=not args.no_chain,
            expiry_dates=expiry_dates,
            expiry_codes=args.expiry_codes,
            expiry_flag=args.expiry_flag,
            spot_by_day=spot_by_day,
        )
    logger.info("dhan_option_history done: %s", json.dumps(result))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = _build_arg_parser().parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
