"""QA finding H4 — LiveExecutor fill-confirmation failure modes.

H4 (High): when an order never confirms within the poll budget, LiveExecutor
           books a Fill at the REFERENCE price (flagged CRITICAL) and relies on
           the boot/EOD broker reconcile to correct drift. If the order in fact
           rejected just after the poll window, a phantom position is tracked
           until the next reconcile. These tests pin the documented behavior so
           a future change to the confirmation policy is caught.
"""
import asyncio

from engine.execution import LiveExecutor
from engine.types import OrderIntent


def _intent():
    return OrderIntent(security_id="111", exchange_segment="NSE_EQ",
                       side="BUY", qty=10, strategy="ORB", reason="test")


class _Client:
    """Place succeeds; order-detail status is scripted."""
    def __init__(self, status_sequence):
        self._seq = list(status_sequence)
    async def place_order(self, **kw):
        return {"orderId": "OID1"}
    async def get_order_by_id(self, order_id):
        st = self._seq.pop(0) if self._seq else "PENDING"
        return {"data": {"orderStatus": st, "averageTradedPrice": 101.0, "filledQty": 10}}


def _fast(ex):
    # Collapse the confirmation waits so tests don't actually sleep ~8s.
    ex._CONFIRM_WAITS = [0, 0, 0]
    return ex


def test_confirmed_fill_uses_broker_average_price():
    """Regression: a TRADED order books the broker's average price, not ref."""
    ex = _fast(LiveExecutor(_Client(["TRADED"])))
    fill = asyncio.run(ex.submit(_intent(), ref_price=100.0))
    assert fill is not None and fill.price == 101.0 and fill.qty == 10


def test_rejected_order_returns_no_fill():
    """Regression: a REJECTED order books nothing (None)."""
    ex = _fast(LiveExecutor(_Client(["REJECTED"])))
    fill = asyncio.run(ex.submit(_intent(), ref_price=100.0))
    assert fill is None


def test_unconfirmed_order_books_phantom_fill_at_ref():
    """Characterization of H4: an order that never confirms is booked at the
    reference price (so the position is tracked pending reconcile). If the order
    actually rejected, this is a phantom until the next broker reconcile."""
    ex = _fast(LiveExecutor(_Client(["PENDING", "PENDING", "PENDING"])))
    fill = asyncio.run(ex.submit(_intent(), ref_price=100.0))
    assert fill is not None
    assert fill.price == 100.0        # reference price, not a broker fill
    assert fill.order_id == "OID1"
