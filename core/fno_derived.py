"""F&O Phase-0 derived metrics — realized vol + implied move.

Two derived quantities feed the volatility-regime gate (ml/fno_vol_gate.py) and
the backtest:

  • realized_vol_20d — trailing 20-day close-to-close annualised volatility of
        NIFTY index futures. Written back into ``futures_bars.realized_vol_20d``.
  • implied_move     — the option market's expected move to expiry, derived from
        the ATM straddle IV: ``spot * straddle_iv * sqrt(dte / 365)``. Written
        back into ``option_atm_iv.implied_move``.

The pure functions (``realized_vol_series``, ``implied_move``) are deterministic
and DB-free so they unit-test without a database. The ``compute_*`` wrappers read
from / write to TimescaleDB.

Conventions:
  • Returns are close-to-close log returns ``ln(C_t / C_{t-1})``.
  • Annualisation uses 252 trading days for realized vol; the implied move uses
    365 calendar days (dte is calendar days to expiry), matching how option IV
    is quoted. Both are annualised *fractions* (straddle_iv is stored as a
    fraction by core/fno_backfill).
  • Sample standard deviation (ddof=1) over the window — the standard realized-
    vol estimator. A window needs ``window + 1`` closes to produce its first
    value; earlier bars get ``None``.
"""
from __future__ import annotations

import logging
import math
from typing import Optional, Sequence

import pandas as pd

logger = logging.getLogger("dhan.fno_derived")

TRADING_DAYS = 252
CALENDAR_DAYS = 365


# ── pure metrics ─────────────────────────────────────────────────────────────────
def daily_log_returns(closes: Sequence[float]) -> list[float]:
    """Close-to-close log returns. Length ``len(closes) - 1`` (empty if < 2)."""
    out: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev is None or cur is None or prev <= 0 or cur <= 0:
            out.append(float("nan"))
        else:
            out.append(math.log(cur / prev))
    return out


def realized_vol_series(
    closes: Sequence[float],
    window: int = 20,
    trading_days: int = TRADING_DAYS,
    ddof: int = 1,
) -> list[Optional[float]]:
    """Trailing rolling annualised realized vol, aligned to ``closes``.

    ``result[i]`` is the annualised stdev of the ``window`` log returns ending at
    close ``i`` (i.e. returns ``r[i-window+1 .. i]``), or ``None`` until a *full*
    window of finite returns exists. ``len(result) == len(closes)``.

    Implemented on ``pandas.rolling(...).std(ddof=1)`` rather than a hand-rolled
    loop. Crucially ``min_periods=window`` means any NaN return inside a window
    (e.g. a data gap / non-positive close) yields ``None`` for that bar rather
    than silently computing vol over a shrunken sample.
    """
    if window < 2:
        raise ValueError(f"window must be >= 2, got {window}")
    n = len(closes)
    result: list[Optional[float]] = [None] * n
    if n < window + 1:
        return result
    rets = daily_log_returns(closes)  # index j corresponds to close j+1; NaN for bad bars
    # rolling std at position k covers rets[k-window+1 .. k]; close index i uses
    # rets[i-window .. i-1] == the window ending at k = i-1.
    roll = pd.Series(rets, dtype=float).rolling(window, min_periods=window).std(ddof=ddof)
    ann = math.sqrt(trading_days)
    for i in range(window, n):
        v = roll.iloc[i - 1]
        result[i] = None if pd.isna(v) else float(v) * ann
    return result


def implied_move(
    spot: Optional[float],
    straddle_iv: Optional[float],
    dte: Optional[int],
    days_in_year: int = CALENDAR_DAYS,
) -> Optional[float]:
    """Expected move to expiry in price points: ``spot * iv * sqrt(dte/365)``.

    ``straddle_iv`` is an annualised fraction. dte=0 → 0 move. None inputs → None.
    """
    if spot is None or straddle_iv is None or dte is None:
        return None
    if spot <= 0 or straddle_iv <= 0 or dte < 0:
        return None
    return spot * straddle_iv * math.sqrt(dte / days_in_year)


def implied_move_pct(
    straddle_iv: Optional[float],
    dte: Optional[int],
    days_in_year: int = CALENDAR_DAYS,
) -> Optional[float]:
    """Expected move as a fraction of spot: ``iv * sqrt(dte/365)``."""
    if straddle_iv is None or dte is None or straddle_iv <= 0 or dte < 0:
        return None
    return straddle_iv * math.sqrt(dte / days_in_year)


# ── DB wrappers (read futures_bars / option_atm_iv → update derived columns) ──────
def _bulk_update(sql: str, rows: list[tuple]) -> int:
    """Run one ``UPDATE ... FROM (VALUES %s)`` via psycopg2 execute_values — a
    single round-trip instead of N (mirrors backfill.py's upsert pattern)."""
    if not rows:
        return 0
    from psycopg2.extras import execute_values

    from db import get_session

    with get_session() as session:
        cur = session.connection().connection.cursor()
        execute_values(cur, sql, rows, page_size=1000)
        cur.close()
    return len(rows)


def compute_realized_vol(symbol: str, timeframe: str = "1d", window: int = 20) -> int:
    """Recompute realized_vol_20d for one symbol/timeframe and write it back to
    futures_bars in a single bulk UPDATE. Returns the number of rows updated.

    NOTE: assumes ``symbol`` is ONE continuous close series — see the warning on
    backfill_futures_bars about per-contract collisions corrupting this metric.
    """
    from sqlalchemy import text

    from db import get_session

    with get_session() as session:
        rows = session.execute(
            text(
                "SELECT time, close FROM futures_bars "
                "WHERE symbol = :s AND timeframe = :tf ORDER BY time"
            ),
            {"s": symbol, "tf": timeframe},
        ).all()
    if not rows:
        return 0
    times = [r[0] for r in rows]
    closes = [float(r[1]) for r in rows]
    vols = realized_vol_series(closes, window=window)
    payload = [(symbol, timeframe, t, v) for t, v in zip(times, vols) if v is not None]
    n = _bulk_update(
        "UPDATE futures_bars AS f SET realized_vol_20d = d.v "
        "FROM (VALUES %s) AS d(s, tf, t, v) "
        "WHERE f.symbol = d.s AND f.timeframe = d.tf AND f.time = d.t",
        payload,
    )
    logger.info("realized_vol_20d: updated %d rows for %s/%s", n, symbol, timeframe)
    return n


def compute_implied_move(symbol: str) -> int:
    """Recompute implied_move for one symbol and write it back to option_atm_iv
    in a single bulk UPDATE. Returns the number of rows updated."""
    from sqlalchemy import text

    from db import get_session

    with get_session() as session:
        rows = session.execute(
            text(
                "SELECT time, expiry_date, spot_ref, straddle_iv, dte "
                "FROM option_atm_iv WHERE symbol = :s"
            ),
            {"s": symbol},
        ).all()
    payload = []
    for t, expiry, spot, iv, dte in rows:
        im = implied_move(
            None if spot is None else float(spot),
            None if iv is None else float(iv),
            None if dte is None else int(dte),
        )
        if im is not None:
            payload.append((symbol, expiry, t, im))
    n = _bulk_update(
        "UPDATE option_atm_iv AS o SET implied_move = d.im "
        "FROM (VALUES %s) AS d(s, e, t, im) "
        "WHERE o.symbol = d.s AND o.expiry_date = d.e AND o.time = d.t",
        payload,
    )
    logger.info("implied_move: updated %d rows for %s", n, symbol)
    return n
