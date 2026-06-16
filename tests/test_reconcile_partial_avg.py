"""QA finding M3 — broker reconcile crashes on a partial average-price row.

M3 (Medium): reconcile_with_broker computes
    avg = float(p.get("buyAvg") if qty > 0 else p.get("sellAvg")) ...
If the broker returns netQty>0 but buyAvg is null (only sellAvg / costPrice
present), this is float(None) → TypeError, aborting the LIVE boot reconcile
that is supposed to establish broker-truth before trading.
"""
import asyncio

import pytest

from engine.portfolio import Portfolio


class _FakeBroker:
    def __init__(self, positions):
        self._positions = positions
    async def get_positions(self):
        return self._positions


@pytest.fixture
def live_pf(monkeypatch):
    pf = Portfolio(mode="LIVE")
    async def _no_persist(pos):
        return None
    monkeypatch.setattr(pf, "_persist", _no_persist)
    return pf


def test_reconcile_uses_costprice_when_avgs_present(live_pf):
    """Regression: a normal long row with buyAvg is adopted cleanly."""
    broker = _FakeBroker([{"securityId": "999", "netQty": 10, "buyAvg": 101.0}])
    asyncio.run(live_pf.reconcile_with_broker(broker))
    assert live_pf.get("999").qty == 10
    assert live_pf.get("999").avg_price == 101.0


@pytest.mark.xfail(strict=True,
                   reason="M3: float(None) when a long row has no buyAvg "
                          "(only sellAvg/costPrice) → reconcile aborts")
def test_reconcile_survives_missing_buy_avg(live_pf):
    # Long position but buyAvg absent — broker reported only sellAvg + costPrice.
    broker = _FakeBroker([{"securityId": "999", "netQty": 10,
                           "buyAvg": None, "sellAvg": 50.0, "costPrice": 100.0}])
    asyncio.run(live_pf.reconcile_with_broker(broker))   # must not raise
    assert live_pf.get("999").qty == 10
