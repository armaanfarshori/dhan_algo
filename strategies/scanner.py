"""
Multi-Stock / Multi-Segment Scanner
=====================================
Automatically scans NSE top movers across NSE_EQ, NSE_FNO and MCX
using a single bulk OHLC call per segment. Position sizing is derived
from 70% of available account balance — no manual lot input.

F&O hedging: when selling options, a far-OTM hedge leg is placed to
reduce SPAN margin requirements (converts naked short → defined-risk spread).

Usage:
    scanner = MultiStockScanner(client, risk, watchlist, strategy_key="sma_crossover")
    asyncio.create_task(scanner.run())
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from strategies.strategy_base import (
    BaseStrategy, Signal,
    SMACrossoverStrategy, SMAConfig,
)
from strategies.backtest_strategies import (
    RSIScalperStrategy, RSIConfig,
    MomentumBreakoutStrategy, MomentumConfig,
    MeanReversionStrategy, MeanReversionConfig,
    BollingerReversionStrategy, BollingerConfig,
    VWAPReversionStrategy, VWAPConfig,
)
from core.watchlist import WatchlistManager, WatchlistStock

logger = logging.getLogger("dhan.scanner")
IST = ZoneInfo("Asia/Kolkata")

STRATEGY_MAP = {
    "sma_crossover":     (SMACrossoverStrategy,      SMAConfig),
    "rsi_scalper":       (RSIScalperStrategy,         RSIConfig),
    "momentum_breakout": (MomentumBreakoutStrategy,   MomentumConfig),
    "mean_reversion":    (MeanReversionStrategy,      MeanReversionConfig),
    "bollinger":         (BollingerReversionStrategy, BollingerConfig),
    "vwap_reversion":    (VWAPReversionStrategy,      VWAPConfig),
}

SEGMENT_PRODUCT = {
    "NSE_EQ":  "INTRADAY",
    "NSE_FNO": "MARGIN",
    "MCX_COMM":"MARGIN",
}


@dataclass
class ScanResult:
    security_id: str
    symbol:      str
    segment:     str
    action:      str
    price:       float
    reason:      str
    qty:         int   = 1
    score:       float = 0.0


class MultiStockScanner:
    """
    Scans top movers across multiple segments every poll_interval seconds.
    Auto-sizes positions from 70% of available account balance.
    """

    TRADE_START = dtime(9, 20)
    TRADE_END   = dtime(15, 0)
    SQUAREOFF   = dtime(15, 15)

    def __init__(
        self,
        client,
        risk_manager,
        watchlist: WatchlistManager,
        strategy_key: str        = "sma_crossover",
        segments: List[str]      = None,   # e.g. ["NSE_EQ", "NSE_FNO"]
        paper_trading: bool      = True,
        poll_interval: float     = 30.0,
        max_positions: int       = 5,
        capital_pct: float       = 0.70,
        hedge_fno: bool          = True,
        hedge_offset: int        = 200,
        paper_balance: float     = 500_000.0,  # simulated capital for paper mode
    ):
        self.client         = client
        self.risk           = risk_manager
        self.watchlist      = watchlist
        self.strategy_key   = strategy_key
        self.segments       = segments or ["NSE_EQ"]
        self.paper_trading  = paper_trading
        self.poll_interval  = poll_interval
        self.max_positions  = max_positions
        self.capital_pct    = capital_pct
        self.hedge_fno      = hedge_fno
        self.hedge_offset   = hedge_offset

        self._running         = False
        self._strategies:     Dict[str, BaseStrategy] = {}
        self._positions:      Dict[str, float]         = {}
        self._current_prices: Dict[str, float]         = {}
        self._paper_balance   = paper_balance
        self._hedge_sids: Dict[str, str]           = {}   # "segment:sid" → hedge_sid
        self.signals:     List[Signal]             = []
        self.orders_placed = 0
        self.scan_results: List[ScanResult] = []
        self._available_balance: float = 0.0

        self._cfg_cls, self._cfg_type = STRATEGY_MAP.get(
            strategy_key, (SMACrossoverStrategy, SMAConfig)
        )

    # ── Duck-typed properties for dashboard handlers ──────────────────────────
    @property
    def config(self):
        class _C:
            name = f"Scanner/{self.strategy_key}"
        return _C()

    @property
    def position(self) -> int:
        return len(self._positions)

    @property
    def entry_price(self) -> float:
        return 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def stop(self):
        self._running = False
        logger.info("⏹ Scanner stopped")

    async def run(self):
        self._running = True
        segs = ", ".join(self.segments)
        logger.info(f"▶ Scanner started | strategy={self.strategy_key} | segments={segs} | "
                    f"capital={int(self.capital_pct*100)}% | hedge={self.hedge_fno}")
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Scanner tick error: {e}")
            await asyncio.sleep(self.poll_interval)

    # ── Core tick ─────────────────────────────────────────────────────────────

    async def _tick(self):
        if self._past_squareoff():
            await self._squareoff_all()
            return

        # Balance: paper uses simulated capital minus deployed; live uses real account
        if self.paper_trading:
            deployed = sum(
                self._current_prices.get(key.split(":")[1], ep) * (
                    self._strategies[key].config.quantity if key in self._strategies else 1
                )
                for key, ep in self._positions.items()
            )
            self._available_balance = max(0.0, self._paper_balance - deployed)
        else:
            try:
                funds = await self.client.get_funds()
                self._available_balance = funds.get("availabelBalance", 0.0)
            except Exception:
                pass

        stocks = self.watchlist.get()
        if not stocks:
            return

        # Fetch quotes for all segments simultaneously
        all_quotes: Dict[str, Dict] = {}   # segment → {sid: tick}
        fetch_tasks = [self._fetch_segment(seg, stocks) for seg in self.segments]
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        for seg, res in zip(self.segments, results):
            if isinstance(res, dict):
                all_quotes[seg] = res

        # Store current prices for open positions (unrealized P&L)
        for seg, seg_quotes in all_quotes.items() if all_quotes else []:
            for sid, tick in seg_quotes.items():
                self._current_prices[sid] = tick.get("last_price", 0.0)

        # Run signal logic
        scan_results: List[ScanResult] = []
        signal_map: dict = {}

        for seg, seg_quotes in all_quotes.items():
            for stock in stocks:
                tick = seg_quotes.get(stock.security_id) or seg_quotes.get(str(stock.security_id))
                if not tick:
                    continue
                price = tick.get("last_price", 0.0)
                if not price:
                    continue

                key      = f"{seg}:{stock.security_id}"
                strategy = self._get_strategy(key, stock, seg)

                try:
                    signal = await strategy.on_tick(tick)
                except Exception as e:
                    logger.debug(f"Signal error {stock.symbol}/{seg}: {e}")
                    continue

                if not signal:
                    continue

                if signal.action in ("BUY", "SELL"):
                    qty   = self._calc_qty(price, seg, stock.lot_size)
                    score = abs(stock.change_pct) + (stock.volume / 1e6)
                    scan_results.append(ScanResult(
                        security_id=stock.security_id, symbol=stock.symbol,
                        segment=seg, action=signal.action,
                        price=price, reason=signal.reason,
                        qty=qty, score=score,
                    ))
                    signal_map[stock.security_id] = {"action": signal.action, "reason": signal.reason}

                elif signal.action == "EXIT" and key in self._positions:
                    await self._exit_pos(key, stock.symbol, price, signal.reason, strategy)
                    signal_map[stock.security_id] = {"action": "EXIT", "reason": signal.reason}

                # Paper mode: check 3% stop-loss on open equity positions
                elif key in self._positions and self.paper_trading:
                    entry = self._positions[key]
                    if entry > 0 and price < entry * 0.97:
                        reason = f"Paper stop-loss hit ({((price-entry)/entry*100):.1f}%)"
                        await self._exit_pos(key, stock.symbol, price, reason, strategy)
                        signal_map[stock.security_id] = {"action": "EXIT", "reason": reason}

        self.watchlist.update_signals(signal_map)
        self.scan_results = scan_results

        if not self._in_window():
            return

        # Trade best signals — no hard position cap, capital drives quantity
        scan_results.sort(key=lambda r: r.score, reverse=True)
        for result in scan_results:
            key = f"{result.segment}:{result.security_id}"
            if key in self._positions:
                continue
            strategy = self._strategies.get(key)
            if strategy:
                await self._enter_pos(result, strategy)

    async def _fetch_segment(self, segment: str, stocks: List[WatchlistStock]) -> Dict:
        try:
            sids     = [int(s.security_id) for s in stocks]
            data     = await self.client.get_ohlc({segment: sids})
            return data.get("data", {}).get(segment, {})
        except Exception as e:
            logger.warning(f"Fetch error {segment}: {e}")
            return {}

    # ── Position sizing ───────────────────────────────────────────────────────

    def _calc_qty(self, price: float, segment: str, lot_size: int = 1) -> int:
        """
        Derive quantity from available balance capped by risk limit.
        The risk check in BaseStrategy.buy() uses qty × price as estimated loss,
        so we must ensure qty × price ≤ max_loss_per_trade.
        """
        budget     = self._available_balance * self.capital_pct
        risk_limit = self.risk.config.max_loss_per_trade if hasattr(self.risk, "config") else 50_000

        if budget <= 0 or price <= 0:
            return lot_size

        if segment == "NSE_EQ":
            qty_budget = max(1, int(budget / price))
            qty_risk   = max(1, int(risk_limit / price))    # cap so qty×price ≤ risk_limit
            return min(qty_budget, qty_risk)

        elif segment in ("NSE_FNO", "MCX_COMM"):
            # Equity scanner on F&O uses full premium as cost (options buyer)
            cost_lot   = price * lot_size
            if cost_lot <= 0:
                return lot_size
            lots_budget = max(1, int(budget / cost_lot))
            lots_risk   = max(1, int(risk_limit / cost_lot))
            return min(lots_budget, lots_risk) * lot_size

        qty_budget = max(1, int(budget / price))
        qty_risk   = max(1, int(risk_limit / price))
        return min(qty_budget, qty_risk)

    # ── Strategy instances ────────────────────────────────────────────────────

    def _get_strategy(self, key: str, stock: WatchlistStock, segment: str) -> BaseStrategy:
        if key not in self._strategies:
            cfg = self._cfg_type(
                name=f"{self.strategy_key}_{stock.symbol}_{segment}",
                security_id=stock.security_id,
                exchange_segment=segment,
                product_type=SEGMENT_PRODUCT.get(segment, "INTRADAY"),
                quantity=1,
                paper_trading=self.paper_trading,
            )
            self._strategies[key] = self._cfg_cls(self.client, self.risk, cfg)
        return self._strategies[key]

    # ── Entry / exit ──────────────────────────────────────────────────────────

    async def _enter_pos(self, result: ScanResult, strategy: BaseStrategy):
        ok, msg = self.risk.check_order(result.qty, result.price, result.action)
        if not ok:
            logger.warning(f"Risk block {result.symbol}: {msg}")
            return

        # Place hedge leg first if F&O sell
        if self.hedge_fno and result.segment == "NSE_FNO" and result.action == "SELL":
            await self._place_fno_hedge(result)

        strategy.config.quantity = result.qty
        if result.action == "BUY":
            r = await strategy.buy(result.price, result.reason)
        else:
            r = await strategy.sell(result.price, result.reason)

        if r is not None:
            key = f"{result.segment}:{result.security_id}"
            self._positions[key] = result.price
            self.orders_placed  += 1
            self.signals.append(Signal(
                action=result.action, price=result.price,
                reason=f"[SCAN/{result.symbol}/{result.segment}] {result.reason}"
            ))
            logger.info(
                f"📊 {result.action} {result.symbol} ({result.segment}) "
                f"@ ₹{result.price:.2f} qty={result.qty} score={result.score:.1f}"
            )

    async def _place_fno_hedge(self, result: ScanResult):
        """
        Buy a far-OTM option as a defined-risk hedge.
        Converts naked short → spread → dramatically reduces SPAN margin.
        e.g. Sell NIFTY 24500 CE → Buy NIFTY 24700 CE (200pt OTM)
        """
        try:
            from core.instruments import InstrumentMaster
            master = await InstrumentMaster.load()
            # Round price to strike grid and offset
            atm   = round(result.price / 50) * 50
            hedge_strike = atm + self.hedge_offset  # OTM for call; ATM - offset for put
            expiry = None
            for exp in master.nifty_expiries():
                expiry = exp
                break
            if not expiry:
                return

            hedge_map = master.find_atm(
                underlying_price=hedge_strike, expiry=expiry, strike_step=50
            )
            opt_type = "CE" if result.action == "SELL" else "PE"
            hedge    = hedge_map.get(opt_type)
            if not hedge:
                return

            if self.paper_trading:
                logger.info(f"📝 [PAPER] Hedge BUY {opt_type} strike {hedge.strike} sid {hedge.security_id}")
                key = f"{result.segment}:{result.security_id}"
                self._hedge_sids[key] = hedge.security_id
                self.orders_placed += 1
                return

            await self.client.place_order(
                transaction_type="BUY",
                exchange_segment=result.segment,
                product_type="MARGIN",
                order_type="MARKET",
                security_id=hedge.security_id,
                quantity=result.qty,
            )
            key = f"{result.segment}:{result.security_id}"
            self._hedge_sids[key] = hedge.security_id
            logger.info(f"🛡 Hedge placed: BUY {opt_type} @ {hedge.strike} qty={result.qty}")
        except Exception as e:
            logger.warning(f"Hedge placement error: {e}")

    async def _exit_pos(self, key: str, symbol: str, price: float, reason: str, strategy: BaseStrategy):
        await strategy.exit_position(price, reason)
        self._positions.pop(key, None)
        self.orders_placed += 1
        self.signals.append(Signal(action="EXIT", price=price,
                                   reason=f"[SCAN/{symbol}] {reason}"))
        # Exit hedge if any
        hedge_sid = self._hedge_sids.pop(key, None)
        if hedge_sid and not self.paper_trading:
            try:
                await self.client.place_order(
                    transaction_type="SELL", exchange_segment=key.split(":")[0],
                    product_type="MARGIN", order_type="MARKET",
                    security_id=hedge_sid, quantity=strategy.config.quantity,
                )
            except Exception as e:
                logger.warning(f"Hedge exit error: {e}")

    async def _squareoff_all(self):
        if not self._positions:
            return
        logger.info(f"⏰ Scanner squareoff: {len(self._positions)} positions")
        for key in list(self._positions.keys()):
            strategy = self._strategies.get(key)
            if strategy and strategy.position != 0:
                await strategy.exit_position(0.0, "EOD squareoff")
            self._positions.pop(key, None)

    # ── Time helpers ──────────────────────────────────────────────────────────

    def _in_window(self) -> bool:
        t = datetime.now(IST).time()
        return self.TRADE_START <= t <= self.TRADE_END

    def _past_squareoff(self) -> bool:
        return datetime.now(IST).time() >= self.SQUAREOFF

    # ── Dashboard data ────────────────────────────────────────────────────────

    def get_scan_summary(self) -> dict:
        return {
            "strategy":           self.strategy_key,
            "segments":           self.segments,
            "watchlist_size":     len(self.watchlist.get()),
            "open_positions":     len(self._positions),
            "orders_placed":      self.orders_placed,
            "available_balance":  self._available_balance,
            "capital_pct":        self.capital_pct,
            "hedge_fno":          self.hedge_fno,
            "latest_signals":     [
                {"sid": r.security_id, "symbol": r.symbol, "segment": r.segment,
                 "action": r.action, "price": r.price, "qty": r.qty, "score": round(r.score, 2)}
                for r in self.scan_results
            ],
            "positions": [
                {"key": k, "entry_price": v} for k, v in self._positions.items()
            ],
            "stock_signals": self._get_stock_signals(),
        }

    def _get_stock_signals(self) -> dict:
        """Returns per-stock SMA/RSI state for the watchlist gauge display."""
        result = {}
        for key, strategy in self._strategies.items():
            sid = key.split(":")[-1]
            entry = {
                "in_position": strategy.position != 0,
                "signal": "",
                "fast_sma": 0.0,
                "slow_sma": 0.0,
                "gap_pct":  0.0,   # (fast-slow)/slow*100, positive=bullish
                "warmed_up": False,
            }
            # SMA crossover strategy
            if hasattr(strategy, "_fast_prices") and hasattr(strategy, "_slow_prices"):
                fp = list(strategy._fast_prices)
                sp = list(strategy._slow_prices)
                if len(fp) == strategy._fast_prices.maxlen and fp:
                    fast = sum(fp) / len(fp)
                    entry["fast_sma"]  = round(fast, 2)
                if len(sp) == strategy._slow_prices.maxlen and sp:
                    slow = sum(sp) / len(sp)
                    entry["slow_sma"]  = round(slow, 2)
                if entry["fast_sma"] and entry["slow_sma"]:
                    entry["warmed_up"] = True
                    entry["gap_pct"]   = round((entry["fast_sma"] - entry["slow_sma"]) / entry["slow_sma"] * 100, 3)
                    entry["signal"]    = "BUY" if entry["gap_pct"] > 0 else "SELL"
            # RSI strategy
            elif hasattr(strategy, "_prices"):
                prices = list(getattr(strategy, "_prices", []))
                if len(prices) > 1:
                    entry["warmed_up"] = True
                    entry["signal"]    = "BUY" if strategy.position > 0 else ("SELL" if strategy.position < 0 else "")
            result[sid] = entry
        return result
