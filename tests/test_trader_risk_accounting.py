"""
Trader boot/halt risk-accounting helpers (apps.trader).

Covers two safety-critical fixes:
  • flatten_all() releases each committed-risk slot UNCONDITIONALLY on a halt —
    a failed flatten must not leak risk forever (it blocks new entries anyway).
  • reregister_position_risk() swaps the flat boot placeholder for the TRUE
    stop-distance risk once each ORB's opening range is rebuilt.

All fakes are hermetic — no DB, no network.
"""
import asyncio
from typing import Optional

from engine.risk import RiskEngine, RiskParams
from engine.types import Fill, OrderIntent, Position
from strategies.orb import ORB, ORBParams

from apps.trader import flatten_all, reregister_position_risk, make_ltp_lookup


# ─── Fakes ──────────────────────────────────────────────────────────────────

class FakePortfolio:
    def __init__(self, positions: dict):
        self._positions = positions
        self.fills_applied: list = []

    def get(self, sid: str) -> Position:
        return self._positions.get(sid, Position(security_id=sid, qty=0))

    async def apply_fill(self, fill: Fill, *, strategy: str = "ORB") -> float:
        self.fills_applied.append(fill)
        return 0.0


class FakeExecutor:
    """Returns a Fill for every submit, unless fill_sids excludes the sid."""

    def __init__(self, fill_sids: Optional[set] = None):
        self._fill_sids = fill_sids   # None = fill everything
        self.submitted: list = []

    async def submit(self, intent: OrderIntent, *, ref_price: float):
        self.submitted.append(intent)
        if self._fill_sids is not None and intent.security_id not in self._fill_sids:
            return None
        return Fill(security_id=intent.security_id, side=intent.side,
                    qty=intent.qty, price=ref_price)


class FakeRunner:
    def __init__(self, sid: str, strategy, last_price: float):
        self.sid = sid
        self.strategy = strategy
        self.last_price = last_price


def _risk_engine(portfolio) -> RiskEngine:
    return RiskEngine(RiskParams(equity_base=500_000), portfolio,
                      ltp_lookup=lambda sid: 0.0)


# ─── flatten_all (bug c) ──────────────────────────────────────────────────────

def test_flatten_all_releases_risk_even_when_flatten_fails():
    """A failed flatten (executor returns None) must STILL release the slot."""
    pos = Position(security_id="111", qty=10, avg_price=100.0)
    port = FakePortfolio({"111": pos})
    risk = _risk_engine(port)
    risk.register_risk("111", 3_000)
    assert risk.committed_risk == 3_000

    orb = ORB("111")
    runner = FakeRunner("111", orb, last_price=95.0)
    # Executor fills NOTHING → flatten fails.
    executor = FakeExecutor(fill_sids=set())

    asyncio.run(flatten_all([runner], port, executor, risk, "daily loss", "NSE_EQ"))

    assert risk.committed_risk == 0          # slot released despite failure
    assert len(port.fills_applied) == 0      # no fill applied
    assert orb.position == 0 or orb.position == 0  # strategy untouched on failure


def test_flatten_all_applies_fill_and_releases_on_success():
    pos = Position(security_id="111", qty=10, avg_price=100.0)
    port = FakePortfolio({"111": pos})
    risk = _risk_engine(port)
    risk.register_risk("111", 3_000)

    orb = ORB("111")
    orb.position = 10
    runner = FakeRunner("111", orb, last_price=95.0)
    executor = FakeExecutor(fill_sids=None)   # fills everything

    asyncio.run(flatten_all([runner], port, executor, risk, "daily loss", "NSE_EQ"))

    assert risk.committed_risk == 0
    assert len(port.fills_applied) == 1
    assert orb.position == 0                  # notify_flat ran


def test_flatten_all_uses_avg_price_when_last_price_zero():
    """A never-ticked position (last_price==0) must be flattened on its entry
    avg_price — the executor rejects ref_price<=0, so without this fallback the
    position would be left naked past the kill switch."""
    pos = Position(security_id="111", qty=8, avg_price=123.5)
    port = FakePortfolio({"111": pos})
    risk = _risk_engine(port)
    risk.register_risk("111", 2_000)

    orb = ORB("111")
    orb.position = 8
    runner = FakeRunner("111", orb, last_price=0.0)   # never ticked
    executor = FakeExecutor(fill_sids=None)           # fills everything

    asyncio.run(flatten_all([runner], port, executor, risk, "kill", "NSE_EQ"))

    assert len(executor.submitted) == 1
    submitted = executor.submitted[0]
    assert submitted.side == "SELL"
    assert submitted.qty == 8
    # ref_price was the avg_price (the FakeExecutor stamps ref_price onto the fill)
    assert port.fills_applied[0].price == 123.5
    assert risk.committed_risk == 0
    assert orb.position == 0


def test_flatten_all_skips_when_no_price_and_no_avg_but_releases_risk():
    """No last_price AND no avg_price → cannot submit (ref<=0), but the risk slot
    is still released unconditionally."""
    pos = Position(security_id="111", qty=4, avg_price=0.0)
    port = FakePortfolio({"111": pos})
    risk = _risk_engine(port)
    risk.register_risk("111", 1_000)
    orb = ORB("111")
    runner = FakeRunner("111", orb, last_price=0.0)
    executor = FakeExecutor(fill_sids=None)

    asyncio.run(flatten_all([runner], port, executor, risk, "kill", "NSE_EQ"))

    assert len(executor.submitted) == 0   # never submitted (ref_price<=0)
    assert risk.committed_risk == 0       # slot still released


def test_flatten_all_skips_flat_positions_but_still_clears_risk_map():
    """A position already flat is not re-submitted; nothing to release."""
    port = FakePortfolio({"111": Position(security_id="111", qty=0)})
    risk = _risk_engine(port)
    orb = ORB("111")
    runner = FakeRunner("111", orb, last_price=95.0)
    executor = FakeExecutor()

    asyncio.run(flatten_all([runner], port, executor, risk, "kill", "NSE_EQ"))

    assert len(executor.submitted) == 0
    assert risk.committed_risk == 0


# ─── make_ltp_lookup — stale-LTP risk guard (QA P1) ──────────────────────────

class _FakeFeed:
    def __init__(self, ltp: float, age):
        self._ltp = ltp
        self._age = age

    def get_ltp(self, sid: str) -> float:
        return self._ltp

    def get_tick_age_s(self, sid: str):
        return self._age


def test_ltp_lookup_uses_fresh_ws_price():
    feed = _FakeFeed(ltp=150.0, age=10.0)        # fresh (age <= 60)
    runner = FakeRunner("111", ORB("111"), last_price=140.0)
    lookup = make_ltp_lookup(feed, [runner], stale_s=60.0)
    assert lookup("111") == 150.0                # fresh WS price wins


def test_ltp_lookup_falls_back_to_runner_last_price_when_stale():
    feed = _FakeFeed(ltp=150.0, age=120.0)       # stale (age > 60)
    runner = FakeRunner("111", ORB("111"), last_price=140.0)
    lookup = make_ltp_lookup(feed, [runner], stale_s=60.0)
    assert lookup("111") == 140.0                # falls back to REST-refreshed last_price


def test_ltp_lookup_falls_back_when_age_none():
    feed = _FakeFeed(ltp=150.0, age=None)        # cold cache → treat as stale
    runner = FakeRunner("111", ORB("111"), last_price=140.0)
    lookup = make_ltp_lookup(feed, [runner], stale_s=60.0)
    assert lookup("111") == 140.0


def test_ltp_lookup_warns_and_uses_stale_ws_when_no_fallback(caplog):
    """No fresh price and no runner last_price → warn (rate-limited) and prefer
    the stale non-zero WS price over 0 (never price a held position as worthless)."""
    import logging
    feed = _FakeFeed(ltp=150.0, age=120.0)       # stale WS price, no fallback
    runner = FakeRunner("111", ORB("111"), last_price=0.0)
    lookup = make_ltp_lookup(feed, [runner], stale_s=60.0, warn_every_s=60.0)

    with caplog.at_level(logging.WARNING, logger="dhan.trader"):
        first = lookup("111")
        second = lookup("111")                   # within warn window → no 2nd warning

    assert first == 150.0                         # stale WS price preferred over 0
    assert second == 150.0
    warnings = [r for r in caplog.records if "no fresh price" in r.message]
    assert len(warnings) == 1, "warning must be rate-limited (once per window)"


def test_ltp_lookup_returns_zero_when_nothing_available():
    feed = _FakeFeed(ltp=0.0, age=None)          # no WS price at all
    runner = FakeRunner("111", ORB("111"), last_price=0.0)
    lookup = make_ltp_lookup(feed, [runner], stale_s=60.0)
    assert lookup("111") == 0.0


# ─── reregister_position_risk (bug d) ─────────────────────────────────────────

def test_reregister_uses_real_stop_distance_after_or_seed():
    """After the OR is seeded, the long position's risk uses the real ORB SL
    edge (or_low * (1 - sl_buffer_pct)), not the flat placeholder."""
    avg_price = 110.0
    qty = 20
    pos = Position(security_id="111", qty=qty, avg_price=avg_price)
    port = FakePortfolio({"111": pos})
    risk = _risk_engine(port)
    # Boot placeholder (flat budget).
    risk.register_risk("111", risk.risk_budget_per_trade)

    orb = ORB("111", ORBParams(sl_buffer_pct=0.002))
    orb.seed_opening_range(__import__("datetime").date.today(), high=112.0, low=105.0)
    assert orb.or_locked
    runner = FakeRunner("111", orb, last_price=110.0)

    reregister_position_risk([runner], port, risk)

    expected_stop = 105.0 * (1 - 0.002)
    expected_risk = risk.position_risk(avg_price, expected_stop, qty)
    assert abs(risk.committed_risk - expected_risk) < 1e-6
    # And it differs from the placeholder (proves the swap happened).
    assert abs(risk.committed_risk - risk.risk_budget_per_trade) > 1.0


def test_reregister_short_uses_upper_stop():
    avg_price = 90.0
    qty = 15
    pos = Position(security_id="111", qty=-qty, avg_price=avg_price)
    port = FakePortfolio({"111": pos})
    risk = _risk_engine(port)
    risk.register_risk("111", risk.risk_budget_per_trade)

    orb = ORB("111", ORBParams(sl_buffer_pct=0.002))
    orb.seed_opening_range(__import__("datetime").date.today(), high=95.0, low=88.0)
    runner = FakeRunner("111", orb, last_price=90.0)

    reregister_position_risk([runner], port, risk)

    expected_stop = 95.0 * (1 + 0.002)
    expected_risk = risk.position_risk(avg_price, expected_stop, qty)
    assert abs(risk.committed_risk - expected_risk) < 1e-6


def test_reregister_keeps_placeholder_when_or_not_locked():
    """If the OR never recovered, the conservative placeholder is kept."""
    pos = Position(security_id="111", qty=10, avg_price=100.0)
    port = FakePortfolio({"111": pos})
    risk = _risk_engine(port)
    placeholder = risk.risk_budget_per_trade
    risk.register_risk("111", placeholder)

    orb = ORB("111")             # never seeded → or_locked False
    assert not orb.or_locked
    runner = FakeRunner("111", orb, last_price=100.0)

    reregister_position_risk([runner], port, risk)

    assert risk.committed_risk == placeholder   # unchanged


def test_reregister_ignores_flat_positions():
    port = FakePortfolio({"111": Position(security_id="111", qty=0)})
    risk = _risk_engine(port)
    orb = ORB("111")
    orb.seed_opening_range(__import__("datetime").date.today(), high=112.0, low=105.0)
    runner = FakeRunner("111", orb, last_price=110.0)

    reregister_position_risk([runner], port, risk)

    assert risk.committed_risk == 0
