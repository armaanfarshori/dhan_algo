"""Static-egress proxy — Dhan whitelists ONE public IP for order placement.

The platform now runs on a CGNAT home line, so order REST calls are routed
through an HTTP proxy on a whitelisted host while everything else (data,
quotes, historicals, the WebSocket feed) stays direct — the proxy is a scarce
single-point-of-failure hop and data traffic is orders of magnitude heavier.

These tests pin the three things that can silently break that:
  1. which rate categories actually carry the proxy kwarg (core/client.py),
  2. how DHAN_PROXY_CATEGORIES parses (config.py),
  3. that scripts/egress_check.py returns the right exit code per failure mode.
"""
import asyncio

import pytest

from config import Config
from core.client import DhanClient


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, body=None):
        self._body = body if body is not None else {"status": "ok"}
        self.status = 200
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self, content_type=None): return self._body
    def raise_for_status(self): return None


class _RecordingSession:
    """Records the kwargs of every request so the proxy routing is inspectable."""
    def __init__(self):
        self.kwargs = []
    def request(self, method, url, **kwargs):
        self.kwargs.append(kwargs)
        return _FakeResp()
    @property
    def last_proxy(self):
        return self.kwargs[-1]["proxy"]


def _client(**kw) -> DhanClient:
    c = DhanClient(client_id="cid", access_token="tok", **kw)
    c._session = _RecordingSession()
    return c


def _get(client, category):
    """Drive one request through _request at the given rate category."""
    return asyncio.run(client._request("GET", "some/endpoint", category))


# ── Client routing ────────────────────────────────────────────────────────────

PROXY = "http://100.64.0.1:8888"


def test_orders_go_through_proxy_by_default():
    """Default categories = {"orders"} — the only IP-gated Dhan traffic."""
    c = _client(proxy_url=PROXY)
    _get(c, "orders")
    assert c._session.last_proxy == PROXY


def test_data_stays_direct_by_default():
    """Market data works from any IP; routing it would waste the proxy's link."""
    c = _client(proxy_url=PROXY)
    _get(c, "data")
    assert c._session.last_proxy is None


def test_quote_and_non_trading_stay_direct_by_default():
    c = _client(proxy_url=PROXY)
    for category in ("quote", "non_trading"):
        _get(c, category)
        assert c._session.last_proxy is None, category


def test_all_sentinel_routes_every_category():
    """`all` is the escape hatch for a box whose entire egress must be pinned."""
    c = _client(proxy_url=PROXY, proxy_categories={"all"})
    for category in ("orders", "data", "quote", "non_trading"):
        _get(c, category)
        assert c._session.last_proxy == PROXY, category


def test_explicit_category_set_is_honoured():
    c = _client(proxy_url=PROXY, proxy_categories={"orders", "non_trading"})
    _get(c, "non_trading")
    assert c._session.last_proxy == PROXY
    _get(c, "data")
    assert c._session.last_proxy is None


def test_categories_are_case_insensitive():
    c = _client(proxy_url=PROXY, proxy_categories={"ORDERS"})
    _get(c, "orders")
    assert c._session.last_proxy == PROXY


def test_no_proxy_url_means_direct_for_every_category():
    """Back-compat: an unconfigured client must behave byte-identically."""
    c = _client()
    for category in ("orders", "data", "quote", "non_trading"):
        _get(c, category)
        assert c._session.last_proxy is None, category


def test_empty_proxy_url_is_treated_as_unset():
    """cfg.dhan_proxy_url defaults to "" — the empty string must not proxy."""
    c = _client(proxy_url="")
    _get(c, "orders")
    assert c._session.last_proxy is None


# ── Config parsing ────────────────────────────────────────────────────────────

def test_proxy_defaults_are_back_compatible():
    cfg = Config(_env_file=None)
    assert cfg.dhan_proxy_url == "", "no proxy unless deliberately configured"
    assert cfg.dhan_whitelisted_egress_ip == ""
    assert cfg.dhan_proxy_categories_set == {"orders"}


@pytest.mark.parametrize("raw,expected", [
    ("orders", {"orders"}),
    (" all ", {"all"}),
    ("orders, data", {"orders", "data"}),
    ("ORDERS,Data", {"orders", "data"}),
    ("orders,,", {"orders"}),
    ("", set()),
])
def test_proxy_categories_parse(monkeypatch, raw, expected):
    monkeypatch.setenv("DHAN_PROXY_CATEGORIES", raw)
    assert Config(_env_file=None).dhan_proxy_categories_set == expected


def test_proxy_url_env_override(monkeypatch):
    monkeypatch.setenv("DHAN_PROXY_URL", PROXY)
    monkeypatch.setenv("DHAN_WHITELISTED_EGRESS_IP", "203.0.113.7")
    cfg = Config(_env_file=None)
    assert cfg.dhan_proxy_url == PROXY
    assert cfg.dhan_whitelisted_egress_ip == "203.0.113.7"


# ── egress_check ──────────────────────────────────────────────────────────────

@pytest.fixture
def egress(monkeypatch):
    """scripts/egress_check.main() with config + network + Telegram stubbed.

    Returns a helper that configures the module and runs main(), collecting any
    alert text so the tests can assert an operator would actually be told.
    """
    import scripts.egress_check as mod

    sent: list[str] = []
    monkeypatch.setattr(mod, "_alert", sent.append)

    def run(proxy_url, expected_ip, result):
        """result: an IP string to return, or an Exception to raise."""
        cfg = Config(_env_file=None).model_copy(update={
            "dhan_proxy_url": proxy_url,
            "dhan_whitelisted_egress_ip": expected_ip,
        })
        monkeypatch.setattr("config.get_config", lambda: cfg)

        async def _fake_fetch(url):
            assert url == proxy_url
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(mod, "_fetch_egress_ip", _fake_fetch)
        return mod.main(), sent

    return run


def test_egress_check_skips_when_unconfigured(egress):
    """No proxy configured = direct egress; a noisy alert would train the
    operator to ignore this check."""
    code, sent = egress("", "", "203.0.113.7")
    assert code == 0
    assert sent == []


def test_egress_check_skips_when_expected_ip_missing(egress):
    code, sent = egress("http://100.64.0.1:8888", "", "203.0.113.7")
    assert code == 0
    assert sent == []


def test_egress_check_match(egress):
    code, sent = egress("http://100.64.0.1:8888", "203.0.113.7", "203.0.113.7")
    assert code == 0
    assert sent == []


def test_egress_check_mismatch_alerts(egress):
    """Wrong IP = every order of the session will be rejected — must alert."""
    code, sent = egress("http://100.64.0.1:8888", "203.0.113.7", "198.51.100.9")
    assert code == 1
    assert len(sent) == 1
    assert "198.51.100.9" in sent[0]
    # Never leak the whitelisted IP into an outbound message.
    assert "203.0.113.7" not in sent[0]


def test_egress_check_unreachable_alerts(egress):
    code, sent = egress(
        "http://100.64.0.1:8888", "203.0.113.7", OSError("connect refused"),
    )
    assert code == 2
    assert len(sent) == 1
    assert "unreachable" in sent[0].lower()


def test_egress_check_alert_failure_does_not_mask_exit_code(monkeypatch):
    """Telegram being down must not turn a real failure into a crash."""
    import scripts.egress_check as mod

    cfg = Config(_env_file=None).model_copy(update={
        "dhan_proxy_url": "http://100.64.0.1:8888",
        "dhan_whitelisted_egress_ip": "203.0.113.7",
    })
    monkeypatch.setattr("config.get_config", lambda: cfg)

    async def _fake_fetch(_url):
        return "198.51.100.9"

    monkeypatch.setattr(mod, "_fetch_egress_ip", _fake_fetch)
    monkeypatch.setattr("core.notify.send", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no net")))
    assert mod.main() == 1
