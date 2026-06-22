"""
NSE Volatility Screener
=======================
Dynamically selects the top-N most volatile / active NSE equity securities.

Two modes:
  1. Historical (default, pre-market safe):
     Queries the bars hypertable and ranks by ATR % over the last N days.
     Works any time — no live market connection needed.

  2. Live (intraday, requires Dhan API):
     Fetches today's OHLC for the Nifty 500 universe from Dhan and ranks
     by today's price change % or volume. Best during market hours.

Usage:
    from core.nse_screener import get_top_volatile, get_top_live

    # Pre-market: top 20 by 30-day ATR%
    securities = get_top_volatile(n=20)

    # Intraday: top 20 by today's move %
    securities = asyncio.run(get_top_live(client, n=20))

Each returns a list of dicts:
    [{"security_id": "2885", "score": 0.0234, "reason": "ATR%=2.34%"}, ...]
"""

import logging
from typing import Any

from sqlalchemy import text

logger = logging.getLogger("dhan.screener")

_DEFAULT_N        = 20
_DEFAULT_LOOKBACK = 30   # trading days for ATR calculation
_MIN_TRADING_DAYS = 10   # skip instruments with sparse data
_UNIVERSE_LIMIT   = 500  # pull from top N instruments by coverage, then re-rank


# ── Historical volatility screener (pre-market safe) ──────────────────────────

def get_top_volatile(
    n: int = _DEFAULT_N,
    lookback_days: int = _DEFAULT_LOOKBACK,
    segment: str = "NSE_EQ",
    min_avg_volume: int = 50_000,
    min_price: float = 200.0,
) -> list[dict[str, Any]]:
    """
    Rank NSE equity securities by average daily ATR % over the last lookback_days.
    Filters out illiquid stocks (avg volume < min_avg_volume shares/day) and
    penny stocks (avg close < min_price — paper fills are fantasy when one
    tick is several bps, and ATR% naturally over-ranks low-priced names).

    Returns top-n sorted by volatility descending.
    """
    from db import get_session

    # Ranks from DAILY bars — the backfill writes both 1m and 1d. The old
    # 1-minute aggregation scanned millions of rows and ran into its 20s
    # statement timeout whenever the backfill was writing; the queued-up
    # queries starved the api's thread executor and froze dashboard file
    # serving. Identical semantics, milliseconds instead of seconds.
    sql = text("""
        SELECT b.security_id,
               AVG((b.high - b.low) / NULLIF(b.close, 0)) AS atr_pct,
               AVG(b.volume)                              AS avg_volume,
               AVG(b.close)                               AS avg_close,
               COUNT(*)                                   AS trading_days
        FROM bars b
        JOIN instruments i ON i.security_id = b.security_id
        WHERE b.timeframe = '1d'
          AND b.time >= NOW() - (:lookback * INTERVAL '1 day')
          AND i.exchange_segment = :seg
          AND i.instrument_type  = 'EQUITY'
        GROUP BY b.security_id
        HAVING COUNT(*) >= :min_days
           AND AVG(b.volume) >= :min_vol
           AND AVG(b.close)  >= :min_price
        ORDER BY atr_pct DESC
        LIMIT :n
    """)

    try:
        with get_session() as session:
            session.execute(text("SET LOCAL statement_timeout = '20000'"))
            rows = session.execute(sql, {
                "lookback":  lookback_days * 2,  # calendar days (2× to cover weekends)
                "seg":       segment,
                "min_days":  _MIN_TRADING_DAYS,
                "min_vol":   min_avg_volume,
                "min_price": min_price,
                "n":         n,
            }).fetchall()
    except Exception as exc:
        logger.warning("Screener query timed out or failed (%s) — returning empty", exc)
        return []

    result = []
    for r in rows:
        result.append({
            "security_id": r[0],
            "score":       round(float(r[1]), 6),   # ATR%
            "avg_volume":  int(r[2]),
            "avg_close":   round(float(r[3]), 2),
            "days":        int(r[4]),
            "reason":      f"ATR%={float(r[1])*100:.2f}%  avgVol={int(r[2]):,}",
        })

    logger.info(
        "Screener [historical]: top %d by ATR%% over %d days — "
        "highest: %s (%.2f%%)",
        len(result), lookback_days,
        result[0]["security_id"] if result else "none",
        (result[0]["score"] * 100) if result else 0,
    )
    return result


# ── Live screener (intraday, Dhan API) ────────────────────────────────────────

async def get_top_live(
    client,
    n: int = _DEFAULT_N,
    segment: str = "NSE_EQ",
    rank_by: str = "change_pct",  # "change_pct" | "volume"
) -> list[dict[str, Any]]:
    """
    Fetch today's OHLC for all NSE_EQ instruments in the bars universe
    (up to 1000 at a time via Dhan batch quote) and rank by today's move %.

    Requires an open DhanClient session — call during market hours.
    """
    from db import get_session

    # Get universe: all NSE equity IDs that have recent bar data
    with get_session() as session:
        rows = session.execute(text("""
            SELECT DISTINCT b.security_id
            FROM bars b
            JOIN instruments i ON i.security_id = b.security_id
            WHERE b.timeframe = '1m'
              AND i.exchange_segment = :seg
              AND i.instrument_type  = 'EQUITY'
              AND b.time >= NOW() - INTERVAL '7 days'
            LIMIT :lim
        """), {"seg": segment, "lim": _UNIVERSE_LIMIT}).fetchall()
    universe = [r[0] for r in rows]

    if not universe:
        logger.warning("Live screener: no instruments in universe — falling back to historical")
        return get_top_volatile(n=n, segment=segment)

    # Dhan get_ohlc accepts {"NSE_EQ": [id1, id2, ...]} with int IDs
    try:
        payload = {segment: [int(sid) for sid in universe]}
        data = await client.get_ohlc(payload)
    except Exception as exc:
        logger.warning("Live screener: Dhan OHLC call failed (%s) — using historical", exc)
        return get_top_volatile(n=n, segment=segment)

    # Parse response and rank
    instruments = data.get("data", {})
    scored = []
    for sid_str, info in instruments.items():
        try:
            prev_close = float(info.get("previousClose") or info.get("close") or 0)
            ltp        = float(info.get("lastPrice") or info.get("ltp") or 0)
            volume     = int(info.get("volume") or 0)
            if prev_close <= 0 or ltp <= 0:
                continue
            change_pct = abs((ltp - prev_close) / prev_close)
            scored.append({
                "security_id": sid_str,
                "score":       round(change_pct, 6),
                "ltp":         ltp,
                "volume":      volume,
                "reason":      f"Δ{change_pct*100:+.2f}%  vol={volume:,}",
            })
        except (TypeError, ValueError):
            continue

    key = "score" if rank_by == "change_pct" else "volume"
    scored.sort(key=lambda x: x.get(key, 0), reverse=True)
    result = scored[:n]

    logger.info(
        "Screener [live]: top %d by %s from %d instruments — "
        "highest: %s (%s)",
        len(result), rank_by, len(universe),
        result[0]["security_id"] if result else "none",
        result[0]["reason"] if result else "",
    )
    return result


# ── Unified entry point ────────────────────────────────────────────────────────

async def screen(
    n: int = _DEFAULT_N,
    segment: str = "NSE_EQ",
    live: bool = False,
    client=None,
) -> list[dict[str, Any]]:
    """
    Main screener entry point.
    live=True requires a DhanClient — falls back to historical if unavailable.
    """
    if live and client is not None:
        return await get_top_live(client, n=n, segment=segment)
    return get_top_volatile(n=n, segment=segment)
