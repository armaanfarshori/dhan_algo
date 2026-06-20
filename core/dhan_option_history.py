"""Dhan rollingoption historical option-IV ingester — 5 years of REAL implied vol.

Closes the single biggest gap in the F&O research track: until now the condor
backtest used ``straddle_iv = India VIX close / 100`` as a *proxy* (VIX is a 30-day
constant-maturity index, NOT the weekly ATM straddle IV we actually trade). Dhan's
"Expired Options Data API" (``POST /v2/charts/rollingoption``, dhanhq.co/docs/v2/
expired-options-data/) exposes up to ~5 years of MINUTE-LEVEL expired-option history
*including real implied volatility* — so we can backfill the true ATM straddle IV
per expiry/day and let ``research/backtest/fno_condor.resolve_iv_source`` read it.

INDEX-AGNOSTIC by construction (see memory ``index-agnostic-fno``): the underlying is
a parameter / registry entry, NIFTY default, NEVER hardcoded into the fetch/parse path.
Index options (OPTIDX) support ATM±10 strikes; single stocks (OPTSTK) ATM±3.

Verified request body (smoke-tested → HTTP 200, real IV returned)::

    POST https://api.dhan.co/v2/charts/rollingoption
    {"exchangeSegment":"NSE_FNO","interval":"5","securityId":13,
     "instrument":"OPTIDX","expiryFlag":"WEEK"|"MONTH","expiryCode":<int>,
     "strike":"ATM","drvOptionType":"CALL"|"PUT",
     "requiredData":["open","high","low","close","iv","oi","volume"],
     "fromDate":"YYYY-MM-DD","toDate":"YYYY-MM-DD"}   # max 30-day window/call

Response shape::

    {"data": {"ce": {"iv":[..],"close":[..],"oi":[..],"volume":[..],...},
              "pe": {...}, "timestamp":[<epoch>,...]}}

(Some payloads carry the epoch array under ``data.timestamp``; others under each
side. Both are handled.)

Hard invariants (CLAUDE.md + handoff):
  • Historical reads only — never touches an order path; ADDITIVE module.
  • Live Dhan fetches refuse to run during market hours (09:15–15:30 IST weekdays)
    via the shared ``core.fno_backfill._assert_off_hours`` guard.
  • Pure parse helpers are deterministic + DB-free so they unit-test without creds
    or a database.

Run live from the trusted machine, off-hours, e.g. the full 5-year NIFTY pull::

    python -m core.dhan_option_history --underlying NIFTY \
        --from 2021-01-01 --to 2026-06-18
    python -m core.dhan_option_history --underlying NIFTY \
        --from 2021-01-01 --to 2026-06-18 --instrument OPTIDX --strikes 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

# Reuse the proven F&O helpers — token resolution, off-hours guard, IV
# normalisation, numeric coercion, ATM-strike rounding, expiry classification.
from core.fno_backfill import (
    _IST,
    _assert_off_hours,
    _normalize_iv,
    _parse_date,
    _safe_float,
    classify_expiry,
    nifty_atm_strike,
    resolve_access_token,
)

logger = logging.getLogger("dhan.option_history")

# Max history window Dhan accepts per rollingoption call.
_MAX_WINDOW_DAYS = 30
# Polite spacing between calls (charts endpoints tolerate ~1 req/s — DH-904 on bursts).
_REQ_SPACING_SEC = 1.0


# ── underlying registry (index-agnostic; NIFTY default, never hardcoded) ──────────
@dataclass(frozen=True)
class Underlying:
    """Static descriptor for one option underlying. ``security_id`` is the Dhan
    rollingoption security id (13 = NIFTY OPTIDX); ``instrument`` is OPTIDX (index)
    or OPTSTK (single stock); ``strike_step`` is the ATM rounding step (₹); and
    ``max_strikes`` caps the ATM±n fan-out (index 10, stock 3 per the API)."""

    symbol: str
    security_id: int
    instrument: str  # OPTIDX | OPTSTK
    strike_step: int
    max_strikes: int


# Index defaults — index supports ATM±10. Extend per new underlying (data-blocked
# multi-index expansion lands here). Stocks (OPTSTK) get ATM±3.
UNDERLYINGS: dict[str, Underlying] = {
    "NIFTY": Underlying("NIFTY", 13, "OPTIDX", 50, 10),
}


def resolve_underlying(
    symbol: str,
    *,
    security_id: Optional[int] = None,
    instrument: Optional[str] = None,
    strike_step: Optional[int] = None,
) -> Underlying:
    """Resolve an Underlying for ``symbol`` from the registry, with explicit
    overrides for off-registry underlyings (e.g. a single stock not yet listed).

    A registry hit is returned as-is unless overridden. For an unknown symbol the
    caller MUST supply at least ``security_id`` (and ``instrument`` — defaults to
    OPTSTK for unknowns, since indices are expected to be pre-registered)."""
    base = UNDERLYINGS.get(symbol.upper())
    if base is None and security_id is None:
        raise ValueError(
            f"Unknown underlying {symbol!r} and no --security-id given. "
            f"Known: {sorted(UNDERLYINGS)}"
        )
    inst = instrument or (base.instrument if base else "OPTSTK")
    is_index = inst == "OPTIDX"
    return Underlying(
        symbol=symbol.upper(),
        security_id=security_id if security_id is not None else base.security_id,  # type: ignore[union-attr]
        instrument=inst,
        strike_step=strike_step if strike_step is not None else (base.strike_step if base else 50),
        max_strikes=(base.max_strikes if base else (10 if is_index else 3)),
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


def strike_offsets(n: int, cap: int) -> list[int]:
    """ATM±``n`` offsets clamped to the per-instrument ``cap`` (index 10, stock 3),
    returned ATM-first then outward: [0, -1, +1, -2, +2, …]. ``n``≤0 → [0]."""
    n = min(max(int(n), 0), int(cap))
    out = [0]
    for i in range(1, n + 1):
        out.extend((-i, i))
    return out


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

    ``strike`` is "ATM" or a signed integer offset string ("-1", "2") that Dhan
    resolves to ATM±k; ``drv_option_type`` is "CALL"|"PUT"; ``expiry_flag`` is
    "WEEK"|"MONTH"; ``expiry_code`` is the expiry index (0 = nearest)."""
    return {
        "exchangeSegment": "NSE_FNO",
        "interval": interval,
        "securityId": u.security_id,
        "instrument": u.instrument,
        "expiryFlag": expiry_flag,
        "expiryCode": expiry_code,
        "strike": strike,
        "drvOptionType": drv_option_type,
        "requiredData": ["open", "high", "low", "close", "iv", "oi", "volume"],
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


# ── pure parsers (deterministic, DB-free) ─────────────────────────────────────────
def parse_rolling_side(raw: dict[str, Any], side: str) -> list[dict[str, Any]]:
    """Parse one CE/PE side of a rollingoption response into per-bar dicts.

    ``side`` is "ce" or "pe". Returns rows with keys time(UTC datetime),
    open/high/low/close (floats, may be None), iv (RAW percent as Dhan returns,
    via _safe_float — NOT normalised here), oi/volume (ints). Truncates to the
    shortest array so a partial/mismatched payload skips silently. Missing side
    or empty arrays → []."""
    data = (raw.get("data") or raw) if isinstance(raw, dict) else {}
    node = data.get(side)
    if not isinstance(node, dict):
        return []
    ts = _extract_timestamps(node, data)
    closes = node.get("close") or []
    ivs = node.get("iv") or []
    n = min(len(ts), len(closes)) if ts else 0
    if n == 0:
        return []
    opens = node.get("open") or [None] * n
    highs = node.get("high") or [None] * n
    lows = node.get("low") or [None] * n
    ois = node.get("oi") or node.get("open_interest") or [None] * n
    vols = node.get("volume") or [None] * n
    rows: list[dict[str, Any]] = []
    for i in range(n):
        try:
            t = datetime.fromtimestamp(int(ts[i]), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
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
    expiry_date: date,
    expiry_type: Optional[str],
    strike_step: int,
    spot_hint: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Collapse CE+PE minute bars for the ATM strike into one option_atm_iv row
    PER TRADING DAY (the table is one ATM sample/day/expiry — alembic 009).

    For each calendar day present we take the LAST intraday bar (closest to the
    cash close — the entry reference the condor prices off), normalise its IV to
    a fraction, and compute ``straddle_iv = mean(call_iv, put_iv)``. ``atm_strike``
    is derived from ``spot_hint`` when supplied (the underlying close is the real
    ATM anchor and is filled later by fno_derived if absent).

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

    atm_strike: Optional[int] = None
    if spot_hint is not None:
        atm_strike = nifty_atm_strike(float(spot_hint), strike_step)

    rows: list[dict[str, Any]] = []
    for d in days:
        c = ce_day.get(d)
        p = pe_day.get(d)
        call_iv = _normalize_iv(c["iv"]) if c else None
        put_iv = _normalize_iv(p["iv"]) if p else None
        if call_iv is None and put_iv is None:
            continue  # nothing real to store for this day
        straddle_iv = (
            (call_iv + put_iv) / 2
            if (call_iv is not None and put_iv is not None)
            else (call_iv if call_iv is not None else put_iv)
        )
        # Sample time = last bar's wall-clock (UTC), so option_atm_iv.time is the
        # genuine intraday capture instant, not midnight.
        sample = c or p
        rows.append(
            {
                "time": sample["time"],
                "symbol": symbol,
                "expiry_date": expiry_date,
                "expiry_type": expiry_type,
                "atm_strike": atm_strike,
                "call_iv": call_iv,
                "put_iv": put_iv,
                "straddle_iv": straddle_iv,
                "dte": max((expiry_date - d).days, 0),
                "spot_ref": float(spot_hint) if spot_hint is not None else None,
                "implied_move": None,  # derived later by core/fno_derived
            }
        )
    return rows


def chain_snapshot_rows_from_side(
    raw: dict[str, Any],
    side: str,
    *,
    underlying_scrip: int,
    expiry_date: date,
    strike: float,
) -> list[dict[str, Any]]:
    """Project one CE/PE rollingoption side into option_chain_snapshot rows
    (one per minute bar). IV stored RAW (percent) to match the snapshot table's
    convention. option_type is CE for the call side, PE for the put."""
    opt_type = "CE" if side == "ce" else "PE"
    bars = parse_rolling_side(raw, side)
    rows: list[dict[str, Any]] = []
    for b in bars:
        rows.append(
            {
                "snapshot_time": b["time"],
                "underlying_scrip": underlying_scrip,
                "underlying_seg": "NSE_FNO",
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


# ── expiry enumeration ────────────────────────────────────────────────────────────
def enumerate_expiry_codes(
    from_date: date, to_date: date, expiry_flag: str
) -> list[tuple[int, date]]:
    """Enumerate (expiry_code, expiry_date) pairs whose expiry falls in the range.

    Dhan's rollingoption keys history by ``expiryCode`` (an index, 0 = the expiry
    nearest *today*, increasing into the past for expired contracts). Since we
    cannot know the exact expiry weekday mapping offline, the live driver resolves
    expiry dates from ``expiry_calendar`` when available; this offline helper is a
    deterministic fallback that emits weekly (Thursday) / monthly (last Thursday)
    candidates so the pagination + per-window logic is unit-testable without a DB.
    Codes are 0-based, oldest-first.
    """
    weekly = expiry_flag.upper() == "WEEK"
    cands: list[date] = []
    d = from_date
    while d <= to_date:
        if d.weekday() == 3:  # Thursday (NIFTY's historical weekly expiry weekday)
            if weekly:
                cands.append(d)
            else:
                # monthly = last Thursday of the month
                nxt = d + timedelta(days=7)
                if nxt.month != d.month:
                    cands.append(d)
        d += timedelta(days=1)
    return [(i, e) for i, e in enumerate(cands)]


# ── DB upserts (reuse fno_backfill's writers verbatim) ────────────────────────────
def _upsert_atm_iv(rows: list[dict[str, Any]]) -> int:
    from core.fno_backfill import _upsert_atm_iv as _w  # lazy: keep parsers DB-free

    return _w(rows)


def _upsert_option_chain_snapshot(rows: list[dict[str, Any]]) -> int:
    from core.fno_backfill import _upsert_option_chain_snapshot as _w

    return _w(rows)


# ── live orchestration (off-hours only) ───────────────────────────────────────────
async def _fetch_side(
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
) -> dict[str, Any]:
    """POST one rollingoption leg. Returns {} on a per-window failure (tolerated)."""
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
    try:
        return await client._request("POST", "charts/rollingoption", "data", payload)
    except Exception as exc:  # noqa: BLE001 — per-window tolerance is the design
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
    req_spacing_sec: float = _REQ_SPACING_SEC,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """Backfill one underlying's option IV over [from_date, to_date].

    For every (expiry, ≤30-day window, strike-offset) it pulls the CE+PE legs,
    writes the ATM straddle into option_atm_iv, and (when ``capture_chain``) the
    per-strike bars into option_chain_snapshot. Per-window failures are tolerated
    (logged, skipped). Off-hours only.

    ``strikes`` overrides the per-instrument ATM±n fan-out (clamped to the cap).
    ``expiry_dates`` (from expiry_calendar) is preferred when supplied; otherwise
    the offline Thursday/last-Thursday fallback is used. Returns counts dict.
    """
    _assert_off_hours("ingest_underlying", now)
    n_off = strikes if strikes is not None else u.max_strikes
    offsets = strike_offsets(n_off, u.max_strikes)
    windows = date_windows(from_date, to_date)
    expiry_flag = "WEEK"  # weekly is the traded series; monthly is the last weekly

    # Resolve (expiry_code, expiry_date) pairs.
    if expiry_dates is not None:
        exp_list = sorted(set(expiry_dates))
        all_exp = list(exp_list)
        code_pairs = list(enumerate(exp_list))
    else:
        code_pairs = enumerate_expiry_codes(from_date, to_date, expiry_flag)
        all_exp = [e for _, e in code_pairs]

    counts = {"atm_rows": 0, "chain_rows": 0, "legs": 0, "windows": 0}

    for expiry_code, expiry_date in code_pairs:
        expiry_type = classify_expiry(expiry_date, all_exp)
        for w_from, w_to in windows:
            # Skip windows entirely after the expiry (no live history there).
            if w_from > expiry_date:
                continue
            counts["windows"] += 1
            for off in offsets:
                strike_param = "ATM" if off == 0 else str(off)
                ce_raw = await _fetch_side(
                    client, u, expiry_flag=expiry_flag, expiry_code=expiry_code,
                    strike=strike_param, drv_option_type="CALL",
                    from_date=w_from.isoformat(), to_date=w_to.isoformat(),
                    interval=interval,
                )
                await asyncio.sleep(req_spacing_sec)
                pe_raw = await _fetch_side(
                    client, u, expiry_flag=expiry_flag, expiry_code=expiry_code,
                    strike=strike_param, drv_option_type="PUT",
                    from_date=w_from.isoformat(), to_date=w_to.isoformat(),
                    interval=interval,
                )
                await asyncio.sleep(req_spacing_sec)
                counts["legs"] += 2

                if off == 0:
                    atm_rows = atm_iv_rows_from_legs(
                        ce_raw, pe_raw, symbol=u.symbol, expiry_date=expiry_date,
                        expiry_type=expiry_type, strike_step=u.strike_step,
                    )
                    counts["atm_rows"] += _upsert_atm_iv(atm_rows)

                if capture_chain:
                    # The actual numeric strike is unknown offline (Dhan resolves
                    # ATM±k server-side); record the offset-encoded pseudo-strike so
                    # rows are distinct per leg. The condor reads option_atm_iv, not
                    # this table, so a pseudo-strike here is harmless provenance.
                    pseudo_strike = float(off)
                    rows = chain_snapshot_rows_from_side(
                        ce_raw, "ce", underlying_scrip=u.security_id,
                        expiry_date=expiry_date, strike=pseudo_strike,
                    )
                    rows += chain_snapshot_rows_from_side(
                        pe_raw, "pe", underlying_scrip=u.security_id,
                        expiry_date=expiry_date, strike=pseudo_strike,
                    )
                    counts["chain_rows"] += _upsert_option_chain_snapshot(rows)

    logger.info(
        "ingest_underlying[%s]: %s (%s→%s, %d expiries, %d offsets)",
        u.symbol, counts, from_date, to_date, len(code_pairs), len(offsets),
    )
    return counts


async def _load_expiry_dates(symbol: str, from_date: date, to_date: date) -> Optional[list[date]]:
    """Best-effort read of expiry_calendar for ``symbol`` within range. Returns
    None if the table is empty/unavailable so the caller falls back to the offline
    Thursday enumeration. DB-only — never called by the pure tests."""
    try:
        from sqlalchemy import text  # noqa: PLC0415

        from db import get_session  # noqa: PLC0415

        with get_session() as session:
            rows = session.execute(
                text(
                    "SELECT expiry_date FROM expiry_calendar "
                    "WHERE symbol = :s AND expiry_date BETWEEN :f AND :t "
                    "ORDER BY expiry_date"
                ),
                {"s": symbol, "f": from_date, "t": to_date},
            ).fetchall()
        out = [r[0] for r in rows]
        return out or None
    except Exception as exc:  # noqa: BLE001
        logger.info("expiry_calendar unavailable (%s) — using offline enumeration", exc)
        return None


# ── CLI ────────────────────────────────────────────────────────────────────────────
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Dhan rollingoption historical option-IV ingester (off-hours only)"
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
        "--strikes", type=int, default=None,
        help="ATM±n strikes to capture (clamped: index ≤10, stock ≤3; default = max)",
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
    )
    from_d = _parse_date(args.from_date)
    to_d = _parse_date(args.to_date)
    if from_d is None or to_d is None:
        raise SystemExit("--from / --to must be YYYY-MM-DD")

    expiry_dates = await _load_expiry_dates(u.symbol, from_d, to_d)

    # Token via the manager (live cache → PIN/TOTP fallback), NOT the static
    # .env token — mirrors core/fno_backfill (avoids DH-901 on long runs).
    access_token = await resolve_access_token()
    async with DhanClient(cfg.dhan_client_id, access_token) as client:
        result = await ingest_underlying(
            client, u, from_d, to_d,
            strikes=args.strikes,
            interval=args.interval,
            capture_chain=not args.no_chain,
            expiry_dates=expiry_dates,
        )
    logger.info("dhan_option_history done: %s", json.dumps(result))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = _build_arg_parser().parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
