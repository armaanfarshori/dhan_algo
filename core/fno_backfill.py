"""F&O Phase-0 data backfill / ingest — NIFTY index options.

Populates the four tables added in Alembic 009 (see docs/fno-handoff.md):

  • ``backfill_futures_bars``  — NIFTY index-futures OHLCV + open interest from
        Dhan's historical charts endpoint (``charts/historical`` / ``intraday``,
        NSE_FNO / FUTIDX). Real history is available here.
  • ``snapshot_atm_iv``        — one ATM-straddle IV row per expiry, pulled from
        the LIVE option-chain endpoint. **Dhan exposes no historical option
        chain / IV** (verified against the v2 docs, 2026-06-19), so this is a
        going-forward EOD collector (run post-close via cron), not a backfill.
        Historical IV would have to be derived (Black-76 on historical option
        OHLCV) — deferred; see Open Q#1 in docs/fno-handoff.md.
  • ``ingest_india_vix``       — India VIX daily OHLC from an NSE public CSV
        (no API quota). Pure file ingest.
  • ``build_expiry_calendar``  — NIFTY expiry dates from the option-chain expiry
        list endpoint, classified weekly/monthly.

Hard invariants (CLAUDE.md + handoff):
  • Historical reads only — this module never touches an order path.
  • All *live* Dhan fetches refuse to run during market hours (09:15–15:30 IST,
    weekdays) via ``_assert_off_hours``. The ATM-IV snapshot is designed to run
    just after close. India-VIX CSV ingest is a local file op (no guard needed).
  • Pure parse/extract helpers are deterministic and DB-free so they unit-test
    without creds or a database.

Run live from the trusted machine, off-hours, e.g.::

    python -m core.fno_backfill --futures --symbol NIFTY --security-id <id> \
        --from 2024-06-01 --to 2026-06-18
    python -m core.fno_backfill --expiry-calendar --symbol NIFTY
    python -m core.fno_backfill --atm-iv --symbol NIFTY          # post-close only
    python -m core.fno_backfill --india-vix path/to/india_vix.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("dhan.fno_backfill")

_IST = timezone(timedelta(hours=5, minutes=30))

# Underlying scrip ids for the option-chain / expiry-list endpoints (UnderlyingSeg=IDX_I).
# 13 = NIFTY 50 (confirmed via core/live_feed.py IDX_I subscription).
SYMBOL_SCRIP: dict[str, int] = {"NIFTY": 13}

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
    if now.tzinfo is not None:
        now = now.astimezone(_IST)
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
    """ATM strike = nearest ``step`` multiple to spot (NIFTY step = 50)."""
    return int(round(spot / step) * step)


def _normalize_iv(v: Any) -> Optional[float]:
    """Normalise an IV value to a fraction. Dhan returns IV in percent
    (e.g. 13.5 → 0.135); pass through values that already look like fractions."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return f / 100.0 if f > 1.5 else f


def parse_futures_history(
    raw: dict[str, Any],
    symbol: str,
    timeframe: str = "1d",
    expiry_date: Optional[date] = None,
) -> list[dict[str, Any]]:
    """Parse a Dhan ``charts/historical|intraday`` response into futures_bars
    rows. Dhan returns parallel arrays (timestamp/open/high/low/close/volume,
    plus open_interest when ``oi=true``). Suspended/illiquid → empty → []."""
    inner = raw.get("data", raw) if isinstance(raw, dict) else {}
    timestamps = inner.get("timestamp") or []
    if not timestamps:
        return []
    opens = inner["open"]
    highs = inner["high"]
    lows = inner["low"]
    closes = inner["close"]
    volumes = inner.get("volume") or [0] * len(timestamps)
    ois = inner.get("open_interest") or inner.get("oi") or [None] * len(timestamps)
    rows: list[dict[str, Any]] = []
    for i, ts in enumerate(timestamps):
        rows.append(
            {
                "time": datetime.fromtimestamp(int(ts), tz=timezone.utc),
                "symbol": symbol,
                "timeframe": timeframe,
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
                "volume": int(volumes[i] or 0),
                "open_interest": (None if ois[i] is None else int(ois[i])),
                "expiry_date": expiry_date,
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
) -> Optional[dict[str, Any]]:
    """Pull the ATM call/put IV out of a Dhan option-chain response.

    Chain shape: ``{"data": {"last_price": <spot>, "oc": {"<strike>":
    {"ce": {"implied_volatility": .., "greeks": {..}}, "pe": {..}}, ...}}}``.
    Returns one option_atm_iv row, or None if the ATM strike is missing.
    IV is normalised to a fraction; ``straddle_iv`` = mean(call_iv, put_iv).
    """
    data = chain.get("data", chain) if isinstance(chain, dict) else {}
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
        return None
    call_iv = _normalize_iv((node.get("ce") or {}).get("implied_volatility"))
    put_iv = _normalize_iv((node.get("pe") or {}).get("implied_volatility"))
    ivs = [x for x in (call_iv, put_iv) if x is not None]
    straddle_iv = sum(ivs) / len(ivs) if ivs else None

    now = (now or _now_ist()).astimezone(_IST)
    dte = (expiry_date - now.date()).days
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


def parse_india_vix_csv(text: str) -> list[dict[str, Any]]:
    """Parse an NSE India-VIX history CSV into india_vix rows.

    NSE's CSV columns vary by export; we match case/space-insensitively on
    Date + Close (High/Low optional). Date is ``DD-MMM-YYYY`` or ``YYYY-MM-DD``.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    norm = {f: f.strip().lower().replace(" ", "").replace("_", "") for f in reader.fieldnames}

    def col(*names: str) -> Optional[str]:
        for orig, n in norm.items():
            if n in names:
                return orig
        return None

    c_date = col("date")
    c_close = col("close", "closingvalue", "vixclose")
    c_high = col("high", "highvalue")
    c_low = col("low", "lowvalue")
    if not c_date or not c_close:
        raise ValueError(f"India VIX CSV missing Date/Close columns: {reader.fieldnames}")

    rows: list[dict[str, Any]] = []
    for r in reader:
        d = _parse_date(r[c_date])
        if d is None:
            continue
        rows.append(
            {
                "time": datetime.combine(d, time(0, 0), tzinfo=_IST).astimezone(timezone.utc),
                "close": float(str(r[c_close]).replace(",", "")),
                "high": _opt_float(r.get(c_high)) if c_high else None,
                "low": _opt_float(r.get(c_low)) if c_low else None,
            }
        )
    return rows


def classify_expiry(expiry: date, all_expiries: Iterable[date]) -> str:
    """A monthly expiry is the last expiry within its calendar month; anything
    else is weekly. Derived from the actual expiry set (NIFTY's weekly expiry
    weekday changed in 2024–25, so never assume a fixed day)."""
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


def _opt_float(v: Any) -> Optional[float]:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
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


def _upsert_india_vix(rows: list[dict[str, Any]]) -> int:
    sql = (
        "INSERT INTO india_vix (time, close, high, low) VALUES %s "
        "ON CONFLICT (time) DO UPDATE SET "
        "close=EXCLUDED.close, high=EXCLUDED.high, low=EXCLUDED.low"
    )
    return _execute_values(sql, [(r["time"], r["close"], r["high"], r["low"]) for r in rows])


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
    expiry_date: Optional[date] = None,
    now: Optional[datetime] = None,
) -> int:
    """Fetch NSE_FNO index-futures OHLCV+OI for one contract and upsert into
    futures_bars. ``security_id`` is the futures contract id (from the
    instruments master). Off-hours only."""
    _assert_off_hours("backfill_futures_bars", now)
    raw = await client.get_daily_historical(
        security_id=security_id,
        exchange_segment="NSE_FNO",
        instrument="FUTIDX",
        from_date=from_date,
        to_date=to_date,
    )
    rows = parse_futures_history(raw, symbol, timeframe, expiry_date)
    n = _upsert_futures_bars(rows)
    logger.info("futures_bars: upserted %d rows for %s (%s→%s)", n, symbol, from_date, to_date)
    return n


async def snapshot_atm_iv(
    client: Any,
    symbol: str,
    expiry_date: date,
    *,
    expiry_type: Optional[str] = None,
    underlying_scrip: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    """Pull the LIVE option chain for one expiry, extract ATM IV, upsert one
    option_atm_iv row. Intended to run post-close (EOD snapshot). Off-hours only
    — Dhan has no historical option chain, so this only ever samples forward."""
    _assert_off_hours("snapshot_atm_iv", now)
    scrip = underlying_scrip if underlying_scrip is not None else SYMBOL_SCRIP[symbol]
    step = SYMBOL_STRIKE_STEP.get(symbol, 50)
    chain = await client.get_fno_option_chain(scrip, expiry_date.isoformat())
    row = extract_atm_iv(chain, symbol, expiry_date, expiry_type, step, now)
    if row is None:
        logger.warning("snapshot_atm_iv: no ATM node for %s %s", symbol, expiry_date)
        return 0
    return _upsert_atm_iv([row])


def ingest_india_vix(csv_source: str | Path) -> int:
    """Ingest India VIX daily OHLC from an NSE CSV (path or raw CSV text)."""
    text = (
        Path(csv_source).read_text()
        if (isinstance(csv_source, Path) or (isinstance(csv_source, str) and "\n" not in csv_source and Path(csv_source).exists()))
        else str(csv_source)
    )
    rows = parse_india_vix_csv(text)
    n = _upsert_india_vix(rows)
    logger.info("india_vix: upserted %d rows", n)
    return n


async def build_expiry_calendar(
    client: Any,
    symbol: str,
    *,
    underlying_scrip: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    """Fetch the live NIFTY expiry list, classify weekly/monthly, upsert
    expiry_calendar. Off-hours only."""
    _assert_off_hours("build_expiry_calendar", now)
    scrip = underlying_scrip if underlying_scrip is not None else SYMBOL_SCRIP[symbol]
    raw = await client.get_fno_expiry_list(scrip)
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
    p.add_argument("--futures", action="store_true", help="backfill index-futures bars")
    p.add_argument("--security-id", help="futures contract security id (for --futures)")
    p.add_argument("--from", dest="from_date")
    p.add_argument("--to", dest="to_date")
    p.add_argument("--atm-iv", action="store_true", help="snapshot ATM IV (post-close)")
    p.add_argument("--expiry", help="expiry date YYYY-MM-DD (for --atm-iv)")
    p.add_argument("--expiry-calendar", action="store_true", help="build expiry calendar")
    p.add_argument("--india-vix", metavar="CSV", help="ingest India VIX CSV path")
    return p


async def _amain(args: argparse.Namespace) -> None:
    from config import get_config
    from core.client import DhanClient
    from db import init_db

    cfg = get_config()
    init_db(cfg.db_url)
    async with DhanClient() as client:
        if args.futures:
            if not args.security_id:
                raise SystemExit("--futures requires --security-id")
            await backfill_futures_bars(
                client, args.symbol, args.security_id, args.from_date, args.to_date
            )
        if args.expiry_calendar:
            await build_expiry_calendar(client, args.symbol)
        if args.atm_iv:
            exp = _parse_date(args.expiry)
            if exp is None:
                raise SystemExit("--atm-iv requires --expiry YYYY-MM-DD")
            await snapshot_atm_iv(client, args.symbol, exp)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = _build_arg_parser().parse_args()
    if args.india_vix:
        ingest_india_vix(args.india_vix)  # local file op — no client/off-hours guard
    if args.futures or args.atm_iv or args.expiry_calendar:
        asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
