"""Equity intraday cost stack — hand-checked numbers."""
from research.backtest.costs import order_brokerage, round_trip_costs


def test_brokerage_cap():
    # Small order: 0.03% of 10,000 = ₹3 < ₹20 cap
    assert order_brokerage(10_000) == 3.0
    # Big order: 0.03% of 1,00,00,000 = ₹3,000 → capped at ₹20
    assert order_brokerage(10_000_000) == 20.0


def test_round_trip_hand_computed():
    # Buy 100 @ 500, sell 100 @ 505: buy T/O 50,000; sell T/O 50,500
    c = round_trip_costs(buy_price=500, sell_price=505, qty=100)
    assert c.brokerage == 30.15            # 15.00 + 15.15 (0.03% each side, under cap)
    assert abs(c.stt - 12.625) < 0.011     # 0.025% × 50,500 (banker's rounding)
    assert abs(c.exchange_fee - 2.9849) < 0.001
    assert abs(c.stamp_duty - 1.5) < 0.001
    # GST = 18% × (30.15 + 2.98485 + 0.1005) ≈ 5.98
    assert abs(c.gst - 5.98) < 0.02
    assert 52 < c.total < 54


def test_costs_scale_with_size():
    small = round_trip_costs(100, 101, 10).total
    big = round_trip_costs(100, 101, 1000).total
    assert big > small * 10   # statutory % charges dominate at size
