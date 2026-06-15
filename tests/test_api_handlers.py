"""QA findings — dashboard API safety surface (apps/api.py).

Covered:
  - Mode change is read-only (POST → 409): trading mode cannot be flipped over
    HTTP without auth (regression — a deliberate safety property).
  - Kill switch writes the flag file the trader's risk loop polls (regression).
  - M6 (Medium): postback only acks; it neither persists nor reconciles the
    fill and has no signature verification.

Handlers are aiohttp coroutines; we drive them with a minimal fake request to
avoid standing up a full app + event loop fixture.
"""
import asyncio

import pytest

import apps.api as api


class _FakeRequest:
    def __init__(self, method="GET", json_body=None):
        self.method = method
        self._json = json_body or {}
    async def json(self):
        return self._json


def test_mode_post_is_read_only():
    """A POST to change trading mode must be refused (409) — going live is an
    env edit + restart, never one HTTP call."""
    resp = asyncio.run(api.trading_mode_handler(_FakeRequest(method="POST")))
    assert resp.status == 409


def test_killswitch_writes_flag_file(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "RUN_DIR", tmp_path)
    monkeypatch.setattr(api, "KILLSWITCH_FILE", tmp_path / "killswitch")
    resp = asyncio.run(api.killswitch_handler(_FakeRequest(method="POST")))
    assert resp.status == 200
    assert (tmp_path / "killswitch").exists()


def test_postback_acks(monkeypatch):
    """Characterization of M6: the postback endpoint returns an ack and does not
    raise — but note it performs NO fill persistence or reconciliation."""
    payload = {"orderId": "X1", "orderStatus": "TRADED", "tradingSymbol": "AERO"}
    resp = asyncio.run(api.postback_handler(_FakeRequest(method="POST", json_body=payload)))
    assert resp.status == 200


@pytest.mark.xfail(strict=True,
                   reason="M6: postback is a no-op logger — it should reconcile the "
                          "broker fill (and verify the source) rather than discard it")
def test_postback_reconciles_fill(monkeypatch):
    recorded = []
    # A reconciling implementation would route the fill somewhere observable.
    monkeypatch.setattr(api, "_reconcile_postback", lambda p: recorded.append(p), raising=False)
    payload = {"orderId": "X1", "orderStatus": "TRADED"}
    asyncio.run(api.postback_handler(_FakeRequest(method="POST", json_body=payload)))
    assert recorded, "postback should hand the fill to reconciliation"
