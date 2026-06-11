"""
Indian equity INTRADAY cost stack (NSE, Dhan rates, 2025-26).

Every backtested round trip pays the full statutory + brokerage stack —
a backtest without these numbers systematically overstates edge, and ORB
on volatile small/midcaps does many small round trips where costs dominate.

Rates:
    Brokerage     ₹20 or 0.03% of turnover per executed order, whichever is LOWER
    STT           0.025% on the SELL side (intraday equity)
    NSE txn fee   0.00297% on turnover, both sides
    SEBI fee      ₹10 per crore of turnover (0.0001%)
    Stamp duty    0.003% on the BUY side
    GST           18% on (brokerage + exchange fee + SEBI fee)

core/charges.py keeps the F&O options calculator; this module is equities.
"""
from dataclasses import dataclass

BROKERAGE_CAP = 20.0
BROKERAGE_PCT = 0.0003       # 0.03%
STT_SELL_PCT = 0.00025       # 0.025% sell side
EXCHANGE_PCT = 0.0000297     # 0.00297% both sides
SEBI_PCT = 0.000001          # ₹10 / crore
STAMP_BUY_PCT = 0.00003      # 0.003% buy side
GST_PCT = 0.18


@dataclass(frozen=True)
class RoundTripCosts:
    brokerage: float
    stt: float
    exchange_fee: float
    sebi_fee: float
    stamp_duty: float
    gst: float
    total: float


def order_brokerage(turnover: float) -> float:
    """₹20 or 0.03%, whichever is lower, per executed order."""
    return round(min(BROKERAGE_CAP, turnover * BROKERAGE_PCT), 2)


def round_trip_costs(buy_price: float, sell_price: float, qty: int) -> RoundTripCosts:
    """Full cost of one intraday round trip (one buy order + one sell order)."""
    buy_turnover = buy_price * qty
    sell_turnover = sell_price * qty
    total_turnover = buy_turnover + sell_turnover

    brokerage = order_brokerage(buy_turnover) + order_brokerage(sell_turnover)
    stt = sell_turnover * STT_SELL_PCT
    exchange = total_turnover * EXCHANGE_PCT
    sebi = total_turnover * SEBI_PCT
    stamp = buy_turnover * STAMP_BUY_PCT
    gst = (brokerage + exchange + sebi) * GST_PCT

    total = brokerage + stt + exchange + sebi + stamp + gst
    return RoundTripCosts(
        brokerage=round(brokerage, 2), stt=round(stt, 2),
        exchange_fee=round(exchange, 4), sebi_fee=round(sebi, 4),
        stamp_duty=round(stamp, 4), gst=round(gst, 2),
        total=round(total, 2),
    )
