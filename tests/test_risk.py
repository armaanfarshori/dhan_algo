"""RiskEngine — sizing math, pre-trade gate, kill switch."""
from engine.portfolio import Portfolio
from engine.risk import RiskEngine, RiskParams
from engine.types import OrderIntent, Position


def make_engine(**kw) -> RiskEngine:
    params = RiskParams(**{**dict(equity=500_000, risk_per_trade=0.01,
                                  max_notional_per_trade=100_000), **kw})
    pf = Portfolio(mode="PAPER")
    return RiskEngine(params, pf, ltp_lookup=lambda sid: 0.0)


def intent(side="BUY", qty=10, sid="999") -> OrderIntent:
    return OrderIntent(security_id=sid, exchange_segment="NSE_EQ",
                       side=side, qty=qty, strategy="ORB")


def test_stop_distance_sizing():
    rm = make_engine()
    # risk budget = 500000 × 0.01 = 5000; stop distance 2 → 2500 shares,
    # capped by notional 100000/100 = 1000
    assert rm.size_position(entry=100, stop=98) == 1000
    # wider stop: distance 10 → 500 shares, notional cap 1000 → 500
    assert rm.size_position(entry=100, stop=90) == 500


def test_sizing_without_stop_uses_notional():
    rm = make_engine()
    # fallback: 5000 / 100 = 50 shares
    assert rm.size_position(entry=100, stop=None) == 50


def test_sizing_rejects_bad_entry():
    rm = make_engine()
    assert rm.size_position(entry=0, stop=10) == 0


def test_zero_qty_blocked():
    rm = make_engine()
    ok, msg = rm.check_intent(intent(qty=0), ref_price=100)
    assert not ok and "0" in msg


def test_notional_cap():
    rm = make_engine()
    ok, msg = rm.check_intent(intent(qty=2000), ref_price=100)   # 200k > 100k
    assert not ok and "Notional" in msg


def test_kill_switch_blocks():
    rm = make_engine()
    rm.activate_kill_switch("test")
    ok, msg = rm.check_intent(intent(qty=1), ref_price=100)
    assert not ok and "halted" in msg.lower()
    rm.resume()
    ok, _ = rm.check_intent(intent(qty=1), ref_price=100)
    assert ok


def test_max_open_positions_blocks_new_entries_not_exits():
    rm = make_engine(max_open_positions=1)
    pf = rm._portfolio
    pf.positions["111"] = Position(security_id="111", qty=10, avg_price=50)

    ok, msg = rm.check_intent(intent(sid="222", qty=1), ref_price=100)
    assert not ok and "Max open positions" in msg

    # Exiting the existing position must still be allowed
    ok, _ = rm.check_intent(intent(sid="111", side="SELL", qty=10), ref_price=55)
    assert ok
