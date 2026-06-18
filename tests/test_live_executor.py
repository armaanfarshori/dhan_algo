"""LiveExecutor fill confirmation + LIVE broker reconcile — the paper→live path."""
import asyncio

import pytest

from engine.execution import LiveExecutor
from engine.portfolio import Portfolio, Position
from engine.types import OrderIntent


def intent(side="BUY", qty=10):
    return OrderIntent(security_id="999", exchange_segment="NSE_EQ",
                       side=side, qty=qty, strategy="ORB")


class FakeDhan:
    """Scripted broker: order placement + order-detail responses."""
    def __init__(self, statuses, avg_price=101.25, fail_place=False):
        self.statuses = list(statuses)
        self.avg_price = avg_price
        self.fail_place = fail_place
        self.placed = []

    async def place_order(self, **kw):
        if self.fail_place:
            raise RuntimeError("DH-906 margin")
        self.placed.append(kw)
        return {"orderId": "OID-1"}

    async def get_order_by_id(self, order_id):
        status = self.statuses.pop(0) if self.statuses else "PENDING"
        return {"data": {"orderStatus": status,
                         "averageTradedPrice": self.avg_price,
                         "filledQty": 10}}


def fast_executor(client):
    ex = LiveExecutor(client)
    ex._CONFIRM_WAITS = [0.01, 0.01]   # don't sleep in tests
    return ex


def test_rejects_zero_qty_no_order_sent():
    """A zero-qty intent must short-circuit before any broker call."""
    broker = FakeDhan(["TRADED"])
    ex = fast_executor(broker)
    assert asyncio.run(ex.submit(intent(qty=0), ref_price=100.0)) is None
    assert broker.placed == []          # no order was placed


def test_rejects_negative_qty_no_order_sent():
    broker = FakeDhan(["TRADED"])
    ex = fast_executor(broker)
    assert asyncio.run(ex.submit(intent(qty=-3), ref_price=100.0)) is None
    assert broker.placed == []


def test_confirmed_fill_uses_broker_avg_price():
    client = FakeDhan(["TRADED"], avg_price=101.25)
    f = asyncio.run(fast_executor(client).submit(intent(), ref_price=100.0))
    assert f is not None
    assert f.price == 101.25        # broker truth, not the reference tick
    assert f.order_id == "OID-1" and f.mode == "LIVE"


def test_rejected_order_returns_none():
    client = FakeDhan(["REJECTED"])
    f = asyncio.run(fast_executor(client).submit(intent(), ref_price=100.0))
    assert f is None


def test_pending_falls_back_to_ref_loudly():
    client = FakeDhan(["TRANSIT", "PENDING"])
    f = asyncio.run(fast_executor(client).submit(intent(), ref_price=100.0))
    assert f is not None and f.price == 100.0   # reconcile corrects later


def test_place_failure_returns_none():
    client = FakeDhan([], fail_place=True)
    f = asyncio.run(fast_executor(client).submit(intent(), ref_price=100.0))
    assert f is None


# ── Broker reconcile ──────────────────────────────────────────────────────────

class FakeBroker:
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


def test_broker_truth_adopted_on_mismatch(live_pf):
    live_pf.positions["999"] = Position(security_id="999", qty=10, avg_price=100)
    broker = FakeBroker([{"securityId": "999", "netQty": 15, "buyAvg": 101.0}])
    asyncio.run(live_pf.reconcile_with_broker(broker))
    assert live_pf.get("999").qty == 15
    assert live_pf.get("999").avg_price == 101.0


def test_broker_flat_clears_engine_position(live_pf):
    live_pf.positions["999"] = Position(security_id="999", qty=10, avg_price=100)
    asyncio.run(live_pf.reconcile_with_broker(FakeBroker([])))
    assert live_pf.get("999").is_flat


def test_paper_mode_is_noop(monkeypatch):
    pf = Portfolio(mode="PAPER")
    pf.positions["999"] = Position(security_id="999", qty=10, avg_price=100)
    # client would blow up if called — paper must never touch the broker
    class Boom:
        async def get_positions(self):
            raise AssertionError("paper mode must not query the broker")
    asyncio.run(pf.reconcile_with_broker(Boom()))
    assert pf.get("999").qty == 10


# ── QR-C4: orders audit trail for failed/rejected LIVE placements ─────────────

class _CapturingDB:
    """Minimal AsyncDBBackend stand-in that records every log_order call."""
    def __init__(self):
        self.calls: list[dict] = []

    async def log_order(self, **kw):
        self.calls.append(kw)


class _DhanRejected:
    """Broker that places successfully but immediately rejects at confirmation."""
    def __init__(self, correlation_id="CORR-REJECT-1"):
        self._cid = correlation_id

    async def place_order(self, **kw):
        return {"orderId": "OID-REJ", "correlationId": self._cid}

    async def get_order_by_id(self, order_id):
        return {"data": {"orderStatus": "REJECTED",
                         "omsErrorDescription": "Insufficient funds"}}


class _DhanNetworkFail:
    """Broker whose place_order raises a network error with an attached correlation_id."""
    def __init__(self, correlation_id="CORR-NET-1"):
        self._cid = correlation_id

    async def place_order(self, **kw):
        exc = RuntimeError("DH-906 network timeout")
        exc._correlation_id = self._cid  # as client.py would set it
        raise exc

    async def get_order_by_id(self, order_id):  # should never be called
        raise AssertionError("get_order_by_id must not be called on network failure")


def _fast_ex(client, db):
    ex = LiveExecutor(client, db_backend=db)
    ex._CONFIRM_WAITS = [0.01]
    return ex


def test_rejected_live_order_writes_orders_row():
    """QR-C4: a REJECTED live order must write an orders row with status=REJECTED
    and preserve the dhan_order_id + correlation_id as client_order_id."""
    db = _CapturingDB()
    client = _DhanRejected(correlation_id="CORR-REJECT-1")
    ex = _fast_ex(client, db)

    fill = asyncio.run(ex.submit(intent(), ref_price=100.0))

    assert fill is None, "REJECTED order must return no fill"
    assert len(db.calls) == 1, "exactly one orders row must be written"
    row = db.calls[0]
    assert row["status"] == "REJECTED"
    assert row["dhan_order_id"] == "OID-REJ"
    assert row["client_order_id"] == "CORR-REJECT-1"
    assert row["mode"] == "LIVE"
    assert row["security_id"] == "999"


def test_network_failed_live_order_writes_orders_row():
    """QR-C4: a network-failed live placement must write an orders row with
    status=FAILED and the correlation_id surfaced by the client exception."""
    db = _CapturingDB()
    client = _DhanNetworkFail(correlation_id="CORR-NET-1")
    ex = _fast_ex(client, db)

    fill = asyncio.run(ex.submit(intent(), ref_price=100.0))

    assert fill is None, "network-failed order must return no fill"
    assert len(db.calls) == 1, "exactly one orders row must be written"
    row = db.calls[0]
    assert row["status"] == "FAILED"
    assert row["dhan_order_id"] is None
    assert row["client_order_id"] == "CORR-NET-1"
    assert row["mode"] == "LIVE"
    assert row["security_id"] == "999"


def test_successful_live_order_still_writes_orders_row():
    """QR-C4 non-regression: a successful trade still writes its orders row
    with status=TRADED and correct correlation_id."""
    db = _CapturingDB()

    class _DhanTraded:
        async def place_order(self, **kw):
            return {"orderId": "OID-OK", "correlationId": "CORR-OK-1"}

        async def get_order_by_id(self, order_id):
            return {"data": {"orderStatus": "TRADED",
                             "averageTradedPrice": 101.5, "filledQty": 10}}

    ex = _fast_ex(_DhanTraded(), db)
    fill = asyncio.run(ex.submit(intent(), ref_price=100.0))

    assert fill is not None and fill.price == 101.5
    assert len(db.calls) == 1
    row = db.calls[0]
    assert row["status"] == "TRADED"
    assert row["dhan_order_id"] == "OID-OK"
    assert row["client_order_id"] == "CORR-OK-1"
