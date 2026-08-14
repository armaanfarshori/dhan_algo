"""F&O Phase-0 data backfill / ingest — NIFTY index options (capture-everything).

Populates the tables added in Alembic 009/010:

  • ``backfill_futures_bars``   — NIFTY index-futures OHLCV + open interest from
        Dhan's historical charts endpoint (``charts/historical`` / ``intraday``,
        NSE_FNO / FUTIDX). Real history is available here.
  • ``backfill_index_bars``     — Daily OHLCV for IDX_I index instruments (NIFTY 50
        id "13", India VIX id "21", …). Replaces the old india_vix table (dropped
        in 010); realized_vol_20d is derived later by core/fno_derived.
  • ``snapshot_option_chain``   — FULL option-chain capture: one row per
        (snapshot_time, underlying_scrip, expiry_date, strike, option_type) with
        all ltp/oi/volume/bid/ask/prev_*/IV/greeks + raw JSONB. ATM IV is also
        projected into option_atm_iv at capture time.
  • ``build_expiry_calendar``   — NIFTY expiry dates from the option-chain expiry
        list endpoint, classified weekly/monthly.

Hard invariants (CLAUDE.md + handoff):
  • Historical reads only — this module never touches an order path.
  • All *live* Dhan fetches refuse to run during market hours (09:15–15:30 IST,
    weekdays) via ``_assert_off_hours``.
  • Pure parse/extract helpers are deterministic and DB-free so they unit-test
    without creds or a database.

Run live from the trusted machine, off-hours, e.g.::

    python -m core.fno_backfill --futures --symbol NIFTY --security-id <id> \
        --from 2024-06-01 --to 2026-06-18
    python -m core.fno_backfill --index --security-id 13 --symbol NIFTY \
        --from 2024-06-01 --to 2026-06-18
    python -m core.fno_backfill --index --security-id 21 --symbol INDIAVIX \
        --from 2024-06-01 --to 2026-06-18
    python -m core.fno_backfill --expiry-calendar --symbol NIFTY
    python -m core.fno_backfill --chain --symbol NIFTY [--expiry YYYY-MM-DD]
    python -m core.fno_backfill --atm-iv --symbol NIFTY [--expiry YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("dhan.fno_backfill")

_IST = timezone(timedelta(hours=5, minutes=30))

# Underlying scrip ids for the option-chain / expiry-list endpoints (UnderlyingSeg=IDX_I).
# 13 = NIFTY 50 (confirmed via core/live_feed.py IDX_I subscription).
SYMBOL_SCRIP: dict[str, int] = {"NIFTY": 13}

# IDX_I feed security ids used by backfill_index_bars.
INDEX_SECURITY_IDS: dict[str, str] = {"NIFTY": "13", "INDIAVIX": "21"}

# ATM strike step (₹) per underlying.
SYMBOL_STRIKE_STEP: dict[str, int] = {"NIFTY": 50}

_MARKET_OPEN = time(9, 15)
_MARKET_CLOSE = time(15, 30)


# ── time / market-hours guard ───────────────────────────────────────────────────
def _now_ist() -> datetime:
    return datetime.now(_IST)


def is_market_hours(now: Optional[datetime] = None) -> bool:
    """True iff ``now`` (default: real IST now) is within NSE trading hours —
    a weekday between 09:15 and 15:30 IST inclusive."""
    now = now or _now_ist()
    # A naive datetime is assumed to already be IST (the module's native zone);
    # a tz-aware one is converted. This avoids mis-judging a naive UTC `now`
    # (e.g. on a UTC-clocked box) as IST wall-clock.
    now = now.replace(tzinfo=_IST) if now.tzinfo is None else now.astimezone(_IST)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return _MARKET_OPEN <= now.time() <= _MARKET_CLOSE


def _assert_off_hours(action: str, now: Optional[datetime] = None) -> None:
    """Refuse any live Dhan fetch during market hours (data reads are allowed
    from any IP, but the handoff mandates off-hours-only to keep this strictly
    a research/backfill path that can never interleave with live trading)."""
    if is_market_hours(now):
        raise RuntimeError(
            f"{action} refused during market hours (09:15–15:30 IST). "
            "Run F&O data jobs off-hours."
        )


# ── pure helpers (deterministic, DB-free) ───────────────────────────────────────
def nifty_atm_strike(spot: float, step: int = 50) -> int:
    """ATM strike = nearest ``step`` multiple to spot (NIFTY step = 50).

    Uses round-half-UP (exchange convention) rather than Python's round-half-even,
    so a spot exactly on a half-step (e.g. 23425) rounds to 23450, not 23400.
    """
    return int(math.floor(spot / step + 0.5)) * step


def _normalize_iv(v: Any) -> Optional[float]:
    """Normalise an IV value to a fraction. Dhan's v2 option chain returns IV in
    PERCENT (e.g. 13.5 → 0.135), so divide by 100 unconditionally. (An earlier
    >1.5 heuristic risked treating a genuine low percent reading as a fraction —
    100× wrong — so it was removed.) This is the single place to revisit if a
    live response ever shows fractional IV. Non-positive / unparseable → None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return f / 100.0


def _safe_float(v: Any) -> Optional[float]:
    """Return float(v) or None on TypeError/ValueError/None. Used by history
    parsers to guard against None or non-numeric elements in Dhan's parallel
    arrays so we never write NULL into NOT-NULL OHLC columns."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_futures_history(
    raw: dict[str, Any],
    symbol: str,
    timeframe: str = "1d",
    expiry_date: Optional[date] = None,
) -> list[dict[str, Any]]:
    """Parse a Dhan ``charts/historical|intraday`` response into futures_bars
    rows. Dhan returns parallel arrays (timestamp/open/high/low/close/volume,
    plus open_interest when ``oi=true``). Suspended/illiquid → empty → []."""
    inner = (raw.get("data") or raw) if isinstance(raw, dict) else {}
    timestamps = inner.get("timestamp") or []
    opens = inner.get("open") or []
    highs = inner.get("high") or []
    lows = inner.get("low") or []
    closes = inner.get("close") or []
    # Truncate to the shortest OHLC array length so a malformed/partial payload
    # (missing key, or mismatched array lengths) skips silently rather than
    # raising KeyError/IndexError mid-backfill. Suspended/illiquid → 0 rows.
    n = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
    if n == 0:
        return []
    volumes = inner.get("volume") or [0] * n
    ois = inner.get("open_interest") or inner.get("oi") or [None] * n
    rows: list[dict[str, Any]] = []
    for i, ts in enumerate(timestamps[:n]):
        o = _safe_float(opens[i])
        h = _safe_float(highs[i])
        lo = _safe_float(lows[i])
        c = _safe_float(closes[i])
        if any(v is None for v in (o, h, lo, c)):
            logger.debug(
                "parse_futures_history: skipping bar %d (None in OHLC): o=%s h=%s l=%s c=%s",
                i, o, h, lo, c,
            )
            continue
        rows.append(
            {
                "time": datetime.fromtimestamp(int(ts), tz=timezone.utc),
                "symbol": symbol,
                "timeframe": timeframe,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": int(_safe_float(volumes[i]) or 0),
                "open_interest": (None if ois[i] is None else int(ois[i])),
                "expiry_date": expiry_date,
            }
        )
    return rows


def parse_index_history(
    raw: dict[str, Any],
    security_id: str,
    symbol: str,
    timeframe: str = "1d",
) -> list[dict[str, Any]]:
    """Parse a Dhan ``charts/historical|intraday`` response into index_bars rows.

    Same array-parsing / guards as parse_futures_history but produces rows for
    the ``index_bars`` table: keys time(UTC), security_id, symbol, timeframe,
    open, high, low, close, volume. Note: realized_vol_20d is NOT set here —
    it is derived later by core/fno_derived.
    """
    inner = (raw.get("data") or raw) if isinstance(raw, dict) else {}
    timestamps = inner.get("timestamp") or []
    opens = inner.get("open") or []
    highs = inner.get("high") or []
    lows = inner.get("low") or []
    closes = inner.get("close") or []
    n = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
    if n == 0:
        return []
    volumes = inner.get("volume") or [0] * n
    rows: list[dict[str, Any]] = []
    for i, ts in enumerate(timestamps[:n]):
        o = _safe_float(opens[i])
        h = _safe_float(highs[i])
        lo = _safe_float(lows[i])
        c = _safe_float(closes[i])
        if any(v is None for v in (o, h, lo, c)):
            logger.debug(
                "parse_index_history: skipping bar %d (None in OHLC): o=%s h=%s l=%s c=%s",
                i, o, h, lo, c,
            )
            continue
        rows.append(
            {
                "time": datetime.fromtimestamp(int(ts), tz=timezone.utc),
                "security_id": security_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": int(_safe_float(volumes[i]) or 0),
            }
        )
    return rows


def extract_atm_iv(
    chain: dict[str, Any],
    symbol: str,
    expiry_date: date,
    expiry_type: Optional[str] = None,
    step: int = 50,
    now: Optional[datetime] = None,
    nearest: bool = False,
) -> Optional[dict[str, Any]]:
    """Pull the ATM call/put IV out of a Dhan option-chain response.

    Chain shape: ``{"data": {"last_price": <spot>, "oc": {"<strike>":
    {"ce": {"implied_volatility": .., "greeks": {..}}, "pe": {..}}, ...}}}``.
    Returns one option_atm_iv row, or None if the ATM strike is missing.
    IV is normalised to a fraction; ``straddle_iv`` = mean(call_iv, put_iv).

    ``nearest`` (default False, preserving the strict index behaviour): if the
    computed ATM strike is absent from the chain, snap to the chain strike NEAREST
    to spot instead of returning None. Enabled for stock options, whose per-
    underlying strike step is not a fixed constant.
    """
    data = (chain.get("data") or chain) if isinstance(chain, dict) else {}
    spot = data.get("last_price")
    oc = data.get("oc") or {}
    if spot is None or not oc:
        return None
    spot = float(spot)
    atm = nifty_atm_strike(spot, step)

    # Strike keys come back as zero-padded strings ("23400.000000"); match on value.
    def _find(strike: int) -> Optional[dict[str, Any]]:
        for k, v in oc.items():
            try:
                if int(round(float(k))) == strike:
                    return v
            except (TypeError, ValueError):
                continue
        return None

    node = _find(atm)
    if node is None:
        if not nearest:
            return None
        # Fallback for stock options (per-underlying strike step is not a fixed
        # constant): snap to the chain strike NEAREST to spot.
        nearest_strike: Optional[int] = None
        nearest_node: Optional[dict[str, Any]] = None
        best_dist = float("inf")
        for k, v in oc.items():
            try:
                kf = float(k)
            except (TypeError, ValueError):
                continue
            dist = abs(kf - spot)
            if dist < best_dist:
                best_dist = dist
                nearest_strike = int(round(kf))
                nearest_node = v if isinstance(v, dict) else None
        if nearest_node is None:
            return None
        atm = nearest_strike if nearest_strike is not None else atm
        node = nearest_node
    call_iv = _normalize_iv((node.get("ce") or {}).get("implied_volatility"))
    put_iv = _normalize_iv((node.get("pe") or {}).get("implied_volatility"))
    straddle_iv = (call_iv + put_iv) / 2 if (call_iv is not None and put_iv is not None) else None

    now = (now or _now_ist()).astimezone(_IST)
    dte = (expiry_date - now.date()).days
    if dte < 0:
        logger.warning("extract_atm_iv: expiry %s already past for %s", expiry_date, symbol)
        return None
    return {
        "time": now.astimezone(timezone.utc),
        "symbol": symbol,
        "expiry_date": expiry_date,
        "expiry_type": expiry_type,
        "atm_strike": atm,
        "call_iv": call_iv,
        "put_iv": put_iv,
        "straddle_iv": straddle_iv,
        "dte": dte,
        "spot_ref": spot,
        "implied_move": None,  # filled by core/fno_derived.compute_implied_move
    }


def parse_option_chain(
    chain: dict[str, Any],
    underlying_scrip: int,
    underlying_seg: str,
    expiry_date: date,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Parse a Dhan option-chain response into option_chain_snapshot rows.

    Chain shape: ``{"data": {"last_price": <spot>, "oc": {"<strike>":
    {"ce": {...}, "pe": {...}}, ...}}}``.

    For EVERY strike and EVERY side present (ce, pe) emits one row capturing ALL
    fields. Missing fields → None; never raises on a partial node. If ``data``
    is null or an error envelope, returns [].

    IV is stored RAW (percent as Dhan returns it) — NOT normalised here.
    Normalisation is done downstream (extract_atm_iv / fno_derived).
    """
    if not isinstance(chain, dict):
        return []
    data = chain.get("data")
    if not data or not isinstance(data, dict):
        return []
    oc = data.get("oc") or {}
    if not oc:
        return []

    spot = data.get("last_price")
    snapshot_time = (now or _now_ist()).astimezone(timezone.utc)

    rows: list[dict[str, Any]] = []
    for strike_key, sides in oc.items():
        try:
            strike = float(strike_key)
        except (TypeError, ValueError):
            continue
        if not isinstance(sides, dict):
            continue
        for opt_type, node in (("CE", sides.get("ce")), ("PE", sides.get("pe"))):
            if node is None:
                continue
            if not isinstance(node, dict):
                continue
            greeks = node.get("greeks") or {}

            def _f(key: str) -> Optional[float]:
                v = node.get(key)
                if v is None:
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            def _i(key: str) -> Optional[int]:
                v = node.get(key)
                if v is None:
                    return None
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None

            def _gf(key: str) -> Optional[float]:
                v = greeks.get(key)
                if v is None:
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            rows.append(
                {
                    "snapshot_time": snapshot_time,
                    "underlying_scrip": underlying_scrip,
                    "underlying_seg": underlying_seg,
                    "expiry_date": expiry_date,
                    "strike": strike,
                    "option_type": opt_type,
                    "security_id": node.get("security_id"),
                    "ltp": _f("last_price"),
                    "prev_close": _f("previous_close_price"),
                    "volume": _i("volume"),
                    "oi": _i("oi"),
                    "prev_oi": _i("previous_oi"),
                    "prev_volume": _i("previous_volume"),
                    "top_bid_price": _f("top_bid_price"),
                    "top_ask_price": _f("top_ask_price"),
                    "top_bid_qty": _i("top_bid_quantity"),
                    "top_ask_qty": _i("top_ask_quantity"),
                    "iv": _f("implied_volatility"),  # raw percent, not normalised
                    "delta": _gf("delta"),
                    "theta": _gf("theta"),
                    "gamma": _gf("gamma"),
                    "vega": _gf("vega"),
                    "spot": float(spot) if spot is not None else None,
                    "raw": node,
                }
            )
    return rows


async def resolve_access_token() -> str:
    """Obtain a Dhan access token for the F&O data path.

    Mirrors apps/trader.py / apps/api.py: prefer the live runtime cache that
    dhan-trader maintains (``read_current_token()`` → dhan_token.json), and only
    if that is missing/expired fall back to ``MasterTokenManager().load_or_generate()``
    (PIN + TOTP). This replaces reading the STATIC ``cfg.dhan_access_token`` from
    .env, which expires and caused DH-901 on the F&O backfill/collector path.

    The token value is never logged. Raises whatever load_or_generate raises if
    no cache exists and generation fails (no creds, network down, etc.).
    """
    from core.token_manager import MasterTokenManager, read_current_token

    cached = read_current_token()
    if cached:
        return cached
    logger.info("No valid cached token — generating via PIN + TOTP")
    return await MasterTokenManager().load_or_generate()


def classify_expiry(expiry: date, all_expiries: Iterable[date]) -> str:
    """A monthly expiry is the last expiry within its calendar month; anything
    else is weekly. Derived from the actual expiry set (NIFTY's weekly expiry
    weekday changed in 2024–25, so never assume a fixed day)."""
    all_expiries = list(all_expiries)
    same_month = [e for e in all_expiries if e.year == expiry.year and e.month == expiry.month]
    return "monthly" if same_month and expiry == max(same_month) else "weekly"


def _parse_date(s: Any) -> Optional[date]:
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ── DB upserts (mirror backfill.py: get_session + execute_values) ────────────────
def _execute_values(sql: str, rows: list[tuple]) -> int:
    if not rows:
        return 0
    from psycopg2.extras import execute_values

    from db import get_session

    with get_session() as session:
        cur = session.connection().connection.cursor()
        execute_values(cur, sql, rows, page_size=1000)
        cur.close()
    return len(rows)


def _upsert_futures_bars(rows: list[dict[str, Any]]) -> int:
    sql = (
        "INSERT INTO futures_bars "
        "(time, symbol, timeframe, open, high, low, close, volume, open_interest, expiry_date) "
        "VALUES %s ON CONFLICT (symbol, timeframe, time) DO UPDATE SET "
        "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close, "
        "volume=EXCLUDED.volume, open_interest=EXCLUDED.open_interest, "
        "expiry_date=EXCLUDED.expiry_date"
    )
    tuples = [
        (
            r["time"], r["symbol"], r["timeframe"], r["open"], r["high"], r["low"],
            r["close"], r["volume"], r["open_interest"], r["expiry_date"],
        )
        for r in rows
    ]
    return _execute_values(sql, tuples)


def _upsert_index_bars(rows: list[dict[str, Any]]) -> int:
    """Upsert rows into index_bars. Conflict key: (security_id, timeframe, time).
    realized_vol_20d is intentionally excluded — filled by core/fno_derived."""
    sql = (
        "INSERT INTO index_bars "
        "(time, security_id, symbol, timeframe, open, high, low, close, volume) "
        "VALUES %s ON CONFLICT (security_id, timeframe, time) DO UPDATE SET "
        "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, "
        "close=EXCLUDED.close, volume=EXCLUDED.volume"
    )
    tuples = [
        (
            r["time"], r["security_id"], r["symbol"], r["timeframe"],
            r["open"], r["high"], r["low"], r["close"], r["volume"],
        )
        for r in rows
    ]
    return _execute_values(sql, tuples)


def _upsert_atm_iv(rows: list[dict[str, Any]]) -> int:
    sql = (
        "INSERT INTO option_atm_iv "
        "(time, symbol, expiry_date, expiry_type, atm_strike, call_iv, put_iv, "
        "straddle_iv, dte, spot_ref, implied_move) VALUES %s "
        "ON CONFLICT (symbol, expiry_date, time) DO UPDATE SET "
        "expiry_type=EXCLUDED.expiry_type, atm_strike=EXCLUDED.atm_strike, "
        "call_iv=EXCLUDED.call_iv, put_iv=EXCLUDED.put_iv, "
        "straddle_iv=EXCLUDED.straddle_iv, dte=EXCLUDED.dte, spot_ref=EXCLUDED.spot_ref"
    )
    tuples = [
        (
            r["time"], r["symbol"], r["expiry_date"], r["expiry_type"], r["atm_strike"],
            r["call_iv"], r["put_iv"], r["straddle_iv"], r["dte"], r["spot_ref"],
            r["implied_move"],
        )
        for r in rows
    ]
    return _execute_values(sql, tuples)


def _upsert_option_chain_snapshot(rows: list[dict[str, Any]]) -> int:
    """Upsert full option-chain rows. Conflict key:
    (snapshot_time, underlying_scrip, expiry_date, strike, option_type).
    raw is serialised to JSON and cast to ::jsonb server-side."""
    sql = (
        "INSERT INTO option_chain_snapshot "
        "(snapshot_time, underlying_scrip, underlying_seg, expiry_date, strike, option_type, "
        "security_id, ltp, prev_close, volume, oi, prev_oi, prev_volume, "
        "top_bid_price, top_ask_price, top_bid_qty, top_ask_qty, "
        "iv, delta, theta, gamma, vega, spot, raw) "
        "VALUES %s ON CONFLICT (snapshot_time, underlying_scrip, expiry_date, strike, option_type) "
        "DO UPDATE SET "
        "underlying_seg=EXCLUDED.underlying_seg, "
        "security_id=EXCLUDED.security_id, ltp=EXCLUDED.ltp, prev_close=EXCLUDED.prev_close, "
        "volume=EXCLUDED.volume, oi=EXCLUDED.oi, prev_oi=EXCLUDED.prev_oi, "
        "prev_volume=EXCLUDED.prev_volume, "
        "top_bid_price=EXCLUDED.top_bid_price, top_ask_price=EXCLUDED.top_ask_price, "
        "top_bid_qty=EXCLUDED.top_bid_qty, top_ask_qty=EXCLUDED.top_ask_qty, "
        "iv=EXCLUDED.iv, delta=EXCLUDED.delta, theta=EXCLUDED.theta, "
        "gamma=EXCLUDED.gamma, vega=EXCLUDED.vega, spot=EXCLUDED.spot, raw=EXCLUDED.raw"
    )
    tuples = [
        (
            r["snapshot_time"], r["underlying_scrip"], r["underlying_seg"],
            r["expiry_date"], r["strike"], r["option_type"],
            r["security_id"], r["ltp"], r["prev_close"],
            r["volume"], r["oi"], r["prev_oi"], r["prev_volume"],
            r["top_bid_price"], r["top_ask_price"], r["top_bid_qty"], r["top_ask_qty"],
            r["iv"], r["delta"], r["theta"], r["gamma"], r["vega"],
            r["spot"],
            json.dumps(r["raw"]) if r["raw"] is not None else None,
        )
        for r in rows
    ]
    # raw column needs ::jsonb cast — use a template
    template = (
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)"
    )
    if not tuples:
        return 0
    from psycopg2.extras import execute_values

    from db import get_session

    with get_session() as session:
        cur = session.connection().connection.cursor()
        execute_values(cur, sql, tuples, template=template, page_size=500)
        cur.close()
    return len(tuples)


def _upsert_expiry_calendar(rows: list[dict[str, Any]]) -> int:
    sql = (
        "INSERT INTO expiry_calendar (symbol, expiry_date, expiry_type) VALUES %s "
        "ON CONFLICT (symbol, expiry_date) DO UPDATE SET expiry_type=EXCLUDED.expiry_type"
    )
    return _execute_values(sql, [(r["symbol"], r["expiry_date"], r["expiry_type"]) for r in rows])


# ── orchestration (live Dhan fetches — off-hours only) ───────────────────────────
async def backfill_futures_bars(
    client: Any,
    symbol: str,
    security_id: str,
    from_date: str,
    to_date: str,
    *,
    timeframe: str = "1d",
    instrument: str = "FUTIDX",
    expiry_date: Optional[date] = None,
    now: Optional[datetime] = None,
) -> int:
    """Fetch NSE_FNO futures OHLCV+OI for one contract and upsert into
    futures_bars. ``security_id`` is the futures contract id (from the
    instruments master). ``instrument`` selects the contract class —
    ``"FUTIDX"`` (index futures, default) or ``"FUTSTK"`` (single-stock
    futures, for the equity F&O universe). Off-hours only.

    WARNING: each ``symbol`` must be ONE continuous/front-month series — writing
    two different physical expiry contracts under the same symbol collides on the
    PK (symbol, timeframe, time) and silently contaminates realized_vol_20d
    across roll gaps. For raw per-contract storage use distinct symbol values
    (e.g. ``NIFTY-2406``, ``NIFTY-2407``). This is Open Q#2 from
    docs/fno-handoff.md; the schema is unchanged.
    """
    _assert_off_hours("backfill_futures_bars", now)
    raw = await client.get_daily_historical(
        security_id=security_id,
        exchange_segment="NSE_FNO",
        instrument=instrument,
        from_date=from_date,
        to_date=to_date,
    )
    rows = parse_futures_history(raw, symbol, timeframe, expiry_date)
    n = _upsert_futures_bars(rows)
    logger.info("futures_bars: upserted %d rows for %s (%s→%s)", n, symbol, from_date, to_date)
    return n


async def backfill_index_bars(
    client: Any,
    security_id: str,
    symbol: str,
    from_date: str,
    to_date: str,
    *,
    timeframe: str = "1d",
    now: Optional[datetime] = None,
) -> int:
    """Fetch IDX_I index OHLCV for one index instrument and upsert into index_bars.

    Used for NIFTY 50 (security_id="13") and India VIX (security_id="21").
    Off-hours only. realized_vol_20d is NOT set here — derived by core/fno_derived.
    """
    _assert_off_hours("backfill_index_bars", now)
    raw = await client.get_daily_historical(
        security_id=security_id,
        exchange_segment="IDX_I",
        instrument="INDEX",
        from_date=from_date,
        to_date=to_date,
    )
    rows = parse_index_history(raw, security_id, symbol, timeframe)
    n = _upsert_index_bars(rows)
    logger.info("index_bars: upserted %d rows for %s/%s (%s→%s)", n, symbol, security_id, from_date, to_date)
    return n


async def snapshot_option_chain(
    client: Any,
    symbol: str = "NIFTY",
    *,
    underlying_scrip: Optional[int] = None,
    underlying_seg: str = "IDX_I",
    expiry_date: Optional[date] = None,
    expiry_type: Optional[str] = None,
    now: Optional[datetime] = None,
    allow_market_hours: bool = False,
) -> dict[str, Any]:
    """Pull the LIVE option chain for one expiry, capture ALL rows into
    option_chain_snapshot, and also project the ATM IV into option_atm_iv.

    If ``expiry_date`` is None, the nearest upcoming expiry is picked via
    ``client.get_fno_expiry_list``. Off-hours only by default; pass
    ``allow_market_hours=True`` for the intraday forward-paper loop (the Dhan
    chain endpoint is a data read that works during market hours).

    Returns ``{"chain_rows": n, "atm": 0|1}``.
    """
    if not allow_market_hours:
        _assert_off_hours("snapshot_option_chain", now)
    # Capture now once so snapshot_time and option_atm_iv.time are identical.
    now = now or _now_ist()
    scrip = underlying_scrip if underlying_scrip is not None else SYMBOL_SCRIP[symbol]
    step = SYMBOL_STRIKE_STEP.get(symbol, 50)

    if expiry_date is None:
        raw_expiries = await client.get_fno_expiry_list(scrip, underlying_seg)
        data = raw_expiries.get("data", raw_expiries) if isinstance(raw_expiries, dict) else raw_expiries
        parsed = sorted({d for d in (_parse_date(x) for x in (data or [])) if d is not None})
        today = now.astimezone(_IST).date()
        future = [d for d in parsed if d >= today]
        if not future:
            logger.warning(
                "snapshot_option_chain: no future expiries for %s (today=%s, all=%s)",
                symbol, today, parsed,
            )
            return {"chain_rows": 0, "atm": 0}
        expiry_date = min(future)

    chain = await client.get_fno_option_chain(scrip, expiry_date.isoformat(), underlying_seg)

    # Full capture — pass the same `now` so snapshot_time is consistent.
    snap_rows = parse_option_chain(chain, scrip, underlying_seg, expiry_date, now)
    n = _upsert_option_chain_snapshot(snap_rows)
    logger.info(
        "option_chain_snapshot: upserted %d rows for %s %s", n, symbol, expiry_date
    )

    # Project ATM IV into option_atm_iv (keep that table current) — same `now`.
    # Stock options (NSE_FNO) have a per-underlying strike step, so snap ATM to the
    # nearest available strike; index chains keep the strict computed-strike match.
    atm_row = extract_atm_iv(
        chain, symbol, expiry_date, expiry_type, step, now,
        nearest=(underlying_seg == "NSE_FNO"),
    )
    atm_count = 0
    if atm_row is not None:
        _upsert_atm_iv([atm_row])
        atm_count = 1
    else:
        logger.warning("snapshot_option_chain: no ATM node for %s %s", symbol, expiry_date)

    return {"chain_rows": n, "atm": atm_count}


async def build_expiry_calendar(
    client: Any,
    symbol: str,
    *,
    underlying_scrip: Optional[int] = None,
    underlying_seg: str = "IDX_I",
    now: Optional[datetime] = None,
) -> int:
    """Fetch the live NIFTY expiry list, classify weekly/monthly, upsert
    expiry_calendar. Off-hours only."""
    _assert_off_hours("build_expiry_calendar", now)
    scrip = underlying_scrip if underlying_scrip is not None else SYMBOL_SCRIP[symbol]
    raw = await client.get_fno_expiry_list(scrip, underlying_seg)
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    expiries = sorted({d for d in (_parse_date(x) for x in (data or [])) if d is not None})
    rows = [
        {"symbol": symbol, "expiry_date": e, "expiry_type": classify_expiry(e, expiries)}
        for e in expiries
    ]
    n = _upsert_expiry_calendar(rows)
    logger.info("expiry_calendar: upserted %d expiries for %s", n, symbol)
    return n


# ── CLI ──────────────────────────────────────────────────────────────────────────
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="F&O Phase-0 data backfill (off-hours only)")
    p.add_argument("--symbol", default="NIFTY")
    p.add_argument("--futures", action="store_true", help="backfill futures bars")
    p.add_argument(
        "--instrument", default="FUTIDX",
        help="futures instrument class for --futures: FUTIDX (default) or FUTSTK (stock)",
    )
    p.add_argument("--security-id", help="security id (for --futures and --index)")
    p.add_argument("--from", dest="from_date")
    p.add_argument("--to", dest="to_date")
    p.add_argument(
        "--index", action="store_true",
        help="backfill index bars (IDX_I); requires --security-id, --from, --to",
    )
    p.add_argument("--expiry-calendar", action="store_true", help="build expiry calendar")
    p.add_argument(
        "--chain", action="store_true",
        help="snapshot full option chain into option_chain_snapshot (post-close only)",
    )
    p.add_argument(
        "--atm-iv", action="store_true",
        help="snapshot ATM IV into option_atm_iv (delegates to snapshot_option_chain)",
    )
    p.add_argument("--expiry", help="expiry date YYYY-MM-DD (for --chain / --atm-iv)")
    return p


async def _amain(args: argparse.Namespace) -> None:
    from config import get_config
    from core.client import DhanClient
    from db import init_db

    cfg = get_config()
    init_db(cfg.db_url)
    # Token via the manager (live cache → PIN/TOTP fallback), NOT the static
    # .env access token, which expires and triggers DH-901 on long-running jobs.
    access_token = await resolve_access_token()
    async with DhanClient(
        cfg.dhan_client_id, access_token,
        proxy_url=cfg.dhan_proxy_url or None,
        proxy_categories=cfg.dhan_proxy_categories_set,
    ) as client:
        if args.futures:
            if not args.security_id:
                raise SystemExit("--futures requires --security-id")
            if not args.from_date or not args.to_date:
                raise SystemExit("--futures requires --from and --to")
            await backfill_futures_bars(
                client, args.symbol, args.security_id, args.from_date, args.to_date,
                instrument=args.instrument,
            )
        if args.index:
            if not args.security_id:
                raise SystemExit("--index requires --security-id")
            if not args.from_date or not args.to_date:
                raise SystemExit("--index requires --from and --to")
            await backfill_index_bars(
                client, args.security_id, args.symbol, args.from_date, args.to_date
            )
        if args.expiry_calendar:
            await build_expiry_calendar(client, args.symbol)
        if args.chain or args.atm_iv:
            exp = _parse_date(args.expiry) if args.expiry else None
            result = await snapshot_option_chain(client, args.symbol, expiry_date=exp)
            logger.info("snapshot_option_chain result: %s", result)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = _build_arg_parser().parse_args()
    if args.futures or args.index or args.chain or args.atm_iv or args.expiry_calendar:
        asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
