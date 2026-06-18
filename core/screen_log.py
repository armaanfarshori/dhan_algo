"""
Daily screened-stock audit log.
===============================
Persists, per trading day, the set of stocks the ATR% screener produced and
whether each one made the final tradeable watchlist (`selected`).  Written on
every watchlist refresh so the selection is auditable after the fact ("why was
security X in the universe on day D?").

This is a best-effort sidecar to trading: `record_daily_screen` NEVER raises.
A DB hiccup logs an exception and returns 0 — it must never break the
screener or the trading loop.

The write is idempotent: UNIQUE(trading_day, security_id) + ON CONFLICT DO
UPDATE means re-running a day (e.g. a mid-session restart that re-screens)
updates the existing rows rather than duplicating them.

Candidate dicts come from core.nse_screener.get_top_volatile — each has
`security_id`, `score` (ATR%), optionally `avg_volume`/`volume`, `avg_close`,
`reason`, `ticker`.  Fields that aren't present are stored NULL.
"""

import logging
from datetime import date, datetime
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger("dhan.screen_log")

IST = ZoneInfo("Asia/Kolkata")

_UPSERT = text("""
    INSERT INTO daily_screen
        (trading_day, security_id, ticker, rank, score, price, volume,
         selected, reason)
    VALUES
        (:trading_day, :security_id, :ticker, :rank, :score, :price, :volume,
         :selected, :reason)
    ON CONFLICT (trading_day, security_id) DO UPDATE SET
        ticker   = EXCLUDED.ticker,
        rank     = EXCLUDED.rank,
        score    = EXCLUDED.score,
        price    = EXCLUDED.price,
        volume   = EXCLUDED.volume,
        selected = EXCLUDED.selected,
        reason   = EXCLUDED.reason,
        created_at = now()
""")


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def record_daily_screen(
    candidates: Iterable[dict[str, Any]],
    selected_ids: Optional[Iterable[str]] = None,
    trading_day: Optional[date] = None,
) -> int:
    """Upsert one row per candidate into daily_screen for `trading_day`.

    Args:
        candidates: the screener's ranked candidate dicts (get_top_volatile
            output). Rank is taken from list order (1-based) unless a dict
            carries its own "rank".
        selected_ids: security_ids that made the final tradeable watchlist.
            If None, every candidate is marked selected=True (the screener
            output IS the selection). If given, selected = security_id in set.
        trading_day: the IST trading day to stamp (defaults to today IST).

    Returns:
        The number of rows written (0 on any failure — non-fatal).

    Never raises: any error is logged via logging.exception and swallowed so
    the screener/trading path is never disrupted by an audit-log write.
    """
    try:
        day = trading_day or datetime.now(IST).date()
        sel_set = set(selected_ids) if selected_ids is not None else None

        rows: list[dict[str, Any]] = []
        for i, c in enumerate(candidates):
            sid = c.get("security_id")
            if sid is None:
                continue
            sid = str(sid)
            # volume: prefer explicit "volume", fall back to screener's avg_volume
            volume = c.get("volume", c.get("avg_volume"))
            # price: prefer explicit "price", fall back to ltp / avg_close
            price = c.get("price", c.get("ltp", c.get("avg_close")))
            rows.append({
                "trading_day": day,
                "security_id": sid,
                "ticker":      c.get("ticker"),
                "rank":        _coerce_int(c.get("rank", i + 1)),
                "score":       _coerce_float(c.get("score")),
                "price":       _coerce_float(price),
                "volume":      _coerce_int(volume),
                "selected":    (sid in sel_set) if sel_set is not None else True,
                "reason":      c.get("reason"),
            })

        if not rows:
            return 0

        from db import get_session
        with get_session() as session:
            for r in rows:
                session.execute(_UPSERT, r)

        logger.info("daily_screen: recorded %d candidates for %s (%d selected)",
                    len(rows), day, sum(1 for r in rows if r["selected"]))
        return len(rows)

    except Exception:
        logger.exception("record_daily_screen failed (non-fatal) — "
                         "audit log skipped for this refresh")
        return 0


def get_screen_for_day(day: date) -> list[dict[str, Any]]:
    """Return all daily_screen rows for `day`, ranked (selected first, then rank).

    Synchronous — async callers should wrap in run_in_executor.
    """
    from db import get_engine
    with get_engine().connect() as conn:
        # Enrich the ticker from instruments at read time — daily_screen.ticker is
        # often null (the screener candidate dicts don't carry it), so without the
        # join the UI shows the numeric security_id instead of a symbol.
        rows = conn.execute(text("""
            SELECT ds.trading_day, ds.security_id,
                   COALESCE(NULLIF(ds.ticker, ''), NULLIF(i.ticker, ''), ds.security_id) AS ticker,
                   ds.rank, ds.score, ds.price, ds.volume, ds.selected, ds.reason, ds.created_at
            FROM daily_screen ds
            LEFT JOIN instruments i ON i.security_id = ds.security_id
            WHERE ds.trading_day = :d
            ORDER BY ds.selected DESC, ds.rank ASC NULLS LAST, ds.score DESC NULLS LAST
        """), {"d": day}).fetchall()
    return [{
        "trading_day": str(r[0]),
        "security_id": r[1],
        "ticker":      r[2] or r[1],
        "rank":        int(r[3]) if r[3] is not None else None,
        "score":       float(r[4]) if r[4] is not None else None,
        "price":       float(r[5]) if r[5] is not None else None,
        "volume":      int(r[6]) if r[6] is not None else None,
        "selected":    bool(r[7]),
        "reason":      r[8],
        "created_at":  str(r[9]),
    } for r in rows]


def list_screen_days(limit: int = 365) -> list[str]:
    """Return distinct trading_days present in daily_screen, newest first."""
    from db import get_engine
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT trading_day
            FROM daily_screen
            ORDER BY trading_day DESC
            LIMIT :lim
        """), {"lim": limit}).fetchall()
    return [str(r[0]) for r in rows]
