"""Token expiry parsing — IST/UTC handling that the whole refresh cycle hangs on."""
from datetime import datetime, timezone

from core.token_manager import _parse_expiry


def test_parses_utc_z_suffix():
    dt = _parse_expiry("2026-06-12T10:00:00Z")
    assert dt == datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc)


def test_parses_naive_as_ist():
    dt = _parse_expiry("2026-06-12T15:30:00")
    # 15:30 IST == 10:00 UTC
    assert dt == datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc)


def test_empty_and_garbage_return_none():
    assert _parse_expiry("") is None
    assert _parse_expiry("not-a-date") is None


# ── rotation hang guard (2026-08-15) ─────────────────────────────────────────
# The SDK login/renew calls carry no network timeout; a wedged request used to
# hang _generate() forever and with it the run() refresh loop (observed live:
# 50+ min stranded on an expired token, silently). These tests pin the
# wait_for guard on both paths.
import asyncio
import sys
import time
import types

import pytest

import core.token_manager as tm


class _HangingLogin:
    def __init__(self, client_id):
        pass

    def generate_token(self, pin, totp):
        time.sleep(3)           # simulates a wedged HTTPS request

    def renew_token(self, old):
        time.sleep(3)


def _install_fake_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "dhanhq",
                        types.SimpleNamespace(DhanLogin=_HangingLogin))
    monkeypatch.setattr(tm, "GENERATE_TIMEOUT_S", 0.2)


def _mgr(monkeypatch):
    monkeypatch.setattr(tm.MasterTokenManager, "__init__",
                        lambda self: None)
    m = tm.MasterTokenManager()
    m.client_id, m.pin, m.totp_secret = "c", "0000", "JBSWY3DPEHPK3PXP"
    m._token, m._expiry, m._callbacks = None, None, []
    m._refresh_lock = None
    return m


def test_generate_times_out_instead_of_hanging(monkeypatch):
    _install_fake_sdk(monkeypatch)
    m = _mgr(monkeypatch)

    # Elapsed is measured INSIDE the loop: asyncio.run() additionally joins the
    # abandoned executor thread at shutdown (the stub's full sleep), which is
    # unavoidable and not what the guard controls.
    async def go():
        t0 = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await m._generate()
        return time.monotonic() - t0

    assert asyncio.run(go()) < 2   # guard (~0.2 s), not the stub's 3 s


def test_renew_timeout_degrades_to_none(monkeypatch):
    _install_fake_sdk(monkeypatch)
    m = _mgr(monkeypatch)
    m._token = "old-token"

    # _renew swallows the timeout and returns None → caller falls back to
    # _generate(), exactly the pre-existing DH-905 fallback contract.
    async def go():
        t0 = time.monotonic()
        out = await m._renew()
        return out, time.monotonic() - t0

    out, elapsed = asyncio.run(go())
    assert out is None
    assert elapsed < 2
