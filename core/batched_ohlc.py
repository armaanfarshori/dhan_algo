"""
BatchedOhlc — a shared, coalescing, TTL-cached REST OHLC fallback.

Problem it solves
------------------
When the WebSocket feed goes quiet, every StrategyRunner whose cached tick is
older than _FEED_FRESH_S independently calls ``client.get_ohlc({seg: [its_sid]})``.
With N watched names that is N near-simultaneous single-instrument REST calls,
which trips Dhan's marketfeed/ohlc rate limit (HTTP 429).

This class collapses those N concurrent stale lookups into ONE batched
``get_ohlc({seg: [all subscribed sids]})`` call:

  * a small per-sid TTL cache (value, monotonic_ts) — a hit inside the TTL
    window returns immediately with no network call;
  * an asyncio.Lock + in-flight guard so simultaneous misses trigger only ONE
    batched fetch per TTL window. Callers re-check the cache after acquiring the
    lock, so the second-through-Nth callers read the just-filled cache instead
    of firing their own fetch.

Dhan's marketfeed/ohlc accepts up to 1000 instruments per call, and get_ohlc
takes ``{segment: [ids]}`` as a LIST — so one call covers the whole watchlist.

Fail-open: any error returns None for the requested sid (the runner then keeps
its existing per-call error handling — 0.0/None/None — unchanged).
"""
import asyncio
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger("dhan.batched_ohlc")

_DEFAULT_TTL_S = 5.0


class BatchedOhlc:
    """Coalescing, TTL-cached batched OHLC fetcher shared across all runners.

    Construct ONCE (in trader.py) with the client + segment + a callable that
    returns the list of currently-subscribed sids (e.g. feed.all_subscribed_sids).
    """

    def __init__(self, client, segment: str, sids_provider: Callable[[], list],
                 ttl_s: float = _DEFAULT_TTL_S):
        self._client = client
        self._segment = segment
        self._sids_provider = sids_provider
        self._ttl = ttl_s
        # sid -> (value_dict, monotonic_ts)
        self._cache: dict[str, tuple[dict, float]] = {}
        # Created lazily on first use so the object can be constructed outside a
        # running loop (loop-agnostic; avoids the py3.9 "no current event loop"
        # error at __init__). Safe under asyncio: the check+assign has no await
        # between, so concurrent first-callers can't make two locks.
        self._lock: Optional[asyncio.Lock] = None

    def _fresh(self, sid: str) -> Optional[dict]:
        """Return the cached value for sid if it is within the TTL, else None."""
        hit = self._cache.get(str(sid))
        if hit is None:
            return None
        value, ts = hit
        if time.monotonic() - ts <= self._ttl:
            return value
        return None

    async def get(self, sid: str) -> Optional[dict]:
        """Return {last_price, ohlc} for ``sid`` (or None on failure).

        On a cache miss this fetches a SINGLE batched get_ohlc for every
        currently-subscribed sid and caches them all, so concurrent misses for
        different sids share one network round-trip.
        """
        sid = str(sid)
        cached = self._fresh(sid)
        if cached is not None:
            return cached

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            # Re-check under the lock — a concurrent caller may have just filled
            # the cache while we waited, so we do not fire a duplicate fetch.
            cached = self._fresh(sid)
            if cached is not None:
                return cached

            try:
                sids = self._sids_provider() or []
                ids = [int(s) for s in sids if str(s).isdigit()]
                # Always include the requested sid even if the provider lagged.
                if sid.isdigit() and int(sid) not in ids:
                    ids.append(int(sid))
                if not ids:
                    return None
                data = await self._client.get_ohlc({self._segment: ids})
            except Exception as exc:
                logger.warning("BatchedOhlc: batched fetch failed (%s)", exc)
                return None

            seg = data.get("data", {}).get(self._segment, {}) or {}
            now = time.monotonic()
            for key, tick in seg.items():
                ltp = float((tick or {}).get("last_price") or 0)
                ohlc = (tick or {}).get("ohlc", {}) or {}
                self._cache[str(key)] = (
                    {"last_price": ltp, "ohlc": ohlc},
                    now,
                )
            hit = self._cache.get(sid)
            return hit[0] if hit is not None else None
