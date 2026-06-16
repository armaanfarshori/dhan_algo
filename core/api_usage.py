"""
Cross-process API spend accounting.

Every process (trader / backfill / api) holds its own DhanClient with its own
in-memory RateLimiter counters.  ApiUsageFlusher reads those counters
periodically and writes only the DELTA since the last flush into the shared
api_usage table via an UPSERT.  The table accumulates the true total for the
trading day across all processes.

IST-midnight reset safety: if calls_today drops (counter reset at midnight),
we treat the new value as the full delta rather than subtracting (which would
yield a negative delta and potentially corrupt the row).

Restart safety: self._last starts empty on every boot.  The first flush after
a restart sends calls_today as-is (as if the entire count is a fresh delta).
This may slightly over-count if the process restarted mid-day, but because we
UPSERT with += the row is never decremented — the worst outcome is a slight
over-count, never a negative count.  For a monitoring table this is acceptable.
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger("dhan.api_usage")

IST = ZoneInfo("Asia/Kolkata")

_UPSERT = text("""
    INSERT INTO api_usage (ts_date, category, process, calls)
    VALUES (:d, :c, :p, :n)
    ON CONFLICT (ts_date, category, process) DO UPDATE SET
        calls      = api_usage.calls + EXCLUDED.calls,
        updated_at = CURRENT_TIMESTAMP
""")

# Ordered list of all 4 known categories (matches RateLimiter.LIMITS keys)
_ALL_CATEGORIES = ("orders", "data", "quote", "non_trading")


class ApiUsageFlusher:
    """Flush per-category API call deltas into the api_usage table.

    Construct with the name of the current process so rows are tagged:
        flusher = ApiUsageFlusher(process="trader")
        await flusher.flush(client)   # called periodically

    thread-safety: flush() is async and runs DB I/O in an executor; the
    _last dict is only accessed from the single async loop that calls flush(),
    so no locking is needed.
    """

    def __init__(self, process: str):
        self.process = process
        self._last: Dict[str, int] = {}   # last flushed calls_today per category

    async def flush(self, client) -> None:
        """Read current call counts, compute deltas, upsert positive deltas.

        Never raises — errors are logged and swallowed so a DB hiccup never
        disrupts the trading or backfill loop.
        """
        try:
            usage = client.rate_limit_usage()
            today = datetime.now(IST).date()
            rows_to_write = []

            for category, info in usage.items():
                calls_today = int(info.get("calls_today", 0))
                last = self._last.get(category, 0)

                if calls_today < last:
                    # IST-midnight reset: counter rolled over — treat the new
                    # value as the full delta for the new day.
                    delta = calls_today
                else:
                    delta = calls_today - last

                if delta <= 0:
                    continue

                rows_to_write.append((category, calls_today, delta))

            if not rows_to_write:
                return

            def _write():
                from db import get_session
                with get_session() as s:
                    for category, calls_today, delta in rows_to_write:
                        s.execute(_UPSERT, {
                            "d": today,
                            "c": category,
                            "p": self.process,
                            "n": delta,
                        })

            await asyncio.get_running_loop().run_in_executor(None, _write)

            # Update _last only after a successful write so a failed write
            # doesn't silently drop the delta.
            for category, calls_today, _delta in rows_to_write:
                self._last[category] = calls_today

        except Exception as exc:
            logger.warning("ApiUsageFlusher[%s].flush() failed: %s", self.process, exc)


def query_today_totals() -> dict:
    """Return today's API usage aggregated across all processes (sync).

    Callers in async contexts should wrap in run_in_executor:
        result = await loop.run_in_executor(None, query_today_totals)

    Returns:
        {
          "date": "YYYY-MM-DD",
          "categories": {
            "<cat>": {
              "total": <int>,
              "per_day": <int|None>,
              "by_process": {"trader": <int>, "backfill": <int>, "api": <int>}
            },
            ...
          }
        }
    All 4 categories are always present even if absent from the table (total=0).
    """
    from core.client import RateLimiter

    today = datetime.now(IST).date()

    def _read():
        from db import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT category, process, calls
                FROM api_usage
                WHERE ts_date = :d
            """), {"d": today}).fetchall()
        return rows

    rows = _read()

    # Build per-category totals and per-process breakdown
    result: Dict[str, dict] = {}
    for cat in _ALL_CATEGORIES:
        limits = RateLimiter.LIMITS.get(cat, {})
        result[cat] = {
            "total": 0,
            "per_day": limits.get("per_day"),   # None if uncapped
            "by_process": {},
        }

    for category, process, calls in rows:
        if category not in result:
            # Unknown category in DB (shouldn't happen, but be defensive)
            result[category] = {"total": 0, "per_day": None, "by_process": {}}
        result[category]["total"] += int(calls)
        result[category]["by_process"][process] = (
            result[category]["by_process"].get(process, 0) + int(calls)
        )

    return {"date": today.isoformat(), "categories": result}
