"""ORB strategy logic — opening range build/lock, breakout entries, shadow gate,
EOD square-off. Time is frozen via monkeypatching the module's datetime."""
import asyncio
from datetime import datetime as real_datetime

import pytest

import strategies.strategy_orb as orb_mod
from strategies.strategy_base import StrategyConfig
from strategies.strategy_orb import ORBStrategy, ORBConfig


class FakeKronos:
    """Stub Kronos engine with a scripted verdict."""
    def __init__(self, side="BUY", confidence=0.9):
        self.side = side
        self.confidence = confidence
        self.calls = 0

    async def score_from_db(self, **_kw):
        self.calls += 1
        return {"side": self.side, "confidence": self.confidence,
                "forecasted_return": 0.01}


class AllowAllRisk:
    def check_order(self, *_a, **_k):
        return True, "OK"


def make_strategy(shadow=True, kronos=None, use_kronos=True):
    cfg = StrategyConfig(name="ORB_TEST", security_id="999", quantity=1,
                         paper_trading=True, max_orders=4)
    ocfg = ORBConfig(orb_minutes=15, use_kronos=use_kronos,
                     kronos_min_confidence=0.4, kronos_shadow=shadow)
    return ORBStrategy(client=None, risk_manager=AllowAllRisk(), config=cfg,
                       orb_config=ocfg, kronos_engine=kronos)


def freeze_time(monkeypatch, hh, mm):
    class _FrozenDT(real_datetime):
        @classmethod
        def now(cls, tz=None):
            base = real_datetime(2026, 6, 11, hh, mm, 0)
            return base.replace(tzinfo=tz) if tz else base
    monkeypatch.setattr(orb_mod, "datetime", _FrozenDT)


def tick(price, high=None, low=None):
    return {"last_price": price,
            "ohlc": {"open": price, "high": high or price,
                     "low": low or price, "close": price}}


def test_or_builds_during_window_no_trade(monkeypatch):
    s = make_strategy()
    freeze_time(monkeypatch, 9, 20)
    sig = asyncio.run(s.on_tick(tick(101, high=102, low=100)))
    assert sig is None
    assert s._or_high == 102 and s._or_low == 100
    assert not s._or_locked
    assert s.position == 0


def test_breakout_enters_long_after_lock(monkeypatch):
    s = make_strategy(kronos=FakeKronos(side="BUY", confidence=0.9))
    freeze_time(monkeypatch, 9, 20)
    asyncio.run(s.on_tick(tick(101, high=102, low=100)))
    freeze_time(monkeypatch, 9, 31)
    sig = asyncio.run(s.on_tick(tick(103)))
    assert s._or_locked
    assert sig is not None and sig.action == "BUY"
    assert s.position == 1
    assert s.entry_price == 103


def test_shadow_mode_never_blocks(monkeypatch):
    """Kronos disagrees AND has high confidence — shadow mode must still allow."""
    kronos = FakeKronos(side="SELL", confidence=0.99)
    s = make_strategy(shadow=True, kronos=kronos)
    freeze_time(monkeypatch, 9, 20)
    asyncio.run(s.on_tick(tick(101, high=102, low=100)))
    freeze_time(monkeypatch, 9, 31)
    sig = asyncio.run(s.on_tick(tick(103)))
    assert kronos.calls == 1, "gate must still be scored (calibration data)"
    assert sig is not None and sig.action == "BUY", "shadow gate must not block"


def test_enforcing_mode_blocks_disagreement(monkeypatch):
    kronos = FakeKronos(side="SELL", confidence=0.99)
    s = make_strategy(shadow=False, kronos=kronos)
    freeze_time(monkeypatch, 9, 20)
    asyncio.run(s.on_tick(tick(101, high=102, low=100)))
    freeze_time(monkeypatch, 9, 31)
    sig = asyncio.run(s.on_tick(tick(103)))
    assert sig is None
    assert s.position == 0
    assert s._long_taken, "blocked direction is consumed for the session"


def test_eod_squareoff(monkeypatch):
    s = make_strategy(kronos=FakeKronos())
    freeze_time(monkeypatch, 9, 20)
    asyncio.run(s.on_tick(tick(101, high=102, low=100)))
    freeze_time(monkeypatch, 9, 31)
    asyncio.run(s.on_tick(tick(103)))
    assert s.position == 1
    freeze_time(monkeypatch, 15, 16)   # past 15:15 square-off
    sig = asyncio.run(s.on_tick(tick(104)))
    assert sig is not None and sig.action == "EXIT"
    assert s.position == 0


def test_narrow_range_skipped(monkeypatch):
    """OR range below min_range_pct of price → no entry."""
    s = make_strategy(kronos=FakeKronos())
    freeze_time(monkeypatch, 9, 20)
    asyncio.run(s.on_tick(tick(1000.1, high=1000.2, low=1000.0)))  # range 0.2 < 0.3% of 1000
    freeze_time(monkeypatch, 9, 31)
    sig = asyncio.run(s.on_tick(tick(1001)))
    assert sig is None
    assert s.position == 0


def test_orb_45min_window_no_crash(monkeypatch):
    """orb_minutes ≥ 45 used to raise ValueError (dtime minute overflow)."""
    cfg = StrategyConfig(name="ORB_TEST", security_id="999", quantity=1, paper_trading=True)
    ocfg = ORBConfig(orb_minutes=45, use_kronos=False)
    s = ORBStrategy(client=None, risk_manager=AllowAllRisk(), config=cfg, orb_config=ocfg)
    freeze_time(monkeypatch, 9, 30)
    sig = asyncio.run(s.on_tick(tick(100, high=101, low=99)))
    assert sig is None and not s._or_locked   # still inside the 45-min window
