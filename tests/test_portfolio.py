"""Portfolio fill accounting — averages, realized P&L, flips.
DB persistence is monkeypatched out; these test the math."""
import asyncio

import pytest

from engine.portfolio import Portfolio
from engine.types import Fill


@pytest.fixture
def pf(monkeypatch):
    p = Portfolio(mode="PAPER")

    async def _no_persist(pos):
        return None
    monkeypatch.setattr(p, "_persist", _no_persist)
    return p


def fill(side, qty, price, sid="999"):
    return Fill(security_id=sid, side=side, qty=qty, price=price)


def test_open_long(pf):
    asyncio.run(pf.apply_fill(fill("BUY", 10, 100), strategy="ORB"))
    pos = pf.get("999")
    assert pos.qty == 10 and pos.avg_price == 100
    assert pf.realized_pnl == 0


def test_add_to_long_averages(pf):
    asyncio.run(pf.apply_fill(fill("BUY", 10, 100)))
    asyncio.run(pf.apply_fill(fill("BUY", 10, 110)))
    pos = pf.get("999")
    assert pos.qty == 20 and pos.avg_price == 105


def test_close_long_realizes_pnl(pf):
    asyncio.run(pf.apply_fill(fill("BUY", 10, 100)))
    realized = asyncio.run(pf.apply_fill(fill("SELL", 10, 105)))
    assert realized == 50
    assert pf.get("999").is_flat
    assert pf.realized_pnl == 50


def test_short_cycle(pf):
    asyncio.run(pf.apply_fill(fill("SELL", 10, 100)))
    assert pf.get("999").qty == -10
    realized = asyncio.run(pf.apply_fill(fill("BUY", 10, 95)))
    assert realized == 50          # short 100 → cover 95
    assert pf.realized_pnl == 50


def test_flip_long_to_short(pf):
    asyncio.run(pf.apply_fill(fill("BUY", 10, 100)))
    realized = asyncio.run(pf.apply_fill(fill("SELL", 15, 110)))
    pos = pf.get("999")
    assert realized == 100          # 10 closed at +10 each
    assert pos.qty == -5
    assert pos.avg_price == 110     # remainder priced at the flip fill


def test_unrealized_pnl(pf):
    asyncio.run(pf.apply_fill(fill("BUY", 10, 100)))
    assert pf.unrealized_pnl(lambda sid: 103.0) == 30
    assert pf.summary(lambda sid: 103.0)["total_pnl"] == 30


# TEST-08 — partial reduce: qty shrinks, avg_price unchanged, realized P&L correct
def test_partial_reduce_long(pf):
    """Open BUY 20 @ 100, then SELL 10 @ 110 (partial reduce).
    Remaining qty must be +10, avg_price must stay 100 (reduce never reprices),
    and realized P&L must be (110 - 100) * 10 == 100."""
    asyncio.run(pf.apply_fill(fill("BUY", 20, 100), strategy="ORB"))
    realized = asyncio.run(pf.apply_fill(fill("SELL", 10, 110)))

    pos = pf.get("999")
    assert pos.qty == 10, f"expected qty 10 after partial reduce, got {pos.qty}"
    assert pos.avg_price == 100, (
        f"avg_price must not change on a reduce — expected 100, got {pos.avg_price}")
    assert realized == 100, f"expected realized P&L 100, got {realized}"
    assert pf.realized_pnl == 100
