"""FEAT-01: RateLimiter.usage() + DhanClient.rate_limit_usage() reporting.

The windowed limiter (TEST-02) now also keeps a cheap per-category daily call
counter for the dashboard spend panel. These tests pin its reporting shape.
"""
import asyncio

from core.client import RateLimiter, DhanClient


def test_usage_counts_calls_today():
    rl = RateLimiter("data")            # per_sec=5 → 3 calls never throttle

    async def go():
        for _ in range(3):
            await rl.acquire()

    asyncio.run(go())
    u = rl.usage()
    assert u["calls_today"] == 3
    assert u["per_sec"] == 5
    assert u["per_day"] == 100_000
    assert u["per_sec_used"] >= 0


def test_usage_counts_uncapped_category():
    rl = RateLimiter("non_trading")     # per_day=None, per_sec=20 → 2 calls fine

    async def go():
        await rl.acquire()
        await rl.acquire()

    asyncio.run(go())
    u = rl.usage()
    assert u["calls_today"] == 2        # counted even though there is no daily cap
    assert u["per_day"] is None


def test_client_rate_limit_usage_shape():
    c = DhanClient(client_id="cid", access_token="tok")
    usage = c.rate_limit_usage()
    assert set(usage) == {"orders", "data", "quote", "non_trading"}
    for cat, u in usage.items():
        assert "calls_today" in u
        assert "per_day" in u
        assert "per_sec" in u
