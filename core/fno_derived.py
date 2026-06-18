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


def _std(xs: Sequence[float], ddof: int = 1) -> Optional[float]:
    """Sample standard deviation; None if too few finite points."""
    finite = [x for x in xs if not math.isnan(x)]
    n = len(finite)
    if n - ddof <= 0:
        return None
    mean = sum(finite) / n
    var = sum((x - mean) ** 2 for x in finite) / (n - ddof)
    return math.sqrt(var)


def realized_vol_series(
    closes: Sequence[float],
    window: int = 20,
    trading_days: int = TRADING_DAYS,
    ddof: int = 1,
) -> list[Optional[float]]:
    """Trailing rolling annualised realized vol, aligned to ``closes``.

    ``result[i]`` is the annualised stdev of the ``window`` log returns ending at
    close ``i`` (i.e. returns ``r[i-window+1 .. i]``), or ``None`` until enough
    history exists. ``len(result) == len(closes)``.
    """
    n = len(closes)
    result: list[Optional[float]] = [None] * n
    if n < window + 1 or window < 2:
        return result
    rets = daily_log_returns(closes)  # index j corresponds to close j+1
    ann = math.sqrt(trading_days)
    # close index i (i >= window) uses returns rets[i-window .. i-1] (window of them)
    for i in range(window, n):
        sd = _std(rets[i - window:i], ddof=ddof)
        result[i] = None if sd is None else sd * ann
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
def compute_realized_vol(symbol: str, timeframe: str = "1d", window: int = 20) -> int:
    """Recompute realized_vol_20d for one symbol/timeframe and write it back to
    futures_bars. Returns the number of rows updated (non-None vol values)."""
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
        updated = 0
        for t, v in zip(times, vols):
            if v is None:
                continue
            session.execute(
                text(
                    "UPDATE futures_bars SET realized_vol_20d = :v "
                    "WHERE symbol = :s AND timeframe = :tf AND time = :t"
                ),
                {"v": v, "s": symbol, "tf": timeframe, "t": t},
            )
            updated += 1
    logger.info("realized_vol_20d: updated %d rows for %s/%s", updated, symbol, timeframe)
    return updated


def compute_implied_move(symbol: str) -> int:
    """Recompute implied_move for one symbol and write it back to option_atm_iv.
    Returns the number of rows updated."""
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
        updated = 0
        for t, expiry, spot, iv, dte in rows:
            im = implied_move(
                None if spot is None else float(spot),
                None if iv is None else float(iv),
                None if dte is None else int(dte),
            )
            if im is None:
                continue
            session.execute(
                text(
                    "UPDATE option_atm_iv SET implied_move = :im "
                    "WHERE symbol = :s AND expiry_date = :e AND time = :t"
                ),
                {"im": im, "s": symbol, "e": expiry, "t": t},
            )
            updated += 1
    logger.info("implied_move: updated %d rows for %s", updated, symbol)
    return updated
