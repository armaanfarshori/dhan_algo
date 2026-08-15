"""Backfill token-rotation waiters — the shared-client concurrency contract.

Live incident 2026-08-15 (4-way --concurrency): the first waiter to see the
rotated dhan_token.json pushed it into the SHARED DhanClient and resumed;
every other waiter then compared file-token vs client-token, saw them equal,
and spun to max_wait believing no rotation had happened. The fix pins the
token that actually FAILED per waiter; these tests are that contract.
"""
import asyncio

import backfill as bf


class _FakeClient:
    def __init__(self, token: str):
        self.access_token = token

    def _on_token_refreshed(self, fresh: str):
        self.access_token = fresh


def test_sibling_waiter_accepts_already_loaded_rotation(monkeypatch):
    client = _FakeClient("OLD")
    monkeypatch.setattr("core.token_manager.read_current_token", lambda: "NEW")

    # Waiter 1 sees the rotation and updates the shared client.
    assert bf._reload_token(client) is True
    assert client.access_token == "NEW"
    # Waiter 2 (client already updated by the sibling): the file token equals
    # the client token AND read_current_token vouches it is unexpired → there
    # is nothing to wait for. This exact call returned False forever before
    # the fix.
    assert bf._reload_token(client) is True


def test_no_valid_token_on_disk_keeps_waiting(monkeypatch):
    client = _FakeClient("OLD")
    # read_current_token returns None for missing OR expired tokens — the one
    # situation where waiting is correct.
    monkeypatch.setattr("core.token_manager.read_current_token", lambda: None)
    assert bf._reload_token(client) is False


def test_concurrent_waiters_both_resolve(monkeypatch):
    """Both concurrent waiters on one shared client resolve after ONE rotation.

    Before the fix the second waiter — entering after the first had already
    refreshed the client — hung to max_wait.
    """
    client = _FakeClient("OLD")
    monkeypatch.setattr("core.token_manager.read_current_token", lambda: "NEW")

    async def go():
        return await asyncio.gather(
            bf._wait_for_fresh_token(client, poll_s=1, max_wait_s=2),
            bf._wait_for_fresh_token(client, poll_s=1, max_wait_s=2),
        )

    assert asyncio.run(go()) == [True, True]
