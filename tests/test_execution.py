"""PaperExecutor — slippage direction and rejection on missing price."""
import asyncio

from engine.execution import PaperExecutor
from engine.types import OrderIntent


def intent(side):
    return OrderIntent(security_id="999", exchange_segment="NSE_EQ",
                       side=side, qty=10, strategy="ORB")


def test_buy_pays_up():
    ex = PaperExecutor(slippage_bps=10)         # 0.1%
    f = asyncio.run(ex.submit(intent("BUY"), ref_price=100.0))
    assert f is not None
    assert f.price == 100.10
    assert f.mode == "PAPER"


def test_sell_receives_less():
    ex = PaperExecutor(slippage_bps=10)
    f = asyncio.run(ex.submit(intent("SELL"), ref_price=100.0))
    assert f.price == 99.90


def test_rejects_zero_price():
    ex = PaperExecutor()
    assert asyncio.run(ex.submit(intent("BUY"), ref_price=0.0)) is None
