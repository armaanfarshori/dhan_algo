"""
Index Options Scanner
======================
Monitors ALL tradeable index underlyings (NIFTY, BANKNIFTY, SENSEX,
FINNIFTY, NIFTYNXT50, MIDCPNIFTY) simultaneously.

For each index:
  - Fetches underlying price via IDX_I bulk call (one request for all)
  - Runs RSI-14 signal logic on the underlying price
  - On oversold crossover  → BUY ATM Call option
  - On overbought crossover → BUY ATM Put option
  - Immediately places Forever OCO: target = BEP+buffer, stop = entry-buffer
  - Max 1 active position per index (up to 6 concurrent)
  - Hard squareoff at 15:15 IST

No equity F&O, no stock options — index options only.

Capital sizing: 70% of available balance per position, spread across active indices.
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from strategies.strategy_base import Signal
from core.instruments import InstrumentMaster, OptionContract
from core.charges import BreakevenCalculator

logger = logging.getLogger("dhan.index_options")
IST = ZoneInfo("Asia/Kolkata")


# ── RSI ───────────────────────────────────────────────────────────────────────

def _rsi(prices: deque, period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    pts = list(prices)[-(period + 1):]
    gains  = [max(pts[i] - pts[i-1], 0) for i in range(1, len(pts))]
    losses = [abs(min(pts[i] - pts[i-1], 0)) for i in range(1, len(pts))]
    avg_g  = sum(gains)  / period
    avg_l  = sum(losses) / period
    if avg_l == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_g / avg_l)), 2)


# ── Per-index state ───────────────────────────────────────────────────────────

@dataclass
class IndexState:
    name:          str
    underlying_id: str
    option_segment: str
    lot_size:      int
    strike_step:   int
    # RSI
    prices:        deque = None
    prev_rsi:      Optional[float] = None
    last_rsi:      float = 0.0
    last_price:    float = 0.0   # live underlying price
    price_change:  float = 0.0   # vs first observed price
    # Position
    in_position:   bool  = False
    option_sid:    Optional[str]  = None
    option_type:   str   = ""     # CE or PE
    entry_premium: float = 0.0
    breakeven:     float = 0.0
    oco_order_id:  Optional[str]  = None
    active_expiry: str   = ""
    strike:        float = 0.0
    orders_placed: int   = 0

    def __post_init__(self):
        if self.prices is None:
            self.prices = deque(maxlen=16)


class IndexOptionsScanner:
    """
    Monitors all 6 index underlyings and trades their ATM options
    based on RSI-14 crossover signals.
    """

    TRADE_START  = dtime(9, 20)
    TRADE_END    = dtime(15, 0)
    SQUAREOFF    = dtime(15, 15)
    RSI_OVERSOLD = 30.0
    RSI_OB       = 70.0

    def __init__(
        self,
        client,
        risk_manager,
        paper_trading: bool  = True,
        capital_pct:   float = 0.70,
        target_buffer: float = 5.0,
        stop_buffer:   float = 5.0,
        poll_interval: float = 10.0,
        min_premium:   float = 10.0,
        max_premium:   float = 500.0,
        rsi_period:    int   = 14,
    ):
        self.client        = client
        self.risk          = risk_manager
        self.paper_trading = paper_trading
        self.capital_pct   = capital_pct
        self.target_buffer = target_buffer
        self.stop_buffer   = stop_buffer
        self.poll_interval = poll_interval
        self.min_premium   = min_premium
        self.max_premium   = max_premium
        self.rsi_period    = rsi_period

        self._running      = False
        self._master: Optional[InstrumentMaster] = None
        self._charges      = BreakevenCalculator()
        self._balance      = 0.0

        # active_segments controls which indices are scanned
        # NSE_FNO → NIFTY, BANKNIFTY, FINNIFTY, NIFTYNXT50, MIDCPNIFTY
        # BSE_FNO → SENSEX
        self.active_segments: List[str] = ["NSE_FNO", "BSE_FNO"]  # default: all

        # Build index states from InstrumentMaster.INDEX_CONFIGS
        self._indices: Dict[str, IndexState] = {
            name: IndexState(
                name=name,
                underlying_id=cfg["underlying_id"],
                option_segment=cfg["option_segment"],
                lot_size=cfg["lot_size"],
                strike_step=cfg["strike_step"],
            )
            for name, cfg in InstrumentMaster.INDEX_CONFIGS.items()
        }

        # Dashboard-visible state
        self.signals: List[Signal] = []
        self.orders_placed = 0

    # ── Duck-typing for dashboard handlers ────────────────────────────────────
    @property
    def config(self):
        class _C:
            name = "Index_Options_Scanner"
        return _C()

    @property
    def position(self) -> int:
        return sum(1 for s in self._indices.values() if s.in_position)

    @property
    def entry_price(self) -> float:
        for s in self._indices.values():
            if s.in_position:
                return s.entry_premium
        return 0.0

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        logger.info(f"▶ Index Options Scanner started | indices: {list(self._indices.keys())}")

        # Load instrument master
        self._master = await InstrumentMaster.load()
        for name, state in self._indices.items():
            state.active_expiry = self._master.nearest_expiry_for_index(name) or ""
            logger.info(f"  {name}: expiry={state.active_expiry}, lot={state.lot_size}, step={state.strike_step}")

        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Index options tick error: {e}")
            await asyncio.sleep(self.poll_interval)

    async def _tick(self):
        if self._past_squareoff():
            await self._squareoff_all()
            return

        # Refresh balance
        try:
            funds = await self.client.get_funds()
            self._balance = funds.get("availabelBalance", 0.0)
        except Exception:
            pass

        # Fetch all underlying prices in ONE call
        all_ids = [int(s.underlying_id) for s in self._indices.values()]
        try:
            data    = await self.client.get_ohlc({"IDX_I": all_ids})
            seg     = data.get("data", {}).get("IDX_I", {})
        except Exception as e:
            logger.warning(f"IDX_I fetch error: {e}")
            return

        # Process only indices whose option_segment is in active_segments
        for name, state in self._indices.items():
            if state.option_segment not in self.active_segments:
                continue
            price = seg.get(state.underlying_id, {}).get("last_price", 0.0)
            if not price:
                continue

            state.prices.append(price)
            rsi = _rsi(state.prices, self.rsi_period)
            state.last_rsi  = rsi or 0.0
            if state.last_price and state.last_price > 0:
                state.price_change = round((price - state.prices[0]) / state.prices[0] * 100, 2)
            state.last_price = price

            if state.in_position:
                await self._check_oco(state)
                state.prev_rsi = rsi
                continue

            if not self._in_window():
                state.prev_rsi = rsi
                continue

            if rsi is None or state.prev_rsi is None:
                state.prev_rsi = rsi
                continue

            prev, state.prev_rsi = state.prev_rsi, rsi

            # RSI oversold → BUY Call
            if prev > self.RSI_OVERSOLD >= rsi:
                await self._enter(state, "CE", price, rsi)
            # RSI overbought → BUY Put
            elif prev < self.RSI_OB <= rsi:
                await self._enter(state, "PE", price, rsi)

    async def _enter(self, state: IndexState, opt_type: str, underlying: float, rsi: float):
        if not self._master or not state.active_expiry:
            return

        # Find ATM contract
        atm = self._master.find_atm_for_index(state.name, underlying, state.active_expiry)
        contract: Optional[OptionContract] = atm.get(opt_type)
        if not contract:
            logger.warning(f"{state.name}: no ATM {opt_type} found @ {underlying:.0f}")
            return

        # Fetch option premium
        try:
            opt_data = await self.client.get_ohlc({state.option_segment: [int(contract.security_id)]})
            premium  = opt_data.get("data", {}).get(state.option_segment, {}).get(
                contract.security_id, {}).get("last_price", 0.0)
        except Exception as e:
            logger.warning(f"{state.name} premium fetch error: {e}")
            return

        if not (self.min_premium <= premium <= self.max_premium):
            logger.info(f"{state.name}: {opt_type} premium ₹{premium} outside filter")
            return

        # Capital sizing: 70% of balance / number of active or potential positions
        active = self.position or 1
        budget = (self._balance * self.capital_pct) / max(active, 1)
        margin_est = premium * contract.lot_size * 0.10   # 10% of notional
        num_lots = max(1, int(budget / margin_est)) if margin_est > 0 else 1
        qty = num_lots * contract.lot_size

        # Risk gate
        ok, msg = self.risk.check_order(qty, premium, "BUY")
        if not ok:
            logger.warning(f"{state.name} risk block: {msg}")
            return

        charges = self._charges.calculate(premium, contract.lot_size, num_lots)
        target_p = round(charges.breakeven_premium + self.target_buffer, 2)
        stop_p   = round(premium - self.stop_buffer, 2)

        logger.info(
            f"📡 {state.name} {opt_type} | RSI {rsi:.1f} | "
            f"Underlying ₹{underlying:,.0f} | Strike {contract.strike} | "
            f"Premium ₹{premium:.2f} | BEP ₹{charges.breakeven_premium:.2f} | "
            f"Target ₹{target_p} | Stop ₹{stop_p}"
        )

        if self.paper_trading:
            state.in_position   = True
            state.option_sid    = contract.security_id
            state.option_type   = opt_type
            state.entry_premium = premium
            state.breakeven     = charges.breakeven_premium
            state.strike        = contract.strike
            state.orders_placed += 1
            self.orders_placed  += 1
            sig = Signal("BUY", premium,
                         f"{state.name} {opt_type} {int(contract.strike)} | RSI {rsi:.1f} | "
                         f"BEP ₹{charges.breakeven_premium:.2f} | T ₹{target_p} S ₹{stop_p}")
            self.signals.append(sig)
            logger.info(f"📝 [PAPER] {state.name} {opt_type} entered @ ₹{premium:.2f}")
            return

        # Live: place market order
        try:
            result = await self.client.place_order(
                transaction_type="BUY",
                exchange_segment=state.option_segment,
                product_type="MARGIN",
                order_type="MARKET",
                security_id=contract.security_id,
                quantity=qty,
            )
            order_id = result.get("orderId")
        except Exception as e:
            logger.error(f"{state.name} entry order failed: {e}")
            return

        # Wait for fill
        fill_price = await self._wait_fill(order_id, premium)
        if not fill_price:
            return

        charges  = self._charges.calculate(fill_price, contract.lot_size, num_lots)
        target_p = round(charges.breakeven_premium + self.target_buffer, 2)
        stop_p   = round(fill_price - self.stop_buffer, 2)

        # Place Forever OCO
        try:
            oco = await self.client.create_forever_order(
                order_flag="OCO", transaction_type="SELL",
                exchange_segment=state.option_segment,
                product_type="MARGIN", order_type="LIMIT",
                security_id=contract.security_id, quantity=qty,
                price=target_p, trigger_price=target_p,
                price1=stop_p,  trigger_price1=stop_p, quantity1=qty,
            )
            state.oco_order_id = oco.get("orderId")
        except Exception as e:
            logger.error(f"{state.name} OCO order failed: {e}")

        state.in_position   = True
        state.option_sid    = contract.security_id
        state.option_type   = opt_type
        state.entry_premium = fill_price
        state.breakeven     = charges.breakeven_premium
        state.strike        = contract.strike
        state.orders_placed += 1
        self.orders_placed  += 1
        sig = Signal("BUY", fill_price,
                     f"{state.name} {opt_type} {int(contract.strike)} | "
                     f"BEP ₹{charges.breakeven_premium:.2f}")
        self.signals.append(sig)

    async def _check_oco(self, state: IndexState):
        if self.paper_trading or not state.oco_order_id:
            return
        try:
            orders = await self.client.get_forever_orders()
            for o in orders:
                if str(o.get("orderId")) == str(state.oco_order_id):
                    if o.get("orderStatus") in ("TRADED", "CANCELLED", "EXPIRED"):
                        logger.info(f"{state.name}: OCO settled → going FLAT")
                        self._go_flat(state)
            return
        except Exception as e:
            logger.warning(f"{state.name} OCO check error: {e}")

    async def _wait_fill(self, order_id: str, fallback: float, max_tries: int = 10):
        for _ in range(max_tries):
            await asyncio.sleep(1)
            try:
                o = await self.client.get_order_by_id(order_id)
                if o.get("orderStatus") == "TRADED":
                    return o.get("price", fallback)
                if o.get("orderStatus") in ("CANCELLED", "REJECTED"):
                    return None
            except Exception:
                pass
        return None

    async def _squareoff_all(self):
        any_open = any(s.in_position for s in self._indices.values())
        if not any_open:
            return
        logger.info("⏰ EOD squareoff — closing all index option positions")
        for state in self._indices.values():
            if state.in_position:
                if state.oco_order_id and not self.paper_trading:
                    try:
                        await self.client.cancel_forever_order(state.oco_order_id)
                    except Exception:
                        pass
                if state.option_sid and not self.paper_trading:
                    try:
                        await self.client.place_order(
                            transaction_type="SELL",
                            exchange_segment=state.option_segment,
                            product_type="MARGIN", order_type="MARKET",
                            security_id=state.option_sid,
                            quantity=state.lot_size,
                        )
                    except Exception as e:
                        logger.error(f"{state.name} squareoff failed: {e}")
                sig = Signal("EXIT", 0.0, f"{state.name} EOD squareoff")
                self.signals.append(sig)
                self._go_flat(state)

    def _go_flat(self, state: IndexState):
        state.in_position   = False
        state.option_sid    = None
        state.option_type   = ""
        state.entry_premium = 0.0
        state.breakeven     = 0.0
        state.oco_order_id  = None
        state.strike        = 0.0

    def stop(self):
        self._running = False
        logger.info("⏹ Index Options Scanner stopped")

    def _in_window(self) -> bool:
        t = datetime.now(IST).time()
        return self.TRADE_START <= t <= self.TRADE_END

    def _past_squareoff(self) -> bool:
        return datetime.now(IST).time() >= self.SQUAREOFF

    def get_summary(self) -> dict:
        return {
            "mode":            "index_options",
            "paper_trading":   self.paper_trading,
            "orders_placed":   self.orders_placed,
            "open_positions":  self.position,
            "balance":         self._balance,
            "active_segments": self.active_segments,
            "active_indices":  [n for n, s in self._indices.items() if s.option_segment in self.active_segments],
            "indices": {
                name: {
                    "rsi":          state.last_rsi,
                    "price":        state.last_price,
                    "change_pct":   state.price_change,
                    "in_position":  state.in_position,
                    "option_type":  state.option_type,
                    "strike":       state.strike,
                    "entry":        state.entry_premium,
                    "breakeven":    state.breakeven,
                    "expiry":       state.active_expiry,
                }
                for name, state in self._indices.items()
            },
        }
