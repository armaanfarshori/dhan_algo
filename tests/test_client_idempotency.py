"""QA findings C1, H1, M1 — DhanClient retry/idempotency & rate limiting.

C1 (Critical): POST /orders is retried on network/5xx with no idempotency key
               (correlation_id empty) → a lost response after a successful
               placement double-places the order.
H1 (High):     RateLimiter enforces only per_sec; per_min/hour/day are defined
               but never applied, so daily caps can be silently blown.
M1 (Medium):   Concurrent auth-error handling is unserialized.

xfail(strict) marks the confirmed defects: the suite stays green today, and
each test flips to a hard failure the moment the bug is fixed (a prompt to
delete the xfail).
"""
import asyncio

import aiohttp
import pytest

from core.client import DhanClient, RateLimiter


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status, body):
        self.status, self._body = status, body
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self, content_type=None): return self._body
    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)


class _FakeSession:
    """Scripts a sequence of responses (or exceptions) and records every call."""
    def __init__(self, script):
        self._script = list(script)
        self.calls = []   # (method, endpoint_tail)
    def request(self, method, url, json=None, params=None):
        self.calls.append((method, url.rsplit("/", 1)[-1], json))
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
    def post_count(self, endpoint_tail):
        return sum(1 for m, e, _ in self.calls if m == "POST" and e == endpoint_tail)


def _client(script, monkeypatch):
    async def _no_sleep(*_a, **_k): return None
    monkeypatch.setattr("core.client.asyncio.sleep", _no_sleep)
    c = DhanClient(client_id="cid", access_token="tok", max_retries=3)
    c._session = _FakeSession(script)
    return c


# ── H1: rate limiter only enforces per-second ──────────────────────────────────

def test_rate_limiter_tracks_only_per_second():
    """Characterization: the windowed (min/hour/day) limits exist as config but
    the limiter keeps no window state for them — documents the H1 gap."""
    rl = RateLimiter("data")
    assert rl.per_sec == 5
    assert RateLimiter.LIMITS["data"]["per_day"] == 100_000
    # Only a 1-second window is maintained; nothing tracks the day.
    assert hasattr(rl, "_second_window")
    assert not hasattr(rl, "_day_window")


@pytest.mark.xfail(strict=True, reason="H1: per_min/hour/day limits defined but unenforced")
def test_rate_limiter_enforces_daily_cap():
    rl = RateLimiter("data")
    assert hasattr(rl, "_day_window"), "limiter should track the daily window"


# ── C1: order placement must not blindly retry without idempotency ─────────────

def test_order_payload_has_no_idempotency_key():
    """Characterization: LiveExecutor places orders with an empty correlationId,
    so a retried POST cannot be de-duplicated broker-side."""
    import inspect
    from engine.execution import LiveExecutor
    src = inspect.getsource(LiveExecutor.submit)
    assert "correlation_id" not in src   # not set → empty default in place_order


@pytest.mark.xfail(strict=True,
                   reason="C1: POST /orders is retried on transient failure with no "
                          "idempotency key → duplicate live order")
def test_order_not_retried_without_idempotency(monkeypatch):
    # First attempt: network blip (order may have reached the broker). Second: 200.
    c = _client([aiohttp.ClientError(), _FakeResp(200, {"orderId": "X1"})], monkeypatch)

    async def go():
        return await c.place_order(
            transaction_type="BUY", exchange_segment="NSE_EQ",
            product_type="INTRADAY", order_type="MARKET",
            security_id="111", quantity=1)

    asyncio.run(go())
    # Desired: at most ONE POST to /orders for one logical order. Today: 2.
    assert c._session.post_count("orders") == 1


# ── M1: concurrent auth-error handling is unserialized ─────────────────────────

@pytest.mark.xfail(strict=True,
                   reason="M1: handle_auth_error has no lock — concurrent DH-901s "
                          "race token generation and the .env rewrite")
def test_token_refresh_is_serialized():
    from core.token_manager import MasterTokenManager
    mgr = MasterTokenManager()
    assert hasattr(mgr, "_refresh_lock"), "token refresh should be guarded by a lock"
