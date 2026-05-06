"""
Options Scalper Strategy
========================
Scalps NIFTY options by:
  1. Computing RSI on the underlying index price
  2. Auto-discovering ATM call/put security IDs from the Dhan scrip master
  3. Buying ATM call (RSI oversold) or put (RSI overbought) at market
  4. Immediately placing a Forever OCO order with:
       target  = breakeven_premium + target_buffer
       stop    = entry_premium    - stop_buffer
  5. Exiting flat at squareoff_time regardless

Config:
    Only EXPIRY_DATE needs to be set. Security IDs are auto-discovered
    from the Dhan instrument master at entry time.
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Optional, Dict
from zoneinfo import ZoneInfo

from strategies.strategy_base import BaseStrategy, StrategyConfig, Signal
from core.charges import BreakevenCalculator
from core.instruments import InstrumentMaster, OptionContract

logger = logging.getLogger("dhan.scalper")
IST = ZoneInfo("Asia/Kolkata")


# ── RSI (Wilder's smoothed) ───────────────────────────────────────────────────

def _compute_rsi(prices: deque, period: int) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    pts = list(prices)[-(period + 1):]
    gains  = [max(pts[i] - pts[i-1], 0) for i in range(1, len(pts))]
    losses = [abs(min(pts[i] - pts[i-1], 0)) for i in range(1, len(pts))]
    avg_gain = sum(gains)  / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class OptionsScalperConfig(StrategyConfig):
    name: str = "Options_Scalper"

    # Underlying index (NIFTY = securityId 13 on IDX_I)
    underlying_security_id: str = "13"
    underlying_exchange:    str = "IDX_I"

    # Option details — security IDs are auto-discovered from instrument master
    option_exchange:  str = "NSE_FNO"
    num_lots:         int = 1
    expiry_date:      str = ""       # "YYYY-MM-DD" — leave empty to auto-select nearest expiry
    strike_step:      int = 50       # NIFTY strikes in steps of 50

    # RSI parameters on the underlying
    rsi_period:      int   = 14
    rsi_oversold:    float = 30.0   # BUY call when RSI crosses below this
    rsi_overbought:  float = 70.0   # BUY put  when RSI crosses above this

    # OCO buffer (rupees of premium beyond breakeven)
    target_buffer:   float = 5.0
    stop_buffer:     float = 5.0

    # Premium sanity filter
    min_premium:     float = 10.0
    max_premium:     float = 200.0

    # Trading time window (IST, 24h)
    trade_start:     str = "09:20"
    trade_end:       str = "15:00"
    squareoff_time:  str = "15:15"

    # Override StrategyConfig defaults
    poll_interval:   float = 10.0
    paper_trading:   bool  = True
    max_orders:      int   = 10


# ── State machine states ──────────────────────────────────────────────────────
FLAT        = "FLAT"
ENTERING    = "ENTERING"
IN_POSITION = "IN_POSITION"


# ── Strategy ──────────────────────────────────────────────────────────────────

class OptionsScalperStrategy(BaseStrategy):
    """
    Options scalper with breakeven-aware OCO exits.
    Overrides BaseStrategy.run() to handle two-security polling.
    """

    def __init__(self, client, risk_manager, config: OptionsScalperConfig):
        super().__init__(client, risk_manager, config)
        self.scalper_cfg: OptionsScalperConfig = config

        # RSI state (on underlying)
        self._prices: deque = deque(maxlen=config.rsi_period + 2)
        self._prev_rsi: Optional[float] = None

        # OCO position state
        self._state:         str            = FLAT
        self._oco_order_id:  Optional[str]  = None
        self._entry_premium: float          = 0.0
        self._option_sid:    Optional[str]  = None
        self._charges        = BreakevenCalculator()
        self._breakeven:     float          = 0.0

        # Instrument master (loaded at first tick)
        self._master: Optional[InstrumentMaster] = None
        self._master_loading:  bool = False
        self._active_expiry:   str  = config.expiry_date

        # Dashboard-visible state
        self.oco_state:         str   = FLAT
        self.breakeven_premium: float = 0.0
        self.last_rsi:          float = 0.0
        self.current_atm:       Optional[Dict] = None   # {"strike", "CE_sid", "PE_sid"}

    # ── Time window helpers ───────────────────────────────────────────────────

    def _ist_time(self) -> dtime:
        return datetime.now(IST).time()

    def _parse_time(self, t: str) -> dtime:
        h, m = map(int, t.split(":"))
        return dtime(h, m)

    def _in_window(self) -> bool:
        now   = self._ist_time()
        start = self._parse_time(self.scalper_cfg.trade_start)
        end   = self._parse_time(self.scalper_cfg.trade_end)
        return start <= now <= end

    def _past_squareoff(self) -> bool:
        return self._ist_time() >= self._parse_time(self.scalper_cfg.squareoff_time)

    # ── on_tick: RSI signal on underlying ────────────────────────────────────

    async def on_tick(self, tick: Dict):
        """Not used — run() is overridden. Kept to satisfy abstract requirement."""
        pass

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        logger.info(f"▶ Options Scalper started (paper={self.scalper_cfg.paper_trading})")

        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Scalper tick error: {e}")
            await asyncio.sleep(self.scalper_cfg.poll_interval)

    async def _tick(self):
        if self._past_squareoff():
            if self._state == IN_POSITION:
                await self._squareoff()
            return

        # ── 0. Load instrument master on first tick ───────────────────────────
        if self._master is None and not self._master_loading:
            self._master_loading = True
            try:
                self._master = await InstrumentMaster.load()
                if not self._active_expiry:
                    self._active_expiry = self._master.nearest_expiry() or ""
                    logger.info(f"Auto-selected expiry: {self._active_expiry}")
                avail = self._master.nifty_expiries()
                logger.info(f"Available NIFTY expiries: {avail[:8]}")
            except Exception as e:
                logger.error(f"Instrument master load failed: {e}")
                self._master_loading = False
                return

        if self._master is None:
            return

        # ── 1. Fetch underlying price for RSI ────────────────────────────────
        try:
            data = await self.client.get_ohlc(
                {self.scalper_cfg.underlying_exchange: [int(self.scalper_cfg.underlying_security_id)]}
            )
            seg  = data.get("data", {}).get(self.scalper_cfg.underlying_exchange, {})
            tick = seg.get(self.scalper_cfg.underlying_security_id, {})
            price = tick.get("last_price", 0.0)
        except Exception as e:
            logger.warning(f"Underlying fetch error: {e}")
            return

        if not price:
            return

        self._prices.append(price)
        rsi = _compute_rsi(self._prices, self.scalper_cfg.rsi_period)
        self.last_rsi = rsi or 0.0

        logger.debug(f"Underlying: ₹{price:.0f} | RSI: {rsi} | State: {self._state}")

        # ── 2. Manage existing position ───────────────────────────────────────
        if self._state == IN_POSITION:
            await self._check_oco_status()
            self._prev_rsi = rsi
            return

        # ── 3. Check entry conditions ─────────────────────────────────────────
        if not self._in_window() or rsi is None or self._prev_rsi is None:
            self._prev_rsi = rsi
            return

        if self.orders_placed >= self.scalper_cfg.max_orders:
            self._prev_rsi = rsi
            return

        ok, reason = self.risk.check_order(self.scalper_cfg.quantity, price, "BUY")
        if not ok:
            logger.warning(f"Risk block: {reason}")
            self._prev_rsi = rsi
            return

        # RSI crossover signals
        if self._prev_rsi > self.scalper_cfg.rsi_oversold and rsi <= self.scalper_cfg.rsi_oversold:
            await self._enter("CALL", price, rsi)

        elif self._prev_rsi < self.scalper_cfg.rsi_overbought and rsi >= self.scalper_cfg.rsi_overbought:
            await self._enter("PUT", price, rsi)

        self._prev_rsi = rsi

    # ── Entry ─────────────────────────────────────────────────────────────────

    async def _enter(self, direction: str, underlying_price: float, rsi: float):
        # Auto-discover ATM security IDs from instrument master
        if not self._master or not self._active_expiry:
            logger.warning("Instrument master not ready — skipping entry")
            return

        atm = self._master.find_atm(
            underlying_price=underlying_price,
            expiry=self._active_expiry,
            strike_step=self.scalper_cfg.strike_step,
        )
        if not atm:
            logger.warning(f"No ATM contracts found for NIFTY @ {underlying_price:.0f} on {self._active_expiry}")
            return

        contract: OptionContract = atm["CE"] if direction == "CALL" else atm["PE"]
        sid      = contract.security_id
        lot_size = contract.lot_size

        # Cache for dashboard
        self.current_atm = {
            "strike":  contract.strike,
            "expiry":  contract.expiry,
            "CE_sid":  atm["CE"].security_id,
            "PE_sid":  atm["PE"].security_id,
        }

        # Fetch option premium
        try:
            opt_data = await self.client.get_ohlc(
                {self.scalper_cfg.option_exchange: [int(sid)]}
            )
            opt_seg   = opt_data.get("data", {}).get(self.scalper_cfg.option_exchange, {})
            opt_tick  = opt_seg.get(sid, {})
            premium   = opt_tick.get("last_price", 0.0)
        except Exception as e:
            logger.warning(f"Option quote error: {e}")
            return

        if not (self.scalper_cfg.min_premium <= premium <= self.scalper_cfg.max_premium):
            logger.info(f"Premium ₹{premium} outside filter [{self.scalper_cfg.min_premium}, {self.scalper_cfg.max_premium}] — skip")
            return

        logger.info(f"📡 RSI {rsi:.1f} → {direction} entry | Underlying ₹{underlying_price:.0f} | Premium ₹{premium:.2f}")

        if self.scalper_cfg.paper_trading:
            charges    = self._charges.calculate(premium, lot_size, self.scalper_cfg.num_lots)
            target_p   = round(charges.breakeven_premium + self.scalper_cfg.target_buffer, 2)
            stop_p     = round(premium - self.scalper_cfg.stop_buffer, 2)

            self._entry_premium       = premium
            self._option_sid          = sid
            self._state               = IN_POSITION
            self._breakeven           = charges.breakeven_premium
            self.breakeven_premium    = charges.breakeven_premium
            self.oco_state            = IN_POSITION
            self.position             = lot_size * self.scalper_cfg.num_lots
            self.entry_price          = premium
            self.orders_placed       += 1

            sig = Signal(action="BUY", price=premium,
                         reason=f"{direction} | RSI {rsi:.1f} | BEP ₹{charges.breakeven_premium:.2f} | T ₹{target_p} S ₹{stop_p}")
            self.signals.append(sig)

            logger.info(
                f"📝 [PAPER] {direction} | Entry ₹{premium:.2f} | "
                f"Charges ₹{charges.total:.2f} | BEP ₹{charges.breakeven_premium:.2f} | "
                f"Target ₹{target_p} | Stop ₹{stop_p}"
            )
            return

        # Live: place market buy
        self._state = ENTERING
        try:
            result = await self.client.place_order(
                transaction_type="BUY",
                exchange_segment=self.scalper_cfg.option_exchange,
                product_type=self.scalper_cfg.product_type,
                order_type="MARKET",
                security_id=sid,
                quantity=lot_size * self.scalper_cfg.num_lots,
            )
            entry_order_id = result.get("orderId")
            logger.info(f"🟢 Entry order placed: {entry_order_id}")
        except Exception as e:
            logger.error(f"Entry order failed: {e}")
            self._state = FLAT
            return

        # Wait for fill
        fill_price = await self._wait_for_fill(entry_order_id, premium)
        if fill_price is None:
            logger.error("Entry order did not fill in time — aborting")
            self._state = FLAT
            return

        charges  = self._charges.calculate(fill_price, lot_size, self.scalper_cfg.num_lots)
        target_p = round(charges.breakeven_premium + self.scalper_cfg.target_buffer, 2)
        stop_p   = round(fill_price - self.scalper_cfg.stop_buffer, 2)

        # Place Forever OCO
        try:
            oco = await self.client.create_forever_order(
                order_flag="OCO",
                transaction_type="SELL",
                exchange_segment=self.scalper_cfg.option_exchange,
                product_type=self.scalper_cfg.product_type,
                order_type="LIMIT",
                security_id=sid,
                quantity=lot_size * self.scalper_cfg.num_lots,
                price=target_p,
                trigger_price=target_p,
                price1=stop_p,
                trigger_price1=stop_p,
                quantity1=lot_size * self.scalper_cfg.num_lots,
            )
            self._oco_order_id = oco.get("orderId")
            logger.info(f"🔗 OCO placed: target ₹{target_p} | stop ₹{stop_p} | id {self._oco_order_id}")
        except Exception as e:
            logger.error(f"OCO order failed: {e}")

        self._entry_premium    = fill_price
        self._option_sid       = sid
        self._state            = IN_POSITION
        self._breakeven        = charges.breakeven_premium
        self.breakeven_premium = charges.breakeven_premium
        self.oco_state         = IN_POSITION
        self.position          = lot_size * self.scalper_cfg.num_lots
        self.entry_price       = fill_price
        self.orders_placed    += 1

        sig = Signal(action="BUY", price=fill_price,
                     reason=f"{direction} | RSI {rsi:.1f} | BEP ₹{charges.breakeven_premium:.2f} | T ₹{target_p} S ₹{stop_p}")
        self.signals.append(sig)

    async def _wait_for_fill(self, order_id: str, fallback: float, max_attempts: int = 10) -> Optional[float]:
        for _ in range(max_attempts):
            await asyncio.sleep(1)
            try:
                order = await self.client.get_order_by_id(order_id)
                status = order.get("orderStatus", "")
                if status == "TRADED":
                    return order.get("price", fallback)
                if status in ("CANCELLED", "REJECTED"):
                    return None
            except Exception as e:
                logger.warning(f"Fill poll error: {e}")
        return None

    # ── OCO status polling ────────────────────────────────────────────────────

    async def _check_oco_status(self):
        if self.scalper_cfg.paper_trading or not self._oco_order_id:
            return
        try:
            orders = await self.client.get_forever_orders()
            for o in orders:
                if str(o.get("orderId")) == str(self._oco_order_id):
                    status = o.get("orderStatus", "")
                    if status in ("TRADED", "CANCELLED", "EXPIRED"):
                        logger.info(f"OCO {self._oco_order_id} → {status}, going FLAT")
                        sig = Signal(action="EXIT", price=self._entry_premium, reason=f"OCO {status}")
                        self.signals.append(sig)
                        self._go_flat()
                    return
        except Exception as e:
            logger.warning(f"OCO status check error: {e}")

    # ── Square-off at end of day ──────────────────────────────────────────────

    async def _squareoff(self):
        logger.info("⏰ Squareoff time — closing all positions")
        if self._oco_order_id and not self.scalper_cfg.paper_trading:
            try:
                await self.client.cancel_forever_order(self._oco_order_id)
            except Exception as e:
                logger.warning(f"OCO cancel error: {e}")

        if self._option_sid and not self.scalper_cfg.paper_trading:
            try:
                await self.client.place_order(
                    transaction_type="SELL",
                    exchange_segment=self.scalper_cfg.option_exchange,
                    product_type=self.scalper_cfg.product_type,
                    order_type="MARKET",
                    security_id=self._option_sid,
                    quantity=self.scalper_cfg.lot_size * self.scalper_cfg.num_lots,
                )
            except Exception as e:
                logger.error(f"Squareoff sell error: {e}")

        sig = Signal(action="EXIT", price=0, reason="End-of-day squareoff")
        self.signals.append(sig)
        self._go_flat()

    def _go_flat(self):
        self._state         = FLAT
        self._oco_order_id  = None
        self._entry_premium = 0.0
        self._option_sid    = None
        self._breakeven     = 0.0
        self.breakeven_premium = 0.0
        self.oco_state      = FLAT
        self.position       = 0
        self.entry_price    = 0.0

    def stop(self):
        self._running = False
        logger.info("⏹ Options Scalper stopped")

    def get_scalper_summary(self) -> Dict:
        return {
            "state":             self._state,
            "oco_state":         self.oco_state,
            "entry_premium":     self._entry_premium,
            "breakeven_premium": self.breakeven_premium,
            "last_rsi":          self.last_rsi,
            "oco_order_id":      self._oco_order_id,
            "option_sid":        self._option_sid,
            "active_expiry":     self._active_expiry,
            "current_atm":       self.current_atm,
            "orders_placed":     self.orders_placed,
            "master_loaded":     self._master is not None,
        }

    def get_expiries(self) -> Dict:
        if not self._master:
            return {"loaded": False, "expiries": [], "nearest": None}
        return {
            "loaded":   True,
            "expiries": self._master.nifty_expiries(),
            "weekly":   self._master.weekly_expiries(),
            "monthly":  self._master.monthly_expiries(),
            "nearest":  self._master.nearest_expiry(),
            "active":   self._active_expiry,
        }
