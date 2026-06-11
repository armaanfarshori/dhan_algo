"""
Risk engine — pre-trade gate, position sizing, and portfolio monitoring.

Two fixes over the old core/risk.py:

1. It watches the PORTFOLIO, not the broker account. The old monitor polled
   client.get_positions() — the real Dhan account — which is empty in paper
   mode, so the daily-loss halt could never fire on paper losses.

2. Position sizing is risk-based: qty = (equity × risk_per_trade) / stop
   distance. Fixed qty=1 made paper P&L statistically meaningless.

The kill switch can also be tripped out-of-process: the api process writes a
flag file, and the monitor loop picks it up within one check interval.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from engine.types import OrderIntent

logger = logging.getLogger("dhan.engine.risk")


@dataclass
class RiskParams:
    max_daily_loss: float = 5_000.0
    max_open_positions: int = 10
    risk_per_trade: float = 0.01          # fraction of equity at risk per trade
    max_notional_per_trade: float = 100_000.0
    equity: float = 500_000.0             # paper balance / live capital
    check_interval_seconds: int = 10
    killswitch_file: Optional[Path] = None  # api process trips this


@dataclass
class RiskState:
    halted: bool = False
    halt_reason: str = ""
    kill_switch: bool = False
    last_checked: Optional[datetime] = None
    violations: List[str] = field(default_factory=list)


class RiskEngine:
    def __init__(self, params: RiskParams, portfolio, ltp_lookup: Callable[[str], float],
                 db_backend=None):
        self.params = params
        self.state = RiskState()
        self._portfolio = portfolio
        self._ltp = ltp_lookup
        self._db = db_backend
        self._on_halt: List[Callable] = []

    def on_halt(self, cb: Callable):
        self._on_halt.append(cb)
        return cb

    # ── Sizing ────────────────────────────────────────────────────────────────

    def size_position(self, entry: float, stop: Optional[float]) -> int:
        """Risk-based sizing. Returns 0 if the trade can't be sized sanely."""
        if entry <= 0:
            return 0
        risk_budget = self.params.equity * self.params.risk_per_trade
        if stop and stop > 0 and abs(entry - stop) > 1e-9:
            qty = int(risk_budget / abs(entry - stop))
        else:
            # No stop given — fall back to notional-based sizing
            qty = int(risk_budget / entry)
        # Notional cap protects against tiny stop distances exploding the size
        max_by_notional = int(self.params.max_notional_per_trade / entry)
        return max(0, min(qty, max_by_notional))

    # ── Pre-trade gate ────────────────────────────────────────────────────────

    def check_intent(self, intent: OrderIntent, ref_price: float) -> tuple[bool, str]:
        if self.state.kill_switch or self.state.halted:
            return False, f"Trading halted: {self.state.halt_reason or 'kill switch active'}"
        if intent.qty <= 0:
            return False, "Quantity sized to 0 — risk budget too small for this stop distance"

        pos = self._portfolio.get(intent.security_id)
        is_exit = pos.qty != 0 and (
            (pos.qty > 0 and intent.side == "SELL") or (pos.qty < 0 and intent.side == "BUY"))
        if not is_exit and self._portfolio.open_count() >= self.params.max_open_positions:
            return False, f"Max open positions ({self.params.max_open_positions}) reached"

        notional = intent.qty * ref_price
        if not is_exit and notional > self.params.max_notional_per_trade:
            return False, (f"Notional ₹{notional:,.0f} exceeds per-trade cap "
                           f"₹{self.params.max_notional_per_trade:,.0f}")
        return True, "OK"

    # ── Monitoring loop ───────────────────────────────────────────────────────

    async def run(self):
        logger.info("RiskEngine: monitoring portfolio (daily-loss limit ₹%s)",
                    f"{self.params.max_daily_loss:,.0f}")
        while True:
            try:
                await self._evaluate()
            except Exception as exc:
                logger.error("Risk check error: %s", exc)
            await asyncio.sleep(self.params.check_interval_seconds)

    async def _evaluate(self):
        # Out-of-process kill switch (written by the api process). Goes
        # through _halt() so on_halt callbacks (flatten + alert) fire.
        ks_file = self.params.killswitch_file
        if ks_file and ks_file.exists() and not self.state.kill_switch:
            reason = ks_file.read_text().strip() or "kill switch file"
            self.state.kill_switch = True
            logger.critical("⛔ KILL SWITCH (external): %s", reason)
            await self._halt(f"Kill switch: {reason}")

        realized = self._portfolio.realized_pnl
        unrealized = self._portfolio.unrealized_pnl(self._ltp)
        total = realized + unrealized
        self.state.last_checked = datetime.now()

        violations = []
        if total < -abs(self.params.max_daily_loss):
            violations.append(f"Daily loss ₹{abs(total):,.0f} exceeds "
                              f"limit ₹{self.params.max_daily_loss:,.0f}")
        self.state.violations = violations

        if violations and not self.state.halted:
            await self._halt("; ".join(violations))

        if self._db:
            try:
                await self._db.snapshot_equity(
                    cash=self.params.equity + realized,
                    holdings_value=sum(
                        abs(p.qty) * float(self._ltp(p.security_id) or p.avg_price)
                        for p in self._portfolio.open_positions()),
                    realized_pnl=realized, unrealized_pnl=unrealized, drawdown=0.0)
            except Exception:
                pass   # equity snapshot is best-effort

    async def _halt(self, reason: str):
        self.state.halted = True
        self.state.halt_reason = reason
        logger.critical("⛔ TRADING HALTED: %s", reason)
        for cb in self._on_halt:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(reason)
                else:
                    cb(reason)
            except Exception as exc:
                logger.error("Halt callback error: %s", exc)

    def activate_kill_switch(self, reason: str = "operator"):
        self.state.kill_switch = True
        self.state.halted = True
        self.state.halt_reason = f"Kill switch: {reason}"
        logger.critical("⛔ KILL SWITCH: %s", reason)

    def resume(self):
        self.state.halted = False
        self.state.kill_switch = False
        self.state.halt_reason = ""
        self.state.violations = []
        if self.params.killswitch_file and self.params.killswitch_file.exists():
            self.params.killswitch_file.unlink()
        logger.warning("Risk halt cleared — trading resumed")

    def get_summary(self) -> dict:
        realized = self._portfolio.realized_pnl
        unrealized = self._portfolio.unrealized_pnl(self._ltp)
        return {
            "realised_pnl": round(realized, 2),
            "unrealised_pnl": round(unrealized, 2),
            "total_pnl": round(realized + unrealized, 2),
            "open_positions": self._portfolio.open_count(),
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
            "violations": self.state.violations,
            "last_checked": self.state.last_checked.isoformat() if self.state.last_checked else None,
        }
