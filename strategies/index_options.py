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
from core.trade_log import get_trade_logger

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
    last_price:      float = 0.0   # live underlying price
    price_change:    float = 0.0   # vs first observed price
    current_premium: float = 0.0   # live option premium (for unrealized P&L)
    unrealized_pnl:  float = 0.0   # current_premium vs entry_premium × lot_size
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
    consec_ob:     int   = 0   # consecutive overbought ticks (confirmation)
    consec_os:     int   = 0   # consecutive oversold ticks (confirmation)

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
    RSI_OVERSOLD = 25.0   # tightened from 30 — fewer but higher-conviction entries
    RSI_OB       = 75.0   # tightened from 70
    CONFIRM_TICKS = 2     # RSI must stay extreme for N consecutive ticks

    def __init__(
        self,
        client,
        risk_manager,
        live_feed=None,          # core.live_feed.LiveFeed — WebSocket data source
        paper_trading:  bool  = True,
        capital_pct:    float = 0.70,
        target_buffer:  float = 5.0,   # kept for legacy; target_pct takes priority
        stop_buffer:    float = 5.0,   # kept for legacy; stop_pct takes priority
        stop_pct:       float = 0.10,  # stop = entry × (1 - 10%)   e.g. ₹188 → stop ₹169
        target_pct:     float = 0.15,  # target = BEP  × (1 + 15%)  e.g. BEP ₹190 → ₹218
        poll_interval:  float = 10.0,
        min_premium:    float = 5.0,
        max_premium:    float = 10_000.0,  # no effective cap — all index options qualify
        rsi_period:     int   = 14,
        paper_balance:  float = 500_000.0,  # simulated capital for paper mode
    ):
        self.client        = client
        self.risk          = risk_manager
        self.paper_trading = paper_trading
        self.capital_pct   = capital_pct
        self.target_buffer = target_buffer
        self.stop_buffer   = stop_buffer
        self.stop_pct      = stop_pct
        self.target_pct    = target_pct
        self.poll_interval = poll_interval
        self.min_premium   = min_premium
        self.max_premium   = max_premium
        self.rsi_period    = rsi_period

        self._live_feed    = live_feed
        self._running      = False
        self._master: Optional[InstrumentMaster] = None
        self._charges      = BreakevenCalculator()
        self._balance      = 0.0          # live account balance (from API)
        self._paper_balance = paper_balance  # simulated paper capital

        # active_segments controls which indices are scanned
        # NSE_FNO → NIFTY, BANKNIFTY, FINNIFTY, NIFTYNXT50, MIDCPNIFTY
        # BSE_FNO → SENSEX
        self.active_segments: List[str] = ["NSE_FNO", "BSE_FNO"]  # default: all
        self.current_step: int = 1  # 1=fetch 2=signal 3=atm 4=premium 5=risk 6=fill 7=oco

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

        # Balance: paper mode uses simulated capital minus deployed; live uses real account
        if self.paper_trading:
            deployed = sum(
                s.entry_premium * s.lot_size
                for s in self._indices.values() if s.in_position
            )
            self._balance = max(0.0, self._paper_balance - deployed)
        else:
            try:
                funds = await self.client.get_funds()
                self._balance = funds.get("availabelBalance", 0.0)
            except Exception:
                pass

        # Fetch underlying prices — WebSocket if connected, REST as fallback
        self.current_step = 1  # FETCH
        use_ws = self._live_feed and self._live_feed.is_connected()

        if use_ws:
            # Build seg dict from live feed (no API call needed)
            seg = {
                state.underlying_id: {"last_price": self._live_feed.get_ltp(state.underlying_id)}
                for state in self._indices.values()
                if self._live_feed.get_ltp(state.underlying_id) > 0
            }
            if not seg:
                use_ws = False  # feed not populated yet, fall back

        if not use_ws:
            all_ids = [int(s.underlying_id) for s in self._indices.values()]
            try:
                data = await self.client.get_ohlc({"IDX_I": all_ids})
                seg  = data.get("data", {}).get("IDX_I", {})
            except Exception as e:
                logger.warning(f"IDX_I fetch error: {e}")
                return

        self.current_step = 2  # SIGNAL

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
                # Fetch current option premium
                if state.option_sid:
                    try:
                        opt = await self.client.get_ohlc({state.option_segment: [int(state.option_sid)]})
                        cur = opt.get("data",{}).get(state.option_segment,{}).get(state.option_sid,{}).get("last_price", 0.0)
                        if cur:
                            state.current_premium = cur
                            state.unrealized_pnl  = round((cur - state.entry_premium) * state.lot_size, 2)
                    except Exception:
                        pass

                if self.paper_trading:
                    # Simulate OCO exit in paper mode (% of premium)
                    cur    = state.current_premium
                    target = state.breakeven * (1 + self.target_pct)
                    stop   = state.entry_premium * (1 - self.stop_pct)
                    if cur >= target:
                        await self._paper_exit(state, cur, f"TARGET HIT ₹{target:.2f} (current ₹{cur:.2f})")
                    elif cur > 0 and cur <= stop:
                        await self._paper_exit(state, cur, f"STOP HIT ₹{stop:.2f} (current ₹{cur:.2f})")
                else:
                    await self._check_oco(state)
                state.prev_rsi = rsi
                continue

            if not self._in_window():
                state.prev_rsi = rsi
                continue

            if rsi is None:
                continue

            if state.prev_rsi is None:
                state.prev_rsi = rsi

            state.prev_rsi = rsi

            # Track consecutive extreme ticks for confirmation filter
            if rsi >= self.RSI_OB:
                state.consec_ob += 1
                state.consec_os  = 0
            elif rsi <= self.RSI_OVERSOLD:
                state.consec_os += 1
                state.consec_ob  = 0
            else:
                state.consec_ob  = 0
                state.consec_os  = 0

            # Entry: RSI must be extreme for CONFIRM_TICKS consecutive readings
            # This filters out single-tick spikes that reverse immediately
            if state.consec_ob >= self.CONFIRM_TICKS and not state.in_position:
                logger.info(f"{state.name}: RSI {rsi:.1f} overbought ×{state.consec_ob} ticks → enter PUT")
                await self._enter(state, "PE", price, rsi)
            elif state.consec_os >= self.CONFIRM_TICKS and not state.in_position:
                logger.info(f"{state.name}: RSI {rsi:.1f} oversold ×{state.consec_os} ticks → enter CALL")
                await self._enter(state, "CE", price, rsi)

    async def _enter(self, state: IndexState, opt_type: str, underlying: float, rsi: float):
        if not self._master or not state.active_expiry:
            return

        # Step 3: Find ATM contract
        self.current_step = 3
        atm = self._master.find_atm_for_index(state.name, underlying, state.active_expiry)
        contract: Optional[OptionContract] = atm.get(opt_type)
        if not contract:
            logger.warning(f"{state.name}: no ATM {opt_type} found @ {underlying:.0f}")
            self.current_step = 1; return

        # Step 4: Fetch option premium
        self.current_step = 4
        try:
            opt_data = await self.client.get_ohlc({state.option_segment: [int(contract.security_id)]})
            premium  = opt_data.get("data", {}).get(state.option_segment, {}).get(
                contract.security_id, {}).get("last_price", 0.0)
        except Exception as e:
            logger.warning(f"{state.name} premium fetch error: {e}")
            self.current_step = 1; return

        # Sanity check: option premium > 20% of underlying = wrong contract
        pct_of_underlying = (premium / underlying) * 100 if underlying > 0 else 0
        logger.info(f"{state.name}: ATM {opt_type} {contract.strike} | premium ₹{premium:.2f} ({pct_of_underlying:.1f}% of underlying ₹{underlying:,.0f})")
        if premium > underlying * 0.20:
            logger.warning(f"{state.name}: premium ₹{premium:.2f} = {pct_of_underlying:.1f}% of underlying — likely wrong contract, skipping")
            self.current_step = 1; return
        if not (self.min_premium <= premium):
            logger.warning(f"{state.name}: premium ₹{premium:.2f} below min ₹{self.min_premium}")
            self.current_step = 1; return

        # Step 5: Risk gate + capital sizing
        # Options BUYER pays full premium upfront (no margin like futures)
        # cost_per_lot = premium × lot_size  (e.g. ₹188 × 65 = ₹12,220)
        self.current_step = 5
        cost_per_lot = premium * contract.lot_size
        if cost_per_lot <= 0:
            self.current_step = 1; return

        risk_limit = self.risk.config.max_loss_per_trade
        budget     = self._balance * self.capital_pct

        # Skip silently if even 1 lot exceeds the risk limit — avoids
        # NIFTYNXT50/FINNIFTY firing the risk-block warning every 10s
        if cost_per_lot > risk_limit:
            logger.debug(
                f"{state.name}: 1 lot = ₹{cost_per_lot:,.0f} > risk limit ₹{risk_limit:,.0f} — "
                f"skipping (increase PAPER_BALANCE or risk limit to trade this index)"
            )
            self.current_step = 1; return

        num_lots_budget = max(1, int(budget / cost_per_lot))
        num_lots_risk   = max(1, int(risk_limit / cost_per_lot))
        num_lots        = min(num_lots_budget, num_lots_risk)
        qty             = num_lots * contract.lot_size
        total_cost      = num_lots * cost_per_lot

        logger.info(f"{state.name}: {num_lots} lot(s) × ₹{cost_per_lot:.0f}/lot = ₹{total_cost:.0f} | budget ₹{budget:.0f}")

        ok, msg = self.risk.check_order(qty, premium, "BUY")
        if not ok:
            logger.warning(f"{state.name} risk block: {msg}")
            self.current_step = 1; return

        charges  = self._charges.calculate(premium, contract.lot_size, num_lots)
        # % of premium: stop 10% below entry, target 15% above BEP
        target_p = round(charges.breakeven_premium * (1 + self.target_pct), 2)
        stop_p   = round(premium * (1 - self.stop_pct), 2)

        logger.info(
            f"📡 {state.name} {opt_type} | RSI {rsi:.1f} | "
            f"Underlying ₹{underlying:,.0f} | Strike {contract.strike} | "
            f"Premium ₹{premium:.2f} | BEP ₹{charges.breakeven_premium:.2f} | "
            f"Target ₹{target_p} | Stop ₹{stop_p}"
        )

        if self.paper_trading:
            self.current_step = 6  # FILL (simulated)
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
            get_trade_logger().log_entry(
                engine="F&O", symbol=f"{state.name} {opt_type} {int(contract.strike)}",
                action="BUY", price=premium, qty=qty, mode="PAPER",
                lot_size=contract.lot_size, num_lots=num_lots,
                bep=charges.breakeven_premium, target=target_p, stop=stop_p,
                rsi=rsi, segment=state.option_segment, strategy="RSI_14",
            )
            self.current_step = 7
            # Reset to scanning after brief pause so pipeline doesn't lock on 07
            await asyncio.sleep(0.5)
            self.current_step = 1
            return

        # Live: place market order (step 6)
        self.current_step = 6
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
        target_p = round(charges.breakeven_premium * (1 + self.target_pct), 2)
        stop_p   = round(fill_price * (1 - self.stop_pct), 2)

        # Step 7: Place Forever OCO
        self.current_step = 7
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
        self.current_step = 1  # Reset — scanner continues on next tick

    async def _paper_exit(self, state: IndexState, exit_premium: float, reason: str):
        """Simulate OCO exit in paper mode."""
        pnl = round((exit_premium - state.entry_premium) * state.lot_size, 2)
        logger.info(
            f"📝 [PAPER EXIT] {state.name} {state.option_type} | {reason} | "
            f"Entry ₹{state.entry_premium} → Exit ₹{exit_premium:.2f} | PnL ₹{pnl:+.2f}"
        )
        sig = Signal("EXIT", exit_premium, f"[PAPER] {state.name} {reason} | PnL ₹{pnl:+.2f}")
        self.signals.append(sig)
        self.orders_placed += 1
        get_trade_logger().log_exit(
            engine="F&O",
            symbol=f"{state.name} {state.option_type} {int(state.strike)}",
            action="EXIT", price=exit_premium,
            qty=state.lot_size, mode="PAPER",
            entry_price=state.entry_premium, pnl=pnl,
            reason=reason, segment=state.option_segment, strategy="RSI_14",
        )
        self._go_flat(state)

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
            "current_step":    self.current_step,
            "active_indices":  [n for n, s in self._indices.items() if s.option_segment in self.active_segments],
            "indices": {
                name: {
                    "rsi":             state.last_rsi,
                    "price":           state.last_price,
                    "change_pct":      state.price_change,
                    "in_position":     state.in_position,
                    "option_type":     state.option_type,
                    "strike":          state.strike,
                    "entry":           state.entry_premium,
                    "current_premium": state.current_premium,
                    "unrealized_pnl":  state.unrealized_pnl,
                    "breakeven":       state.breakeven,
                    "target":          round(state.breakeven * (1 + self.target_pct), 2) if state.in_position else 0,
                    "stop":            round(state.entry_premium * (1 - self.stop_pct), 2) if state.in_position else 0,
                    "lot_size":        state.lot_size,
                    "expiry":          state.active_expiry,
                    "option_sid":      state.option_sid,
                }
                for name, state in self._indices.items()
            },
        }
