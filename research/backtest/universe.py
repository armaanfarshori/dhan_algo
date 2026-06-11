"""
Point-in-time universe — the screener as it would have run on a given day.

The live platform picks its watchlist with core/nse_screener.get_top_volatile,
which ranks by ATR% over the trailing window. Reusing today's ranking for a
historical backtest is lookahead (you'd be selecting on volatility the
strategy hadn't seen yet). This module reruns the same ranking with a hard
`time < as_of` cutoff, so each backtest day trades the names the screener
would actually have picked that morning.

Survivorship: the query draws from the bars table, which keeps delisted and
suspended securities — names that later died stay in the historical universe.
"""
import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text

logger = logging.getLogger("dhan.backtest.universe")

_SQL = text("""
    WITH daily_agg AS (
        SELECT
            b.security_id,
            b.time::date                                      AS dt,
            MAX(b.high)                                       AS day_high,
            MIN(b.low)                                        AS day_low,
            (ARRAY_AGG(b.close ORDER BY b.time DESC))[1]      AS last_close,
            SUM(b.volume)                                     AS day_volume
        FROM bars b
        JOIN instruments i ON i.security_id = b.security_id
        WHERE b.timeframe = '1m'
          AND b.time >= :window_start
          AND b.time <  :as_of               -- POINT-IN-TIME: nothing from the future
          AND i.exchange_segment = :seg
          AND i.instrument_type  = 'EQUITY'
        GROUP BY b.security_id, b.time::date
    ),
    ranked AS (
        SELECT security_id,
               COUNT(*)                                          AS trading_days,
               AVG((day_high - day_low) / NULLIF(last_close, 0)) AS atr_pct,
               AVG(day_volume)                                   AS avg_volume
        FROM daily_agg
        GROUP BY security_id
        HAVING COUNT(*) >= :min_days
    )
    SELECT security_id, atr_pct, avg_volume
    FROM ranked
    WHERE avg_volume >= :min_vol
    ORDER BY atr_pct DESC
    LIMIT :n
""")


def point_in_time_universe(
    as_of: date,
    n: int = 5,
    lookback_days: int = 30,
    segment: str = "NSE_EQ",
    min_avg_volume: int = 10_000,
    statement_timeout_ms: int = 60_000,
) -> list[dict[str, Any]]:
    """Top-n by trailing ATR% using ONLY bars strictly before as_of."""
    from db import get_session
    window_start = as_of - timedelta(days=lookback_days * 2)   # calendar → trading days

    with get_session() as s:
        s.execute(text(f"SET LOCAL statement_timeout = '{statement_timeout_ms}'"))
        rows = s.execute(_SQL, {
            "window_start": window_start, "as_of": as_of, "seg": segment,
            "min_days": max(5, lookback_days // 3), "min_vol": min_avg_volume, "n": n,
        }).fetchall()

    return [{"security_id": r[0], "atr_pct": float(r[1] or 0),
             "avg_volume": int(r[2] or 0)} for r in rows]
