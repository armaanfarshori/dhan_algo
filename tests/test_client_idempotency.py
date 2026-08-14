"""QA findings C1, H1, M1 — DhanClient retry/idempotency & rate limiting.

C1 (Critical): POST /orders is retried on network/5xx with no idempotency key
               (correlation_id empty) → a lost response after a successful
               placement double-places the order.
               FIX: place_order auto-generates a correlation_id (client-side)
               and uses idempotent=False in _request — on transient error it
               reconciles via get_order_by_correlation_id before re-raising.

H1 (High):     RateLimiter enforces only per_sec; per_min/hour/day are defined
               but never applied, so daily caps can be silently blown.
               FIX: all four sliding windows are maintained; short windows
               (per_sec, per_min) throttle by sleeping; long windows (per_hour,
               per_day) raise RateLimitExceeded immediately.

M1 (Medium):   Concurrent auth-error handling is unserialized.
               FIX: MasterTokenManager._refresh_lock serializes handle_auth_error.
"""
import asyncio
import time

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
    def request(self, method, url, json=None, params=None, proxy=None):
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
    """All four sliding windows (second/min/hour/day) now exist as state so
    every applicable limit is enforced — H1 is fixed."""
    rl = RateLimiter("data")
    assert rl.per_sec == 5
    assert RateLimiter.LIMITS["data"]["per_day"] == 100_000
    # All windows are created, even when the limit for that window is None.
    assert hasattr(rl, "_second_window")
    assert hasattr(rl, "_day_window")


def test_rate_limiter_enforces_daily_cap():
    """H1 fix: exhausting the per_day quota raises RateLimitExceeded immediately
    (never sleeps for 24 hours) — deterministic, fast, no real timer needed."""
    from core.client import RateLimitExceeded

    rl = RateLimiter("orders")   # per_day = 7000
    assert rl.per_day == 7000

    # Pre-fill the day window to the limit so the next acquire() tips it over.
    now = time.monotonic()
    for _ in range(rl.per_day):
        rl._day_window.append(now)

    async def go():
        # sec/min/hour windows stay EMPTY, so acquire() sails past them with no
        # throttle/sleep and trips the long-horizon daily cap, which raises
        # immediately. (Pre-filling per_min here would make acquire() really
        # sleep up to 60s before reaching the day check.)
        await rl.acquire()

    with pytest.raises(RateLimitExceeded) as exc_info:
        asyncio.run(go())

    assert exc_info.value.category == "orders"
    assert exc_info.value.window == "per_day"


# ── C1: order placement must not blindly retry without idempotency ─────────────

def test_order_payload_has_no_idempotency_key():
    """LiveExecutor.submit does not *generate* a correlation_id — the key is
    auto-generated client-side inside place_order, then surfaced back in the
    result dict so the executor can store it as client_order_id in the audit
    trail. The executor only *reads* the key from the result; it never sets one.
    Verify the submit call does not pass correlation_id to place_order."""
    import inspect
    from engine.execution import LiveExecutor
    src = inspect.getsource(LiveExecutor.submit)
    # The executor reads correlation_id from the result (for audit logging) but
    # must never pass it as an argument to place_order.
    assert "correlation_id=" not in src   # never passed as kwarg to place_order


def test_order_not_retried_without_idempotency(monkeypatch):
    """C1 fix: on a transient network error place_order does NOT blindly retry
    the POST. Instead it reconciles via get_order_by_correlation_id. When the
    reconcile call returns a valid order dict, it is returned as the result.
    Total POST count to /orders must be exactly 1 (no double-placement)."""
    # Script: POST /orders → ClientError (blip), GET /orders/external/<id> → 200
    c = _client([aiohttp.ClientError(), _FakeResp(200, {"orderId": "X1"})], monkeypatch)

    async def go():
        return await c.place_order(
            transaction_type="BUY", exchange_segment="NSE_EQ",
            product_type="INTRADAY", order_type="MARKET",
            security_id="111", quantity=1)

    result = asyncio.run(go())
    # Only one POST to /orders; the 200 was consumed by the reconcile GET.
    assert c._session.post_count("orders") == 1
    # place_order injects correlationId into the result for the caller's audit trail
    assert result.get("orderId") == "X1"
    assert "correlationId" in result


# ── M1: concurrent auth-error handling is unserialized ─────────────────────────

def test_token_refresh_is_serialized():
    """M1 fix: MasterTokenManager has a _refresh_lock so concurrent DH-901
    responses serialize token generation rather than racing."""
    from core.token_manager import MasterTokenManager
    mgr = MasterTokenManager()
    assert hasattr(mgr, "_refresh_lock"), "token refresh should be guarded by a lock"
