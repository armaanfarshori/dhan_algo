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

from apps.trader import flatten_all, reregister_position_risk


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
