"""
Point-in-time universe — the screener as it would have run on a given day.

The live platform picks its watchlist with core/nse_screener.get_top_volatile,
which ranks by ATR% over the trailing window. Reusing today's ranking for a
historical backtest is lookahead (you'd be selecting on volatility the
strategy hadn't seen yet). This module reruns the same ranking with a hard
`time < as_of` cutoff, so each backtest day trades the names the screener
would actually have picked that morning.

Survivorship: this runs against the cleaned replica (dhan_clean), whose universe
is drawn from the CURRENT scrip master — delisted/suspended names are absent, so
results are an optimistic upper bound (logged loudly by build_clean_db.py).
"""
import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text

logger = logging.getLogger("dhan.backtest.universe")

# Runs against dhan_clean, which holds ONLY 1-minute bars + the clean_universe
# table (no `instruments`, no 1d rollup). So we derive daily high/low/close/
# volume from the 1m bars on the fly (cheap now — dhan_clean is small and the
# backfill is no longer writing) and use clean_universe membership in place of
# the old instruments EQUITY filter (it is already the validated NSE_EQ liquid
# set). Point-in-time is preserved by the `time < :as_of` cutoff.
_SQL = text("""
    WITH daily AS (
        SELECT b.security_id,
               (b.time AT TIME ZONE 'Asia/Kolkata')::date AS d,
               MAX(b.high)            AS hi,
               MIN(b.low)             AS lo,
               last(b.close, b.time)  AS cl,
               SUM(b.volume)          AS vol
        FROM bars b
        WHERE b.timeframe = '1m'
          AND b.time >= :window_start
          AND b.time <  :as_of               -- POINT-IN-TIME: nothing from the future
        GROUP BY b.security_id, d
    )
    SELECT daily.security_id,
           AVG((daily.hi - daily.lo) / NULLIF(daily.cl, 0)) AS atr_pct,
           AVG(daily.vol)                                   AS avg_volume
    FROM daily
    JOIN clean_universe u ON u.security_id = daily.security_id
    GROUP BY daily.security_id
    HAVING COUNT(*) >= :min_days
       AND AVG(daily.vol) >= :min_vol
    ORDER BY atr_pct DESC, daily.security_id ASC   -- deterministic tiebreak at the LIMIT cut
    LIMIT :n
""")


def point_in_time_universe(
    as_of: date,
    n: int = 5,
    lookback_days: int = 30,
    segment: str = "NSE_EQ",   # implicit: clean_universe is the NSE_EQ liquid set
    min_avg_volume: int = 10_000,
    statement_timeout_ms: int = 60_000,
) -> list[dict[str, Any]]:
    """Top-n by trailing ATR% using ONLY bars strictly before as_of."""
    from db import get_session
    window_start = as_of - timedelta(days=lookback_days * 2)   # calendar → trading days

    with get_session() as s:
        # Cast to int before inlining: PostgreSQL rejects bind params in SET
        # statements, but coercing to int eliminates any injection surface.
        ms = int(statement_timeout_ms)
        s.execute(text(f"SET LOCAL statement_timeout = {ms}"))
        rows = s.execute(_SQL, {
            "window_start": window_start, "as_of": as_of,
            "min_days": max(5, lookback_days // 3), "min_vol": min_avg_volume, "n": n,
        }).fetchall()

    return [{"security_id": r[0], "atr_pct": float(r[1] or 0),
             "avg_volume": int(r[2] or 0)} for r in rows]
