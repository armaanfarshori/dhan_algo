"""TEST-03 — LiveFeed.subscribe() must coerce integer SecurityIds to strings.

The biggest live incident on 2026-06-12 was Dhan's WebSocket silently dropping
every packet when integer SecurityIds were sent in the subscribe JSON.  The fix
(str(int(sid))) lives in LiveFeed.subscribe().  These tests verify that
coercion without touching the network.

dhanhq uses Python 3.10 match-syntax which fails to parse under the Mac's
system Python 3.9.  We stub the package at import time so that live_feed.py
can be imported and the pure-Python logic exercised hermetically.
"""
import asyncio
import sys
import types


# ── Stub dhanhq before any other import touches it ───────────────────────────

def _stub_dhanhq():
    """Install minimal dhanhq stubs so live_feed.py can be imported on Py 3.9."""
    # Top-level dhanhq module
    dhanhq_mod = types.ModuleType("dhanhq")

    class DhanContext:
        def __init__(self, client_id=None, access_token=None):
            pass

    dhanhq_mod.DhanContext = DhanContext
    sys.modules.setdefault("dhanhq", dhanhq_mod)

    # dhanhq.marketfeed sub-module
    mf_mod = types.ModuleType("dhanhq.marketfeed")

    class MarketFeed:
        IDX     = "IDX"
        NSE     = "NSE"
        NSE_FNO = "NSE_FNO"
        BSE_FNO = "BSE_FNO"
        MCX     = "MCX"
        Quote   = "Quote"

        def __init__(self, **kwargs):
            pass

    mf_mod.MarketFeed = MarketFeed
    sys.modules.setdefault("dhanhq.marketfeed", mf_mod)


_stub_dhanhq()

# ── Now safe to import ────────────────────────────────────────────────────────
from core.live_feed import CandleBuilder, LiveFeed  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_feed(**kwargs):
    """Construct a LiveFeed without opening any network connection.

    LiveFeed.__init__ creates an asyncio.Event(), which requires a running
    event loop on Python 3.9.  We ensure one exists before construction so
    that tests are order-independent when the full suite is collected.
    """
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    return LiveFeed(client_id="dummy_client", access_token="dummy_token", **kwargs)


# ── String coercion tests ─────────────────────────────────────────────────────

def test_subscribe_int_sid_stored_as_string():
    """Integer SIDs must be converted to strings before being queued."""
    feed = _make_feed()
    feed.subscribe({"NSE_EQ": [2885]})
    sids = feed.all_subscribed_sids()
    assert "2885" in sids
    assert 2885 not in sids


def test_subscribe_mixed_sids_all_strings():
    """Multiple integer SIDs across segments must all become strings."""
    feed = _make_feed()
    feed.subscribe({"NSE_EQ": [2885, 3045], "IDX_I": [13, 25]})
    sids = feed.all_subscribed_sids()
    for expected in ("2885", "3045", "13", "25"):
        assert expected in sids, f"Expected '{expected}' in sids, got {sids}"
    for item in sids:
        assert isinstance(item, str), f"Expected str, got {type(item)}: {item!r}"


def test_subscribe_string_sid_remains_string():
    """If a caller already passes a string ID it should still work correctly."""
    feed = _make_feed()
    feed.subscribe({"NSE_EQ": ["2885"]})
    sids = feed.all_subscribed_sids()
    assert "2885" in sids


def test_subscribe_raw_subscriptions_contain_string():
    """Internal _subscriptions list must store the string form of the SID."""
    feed = _make_feed()
    feed.subscribe({"NSE_EQ": [2885]})
    assert len(feed._subscriptions) == 1
    _, sid_stored, _ = feed._subscriptions[0]
    assert sid_stored == "2885"
    assert isinstance(sid_stored, str)


def test_subscribe_unknown_segment_skipped():
    """An unknown segment must be skipped without adding any subscription."""
    feed = _make_feed()
    feed.subscribe({"UNKNOWN_SEG": [999]})
    assert len(feed._subscriptions) == 0


def test_subscribe_multiple_calls_accumulate():
    """Calling subscribe() twice should accumulate rather than replace."""
    feed = _make_feed()
    feed.subscribe({"NSE_EQ": [2885]})
    feed.subscribe({"NSE_EQ": [3045]})
    sids = feed.all_subscribed_sids()
    assert "2885" in sids
    assert "3045" in sids


def test_is_connected_initially_false():
    """A freshly constructed feed is not connected (no WS opened)."""
    feed = _make_feed()
    assert not feed.is_connected()


def test_get_tick_unknown_sid_returns_none():
    """get_tick() on an unseen SID must return None, not raise."""
    feed = _make_feed()
    assert feed.get_tick("99999") is None


def test_get_ltp_unknown_sid_returns_zero():
    """get_ltp() on an unseen SID must return 0.0."""
    feed = _make_feed()
    assert feed.get_ltp("99999") == 0.0


def test_parse_ltt_is_ist_not_double_offset():
    """Regression (2026-06-18 prod incident): the dhanhq LTT "HH:MM:SS" string is
    already the IST wall-clock time of the trade. _parse_ltt must build it directly
    in IST — NOT treat it as UTC and astimezone(IST), which double-offset by +5:30
    and pushed every tick ~5.5h into the future, freezing the BarBuilder."""
    dt = LiveFeed._parse_ltt("11:24:00")
    assert dt is not None
    assert (dt.hour, dt.minute, dt.second) == (11, 24, 0)   # NOT 16:54
    assert dt.utcoffset().total_seconds() == 5.5 * 3600     # IST-aware


def test_parse_ltt_invalid_returns_none():
    assert LiveFeed._parse_ltt("") is None
    assert LiveFeed._parse_ltt(None) is None
    assert LiveFeed._parse_ltt("not-a-time") is None


def test_all_subscribed_sids_empty_initially():
    """Before subscribe() is called, the SID list must be empty."""
    feed = _make_feed()
    assert feed.all_subscribed_sids() == []


# ── CandleBuilder unit tests ──────────────────────────────────────────────────

def test_candle_builder_no_closed_candle_initially():
    cb = CandleBuilder()
    assert cb.last_closed is None


def test_candle_builder_tracks_current_bar():
    cb = CandleBuilder()
    cb.on_tick(100.0, volume=1000)
    cur = cb.current
    assert cur["open"] == 100.0
    assert cur["high"] == 100.0
    assert cur["low"] == 100.0
    assert cur["close"] == 100.0
    assert cur["volume"] == 1000


def test_candle_builder_high_low_update():
    """Within the same minute the high/low must expand on subsequent ticks."""
    cb = CandleBuilder()
    cb.on_tick(100.0)
    cb.on_tick(105.0)
    cb.on_tick(98.0)
    cur = cb.current
    assert cur["high"] == 105.0
    assert cur["low"] == 98.0
    assert cur["open"] == 100.0
    assert cur["close"] == 98.0


# ── QR-C3: tick-cache staleness tests ────────────────────────────────────────

def test_tick_cache_cleared_on_invalidate():
    """_invalidate_tick_cache() must remove all cached ticks and timestamps.

    QR-C3: after a WebSocket disconnect the old prices must not survive in
    _ticks so that get_tick() / get_ltp() return empty / zero, forcing the
    runner to fall back to REST rather than using pre-disconnect prices.
    """
    feed = _make_feed()
    # Inject a synthetic tick directly (bypasses network)
    feed._on_data({"security_id": "2885", "LTP": 150.0, "volume": 1000})
    assert feed.get_ltp("2885") == 150.0, "tick must be cached before invalidation"

    feed._invalidate_tick_cache()

    assert feed.get_tick("2885") is None, "tick must be gone after invalidation"
    assert feed.get_ltp("2885") == 0.0,   "ltp must be 0 (not cached) after invalidation"


def test_tick_age_none_when_cache_cold():
    """get_tick_age_s() returns None for a security that has never ticked or
    whose cache was cleared — so callers treat it as infinite age (stale).
    """
    feed = _make_feed()
    assert feed.get_tick_age_s("2885") is None


def test_tick_age_none_after_invalidation():
    """After a reconnect invalidation, get_tick_age_s() must return None even
    for a security that had a fresh tick before the disconnect.
    """
    feed = _make_feed()
    feed._on_data({"security_id": "2885", "LTP": 150.0, "volume": 1000})
    assert feed.get_tick_age_s("2885") is not None, "age must be set after tick"

    feed._invalidate_tick_cache()

    assert feed.get_tick_age_s("2885") is None, "age must be None after cache clear"


def test_tick_age_grows_over_time():
    """get_tick_age_s() must return a non-negative value that increases over
    time, so the staleness check in the runner is meaningful.
    """
    import time as _time
    feed = _make_feed()
    feed._on_data({"security_id": "2885", "LTP": 150.0, "volume": 1000})
    age1 = feed.get_tick_age_s("2885")
    _time.sleep(0.05)
    age2 = feed.get_tick_age_s("2885")
    assert age1 is not None and age1 >= 0
    assert age2 > age1, "age must grow with wall-clock time"
