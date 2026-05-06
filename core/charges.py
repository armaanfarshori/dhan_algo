"""
Dhan brokerage and statutory charge calculator for options trades.
All rates as of 2025 for NSE F&O options.
"""

from dataclasses import dataclass


@dataclass
class ChargeBreakdown:
    brokerage:         float
    stt:               float
    exchange_fee:      float
    sebi_fee:          float
    gst:               float
    stamp_duty:        float
    total:             float
    breakeven_premium: float  # minimum exit premium to cover all charges


class BreakevenCalculator:
    BROKERAGE_PER_LEG = 20.0      # ₹20 flat per executed order
    STT_SELL_PCT      = 0.001     # 0.1% on sell-side turnover (options)
    EXCHANGE_FEE_PCT  = 0.00053   # 0.053% of total turnover (NSE F&O)
    SEBI_FEE_PER_CR   = 10.0     # ₹10 per crore of turnover
    GST_PCT           = 0.18      # 18% on brokerage + exchange fee + SEBI fee
    STAMP_DUTY_PCT    = 0.00003   # 0.003% on buy-side turnover

    def calculate(self, entry_premium: float, lot_size: int, num_lots: int = 1) -> ChargeBreakdown:
        qty          = lot_size * num_lots
        buy_turnover = entry_premium * qty
        sell_turnover = entry_premium * qty  # approximate; actual exit price unknown at entry

        brokerage    = self.BROKERAGE_PER_LEG * 2
        stt          = sell_turnover * self.STT_SELL_PCT
        exchange_fee = (buy_turnover + sell_turnover) * self.EXCHANGE_FEE_PCT
        sebi_fee     = ((buy_turnover + sell_turnover) / 1e7) * self.SEBI_FEE_PER_CR
        gst          = (brokerage + exchange_fee + sebi_fee) * self.GST_PCT
        stamp_duty   = buy_turnover * self.STAMP_DUTY_PCT

        total = brokerage + stt + exchange_fee + sebi_fee + gst + stamp_duty
        breakeven_premium = entry_premium + (total / qty)

        return ChargeBreakdown(
            brokerage=round(brokerage, 2),
            stt=round(stt, 2),
            exchange_fee=round(exchange_fee, 2),
            sebi_fee=round(sebi_fee, 4),
            gst=round(gst, 2),
            stamp_duty=round(stamp_duty, 4),
            total=round(total, 2),
            breakeven_premium=round(breakeven_premium, 2),
        )
