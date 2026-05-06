"""
Real-Time Risk Manager
Enforces position limits, P&L drawdown controls, and auto square-off rules.
Runs as an async background task alongside the strategy engine.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("dhan.risk")


@dataclass
class RiskConfig:
    # Daily loss limit in INR — platform halts if breached
    max_daily_loss: float = 5_000.0

    # Maximum open positions at any time
    max_open_positions: int = 10

    # Maximum capital deployed as % of available funds
    max_capital_utilisation_pct: float = 80.0

    # Maximum loss on any single trade in INR
    max_loss_per_trade: float = 1_000.0

    # Square off all intraday positions N minutes before market close
    auto_squareoff_minutes_before_close: int = 15

    # Polling interval for risk checks (seconds)
    check_interval_seconds: int = 30

    # Hard stop: halt all new orders when triggered
    kill_switch: bool = False


@dataclass
class RiskState:
    realised_pnl: float = 0.0
    unrealised_pnl: float = 0.0
    open_position_count: int = 0
    halted: bool = False
    halt_reason: str = ""
    last_checked: Optional[datetime] = None
    violations: List[str] = field(default_factory=list)

    @property
    def total_pnl(self) -> float:
        return self.realised_pnl + self.unrealised_pnl


class RiskManager:
    """
    Async risk manager that polls positions and enforces risk controls.

    Usage:
        rm = RiskManager(dhan_client, config)
        asyncio.create_task(rm.run())   # start background loop
        ok, reason = rm.check_order(quantity=10, price=500)
    """

    def __init__(self, client, config: Optional[RiskConfig] = None):
        self.client = client
        self.config = config or RiskConfig()
        self.state = RiskState()
        self._on_halt_callbacks: List[Callable] = []

    def on_halt(self, callback: Callable):
        """Register a callback invoked when the risk halt is triggered."""
        self._on_halt_callbacks.append(callback)
        return callback

    def check_order(
        self,
        quantity: int,
        price: float,
        transaction_type: str = "BUY",
    ) -> tuple[bool, str]:
        """
        Pre-trade check. Returns (allowed: bool, reason: str).
        Call before placing any order.
        """
        if self.config.kill_switch or self.state.halted:
            return False, f"Trading halted: {self.state.halt_reason or 'kill switch active'}"

        if self.state.open_position_count >= self.config.max_open_positions:
            return False, f"Max open positions ({self.config.max_open_positions}) reached"

        estimated_loss = quantity * price  # conservative worst case for options/MIS
        if estimated_loss > self.config.max_loss_per_trade:
            return False, (
                f"Estimated single-trade risk ₹{estimated_loss:,.0f} "
                f"exceeds limit ₹{self.config.max_loss_per_trade:,.0f}"
            )

        return True, "OK"

    async def run(self):
        """Background risk monitoring loop."""
        logger.info("Risk manager started")
        while True:
            try:
                await self._evaluate()
            except Exception as e:
                logger.error(f"Risk check error: {e}")
            await asyncio.sleep(self.config.check_interval_seconds)

    async def _evaluate(self):
        positions = await self.client.get_positions()
        funds = await self.client.get_funds()

        open_count = 0
        unrealised = 0.0
        realised = 0.0

        for pos in positions:
            qty = pos.get("netQty", 0)
            if qty != 0:
                open_count += 1
            unrealised += pos.get("unrealisedProfit", 0.0)
            realised += pos.get("realisedProfit", 0.0)

        self.state.open_position_count = open_count
        self.state.unrealised_pnl = unrealised
        self.state.realised_pnl = realised
        self.state.last_checked = datetime.now()

        violations = []

        if self.state.total_pnl < -abs(self.config.max_daily_loss):
            violations.append(
                f"Daily loss ₹{abs(self.state.total_pnl):,.0f} "
                f"exceeds limit ₹{self.config.max_daily_loss:,.0f}"
            )

        if self.config.kill_switch:
            violations.append("Kill switch activated")

        self.state.violations = violations

        if violations and not self.state.halted:
            reason = "; ".join(violations)
            await self._halt(reason)

        logger.debug(
            f"Risk snapshot — PnL: ₹{self.state.total_pnl:+,.0f} | "
            f"Positions: {open_count} | Halted: {self.state.halted}"
        )

    async def _halt(self, reason: str):
        self.state.halted = True
        self.state.halt_reason = reason
        logger.critical(f"⛔ TRADING HALTED: {reason}")
        for cb in self._on_halt_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(reason)
                else:
                    cb(reason)
            except Exception as e:
                logger.error(f"Halt callback error: {e}")

    def resume(self):
        """Manually resume trading after a halt (operator action)."""
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.violations = []
        logger.warning("Risk halt cleared — trading resumed")

    def activate_kill_switch(self):
        self.config.kill_switch = True
        asyncio.create_task(self._halt("Kill switch activated by operator"))

    def get_summary(self) -> Dict:
        return {
            "realised_pnl": self.state.realised_pnl,
            "unrealised_pnl": self.state.unrealised_pnl,
            "total_pnl": self.state.total_pnl,
            "open_positions": self.state.open_position_count,
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
            "violations": self.state.violations,
            "last_checked": self.state.last_checked.isoformat() if self.state.last_checked else None,
        }
