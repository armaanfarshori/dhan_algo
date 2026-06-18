"""
BatchedOhlc — coalescing TTL-cached REST OHLC fallback.

Core contract: N concurrent stale lookups must collapse into ONE batched
get_ohlc call (the 429-burst fix), and cache hits inside the TTL must not call
the client at all.
"""
import asyncio

from core.batched_ohlc import BatchedOhlc


class FakeClient:
    """Records every get_ohlc call's instrument map."""

    def __init__(self, ltp: float = 100.0):
        self._ltp = ltp
        self.calls: list = []

    async def get_ohlc(self, instruments: dict) -> dict:
        self.calls.append(instruments)
        # Let any concurrent coroutine reach the lock before we return, so the
        # coalescing path is genuinely exercised.
        await asyncio.sleep(0)
        seg = next(iter(instruments))
        ids = instruments[seg]
        return {
            "data": {
                seg: {
                    str(i): {"last_price": self._ltp + i,
                             "ohlc": {"high": self._ltp + i + 1,
                                      "low": self._ltp + i - 1}}
                    for i in ids
                }
            }
        }


def test_concurrent_stale_lookups_coalesce_into_one_call():
    """N simultaneous misses for different sids → exactly ONE get_ohlc call."""
    client = FakeClient()
    sids = ["1", "2", "3", "4", "5"]
    b = BatchedOhlc(client, "NSE_EQ", lambda: sids)

    async def _run():
        return await asyncio.gather(*[b.get(s) for s in sids])

    results = asyncio.run(_run())

    assert len(client.calls) == 1, (
        f"5 concurrent misses must coalesce into 1 batched call, got {len(client.calls)}")
    # Every requested sid resolved from that single batch.
    assert all(r is not None for r in results)
    assert results[0]["last_price"] == 101.0   # ltp(100) + sid(1)
    # The single call asked for every subscribed sid.
    assert sorted(client.calls[0]["NSE_EQ"]) == [1, 2, 3, 4, 5]


def test_cache_hit_within_ttl_skips_client():
    """A second lookup inside the TTL window must not call the client again."""
    client = FakeClient()
    b = BatchedOhlc(client, "NSE_EQ", lambda: ["1", "2"], ttl_s=100.0)

    async def _run():
        first = await b.get("1")
        second = await b.get("1")
        third = await b.get("2")   # cached from the first batch
        return first, second, third

    first, second, third = asyncio.run(_run())

    assert len(client.calls) == 1   # only the first miss fetched
    assert first == second
    assert third is not None        # sid 2 was in the same batch


def test_fetch_failure_returns_none():
    """A client error must fail open (return None), never raise."""

    class BoomClient:
        async def get_ohlc(self, instruments):
            raise RuntimeError("429 too many requests")

    b = BatchedOhlc(BoomClient(), "NSE_EQ", lambda: ["1"])
    assert asyncio.run(b.get("1")) is None


def test_requested_sid_included_even_if_provider_lags():
    """If the provider hasn't listed the sid yet, it is still fetched."""
    client = FakeClient()
    b = BatchedOhlc(client, "NSE_EQ", lambda: [])   # provider returns nothing
    result = asyncio.run(b.get("7"))
    assert result is not None
    assert client.calls[0]["NSE_EQ"] == [7]
