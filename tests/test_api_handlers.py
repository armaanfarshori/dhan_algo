"""QA findings — dashboard API safety surface (apps/api.py).

Covered:
  - Mode change is read-only (POST → 409): trading mode cannot be flipped over
    HTTP without auth (regression — a deliberate safety property).
  - Kill switch writes the flag file the trader's risk loop polls (regression).
  - M6 (Medium): postback only acks; it neither persists nor reconciles the
    fill and has no signature verification.
  - SEC-04: shared-secret gate on POST /api/killswitch and
    POST /api/watchlist/refresh.

Handlers are aiohttp coroutines; we drive them with a minimal fake request to
avoid standing up a full app + event loop fixture.
"""
import asyncio
import types


import apps.api as api


class _FakeRequest:
    def __init__(self, method="GET", json_body=None, headers=None):
        self.method = method
        self._json = json_body or {}
        self.headers = headers or {}
    async def json(self):
        return self._json


def _make_cfg(**overrides):
    """Return a SimpleNamespace that looks like cfg, with any field overrideable."""
    import config as cfg_mod
    base = cfg_mod.Config()
    d = dict(base)
    d.update(overrides)
    return types.SimpleNamespace(**d)


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


def test_postback_reconciles_fill(monkeypatch):
    recorded = []
    # A reconciling implementation would route the fill somewhere observable.
    monkeypatch.setattr(api, "_reconcile_postback", lambda p: recorded.append(p), raising=False)
    payload = {"orderId": "X1", "orderStatus": "TRADED"}
    asyncio.run(api.postback_handler(_FakeRequest(method="POST", json_body=payload)))
    assert recorded, "postback should hand the fill to reconciliation"


# ── SEC-04: shared-secret gate ────────────────────────────────────────────────

def test_killswitch_token_set_correct_header_allowed(tmp_path, monkeypatch):
    """Token configured + correct X-Dashboard-Token → not 401 (kill-switch fires)."""
    monkeypatch.setattr(api, "RUN_DIR", tmp_path)
    monkeypatch.setattr(api, "KILLSWITCH_FILE", tmp_path / "killswitch")
    monkeypatch.setattr(api, "cfg", _make_cfg(dashboard_token="secret123"))
    monkeypatch.setattr(api, "_unprotected_warned", False)
    req = _FakeRequest(method="POST", headers={"X-Dashboard-Token": "secret123"})
    resp = asyncio.run(api.killswitch_handler(req))
    assert resp.status != 401


def test_killswitch_token_set_no_header_rejected(monkeypatch):
    """Token configured + no header → 401."""
    monkeypatch.setattr(api, "cfg", _make_cfg(dashboard_token="secret123"))
    monkeypatch.setattr(api, "_unprotected_warned", False)
    req = _FakeRequest(method="POST", headers={})
    resp = asyncio.run(api.killswitch_handler(req))
    assert resp.status == 401


def test_killswitch_token_set_wrong_header_rejected(monkeypatch):
    """Token configured + wrong value → 401."""
    monkeypatch.setattr(api, "cfg", _make_cfg(dashboard_token="secret123"))
    monkeypatch.setattr(api, "_unprotected_warned", False)
    req = _FakeRequest(method="POST", headers={"X-Dashboard-Token": "wrongtoken"})
    resp = asyncio.run(api.killswitch_handler(req))
    assert resp.status == 401


def test_killswitch_token_unset_allowed(tmp_path, monkeypatch):
    """Token unset → fail-open (not 401), preserves backward compatibility."""
    monkeypatch.setattr(api, "RUN_DIR", tmp_path)
    monkeypatch.setattr(api, "KILLSWITCH_FILE", tmp_path / "killswitch")
    monkeypatch.setattr(api, "cfg", _make_cfg(dashboard_token=""))
    monkeypatch.setattr(api, "_unprotected_warned", False)
    req = _FakeRequest(method="POST", headers={})
    resp = asyncio.run(api.killswitch_handler(req))
    assert resp.status != 401


def test_killswitch_bearer_token_accepted(tmp_path, monkeypatch):
    """Authorization: Bearer <token> is also accepted as an alternative to X-Dashboard-Token."""
    monkeypatch.setattr(api, "RUN_DIR", tmp_path)
    monkeypatch.setattr(api, "KILLSWITCH_FILE", tmp_path / "killswitch")
    monkeypatch.setattr(api, "cfg", _make_cfg(dashboard_token="secret123"))
    monkeypatch.setattr(api, "_unprotected_warned", False)
    req = _FakeRequest(method="POST", headers={"Authorization": "Bearer secret123"})
    resp = asyncio.run(api.killswitch_handler(req))
    assert resp.status != 401
