"""
Risk engine — pre-trade gate, position sizing, and portfolio monitoring.

Specification principles (2026-06-13 rework):

1. All limits are FRACTIONS of equity, never absolute rupees — paper and
   live share one risk geometry, so paper actually rehearses live. Live
   mode additionally scales every fraction by live_risk_scale (training
   wheels for M8 tiny-live).
2. Daily/weekly P&L comes from the DATABASE, not process memory. A restart
   must never reset the loss meter (it did, on 2026-06-12 — the morning's
   −₹3,533 vanished from the risk view mid-session).
3. A loss halt PERSISTS across restarts (run/halt_state.json) for its scope
   (day or week) and requires deliberate human reset. The kill-switch file
   already persisted; the daily-loss halt did not.
4. Aggregate open risk is capped: new entries are blocked once committed
   stop-distance risk plus today's realized losses reach the daily budget,
   so the daily limit is enforced BEFORE the loss exists, not 10s after.
5. Sizing respects microstructure: stop distance is floored (tiny ORB
   ranges must not explode size into slippage-dominated trades) and qty is
   capped at a fraction of 20-day ADV (a fill paper can simulate honestly
   is one the live market can absorb).
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from engine.types import OrderIntent

logger = logging.getLogger("dhan.engine.risk")


@dataclass
class RiskParams:
    equity_base: float = 500_000.0        # starting capital (paper balance / live capital)
    risk_per_trade: float = 0.005         # fraction of equity at risk per trade
    max_daily_loss_pct: float = 0.02      # halt + flatten for the day
    weekly_loss_pct: float = 0.05         # halt until next week
    max_notional_pct: float = 0.20        # per-trade notional cap
    max_gross_exposure_pct: float = 1.00  # Σ|position notional| cap — no implicit leverage
    adv_participation_pct: float = 0.01   # qty ≤ this fraction of 20-day ADV
    min_stop_distance_pct: float = 0.0035 # stop floor: 0.35% of entry
    max_open_positions: int = 10
    check_interval_seconds: int = 10
    killswitch_file: Optional[Path] = None  # api process trips this
    halt_file: Optional[Path] = None        # loss halts persist here across restarts
    resume_file: Optional[Path] = None      # api process drops this to clear a halt


@dataclass
class RiskState:
    halted: bool = False
    halt_reason: str = ""
    halt_scope: str = ""                  # "day" | "week" | "" (kill switch)
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
        self._on_resume: List[Callable] = []
        # DB-derived P&L cache, refreshed each monitor cycle (and at boot).
        self._realized_total = 0.0
        self._realized_today = 0.0
        self._realized_week = 0.0
        # Committed stop-distance risk per open position.
        self._open_risk: Dict[str, float] = {}
        self._adv_cache: Dict[str, float] = {}

    def on_halt(self, cb: Callable):
        self._on_halt.append(cb)
        return cb

    def on_resume(self, cb: Callable):
        self._on_resume.append(cb)
        return cb

    # ── Equity & budgets (all derived, all fractions of current equity) ──────

    @property
    def equity(self) -> float:
        """Current equity = starting capital + all-time realized P&L (from DB).
        Sizing compounds gains and — more importantly — shrinks in drawdown."""
        return max(1.0, self.params.equity_base + self._realized_total)

    @property
    def risk_budget_per_trade(self) -> float:
        return self.equity * self.params.risk_per_trade

    @property
    def daily_loss_budget(self) -> float:
        return self.equity * self.params.max_daily_loss_pct

    @property
    def weekly_loss_budget(self) -> float:
        return self.equity * self.params.weekly_loss_pct

    @property
    def committed_risk(self) -> float:
        return sum(self._open_risk.values())

    # ── DB-backed P&L (restart-proof) ─────────────────────────────────────────

    async def refresh_pnl(self):
        """Pull realized P&L (all-time / today / this week) from the trades
        table. Falls back to the in-process portfolio on DB failure."""
        def _query():
            from sqlalchemy import text
            from db import get_session
            with get_session() as s:
                return s.execute(text("""
                    SELECT COALESCE(SUM(pnl), 0),
                           COALESCE(SUM(pnl) FILTER (WHERE exit_ts >= timezone('Asia/Kolkata', date_trunc('day', timezone('Asia/Kolkata', now())))), 0),  -- IST trading day (not UTC CURRENT_DATE)
                           COALESCE(SUM(pnl) FILTER (
                               WHERE exit_ts >= timezone('Asia/Kolkata', date_trunc('week', timezone('Asia/Kolkata', now())))), 0)  -- IST trading week (not UTC CURRENT_DATE)
                    FROM trades WHERE status = 'CLOSED'
                """)).fetchone()
        try:
            row = await asyncio.get_running_loop().run_in_executor(None, _query)
            self._realized_total = float(row[0])
            self._realized_today = float(row[1])
            self._realized_week = float(row[2])
        except Exception as exc:
            logger.warning("Risk: DB P&L refresh failed (%s) — using process-local "
                           "realized for today; weekly value preserved from last DB read "
                           "(restart-unsafe)", exc)
            self._realized_today = self._portfolio.realized_pnl
            # Do NOT overwrite _realized_week with session-only P&L: the weekly meter
            # could be orders of magnitude larger (losses from Mon–Thu wouldn't show on
            # a Fri restart). Keep the last DB-derived value; it may be stale but it is
            # never an understatement compared to replacing it with ≤1-day session P&L.
            # _realized_week intentionally left unchanged.

    # ── Persistent loss halts ─────────────────────────────────────────────────

    def load_persisted_halt(self):
        """On boot: re-enter a halt that is still in scope. A restart must
        not silently re-arm a halted session."""
        hf = self.params.halt_file
        if not hf or not hf.exists():
            return
        try:
            data = json.loads(hf.read_text())
        except Exception:
            hf.unlink(missing_ok=True)
            return
        today = date.today()
        y, w = today.isocalendar()[:2]
        in_scope = (
            (data.get("scope") == "day" and data.get("date") == today.isoformat())
            or (data.get("scope") == "week"
                and data.get("week") == f"{y}-W{w:02d}")
        )
        if in_scope:
            self.state.halted = True
            self.state.halt_scope = data.get("scope", "day")
            self.state.halt_reason = f"persisted halt: {data.get('reason', '')}"
            logger.critical("⛔ HALT RESTORED FROM DISK (%s): %s — manual reset required "
                            "(delete %s + restart, or POST resume)",
                            self.state.halt_scope, data.get("reason"), hf)
        else:
            hf.unlink(missing_ok=True)   # stale — previous day/week

    def _persist_halt(self, reason: str, scope: str):
        hf = self.params.halt_file
        if not hf:
            return
        try:
            today = date.today()
            y, w = today.isocalendar()[:2]
            hf.write_text(json.dumps({
                "scope": scope, "reason": reason,
                "date": today.isoformat(),
                "week": f"{y}-W{w:02d}",
                "ts": datetime.now(timezone.utc).isoformat(),  # T5: tz-aware
            }))
        except Exception as exc:
            logger.error("Risk: failed to persist halt state: %s", exc)

    # ── Sizing ────────────────────────────────────────────────────────────────

    def size_position(self, entry: float, stop: Optional[float],
                      adv: Optional[float] = None) -> int:
        """Risk-based sizing with a stop-distance floor and liquidity cap.
        Returns 0 if the trade can't be sized sanely."""
        if entry <= 0:
            return 0
        # Floor the stop distance — a paper-thin ORB range must not explode
        # size into a trade whose real risk is all slippage.
        floor = entry * self.params.min_stop_distance_pct
        if stop and stop > 0:
            dist = max(abs(entry - stop), floor)
            qty = int(self.risk_budget_per_trade / dist)
        else:
            # No stop — treat full entry as at risk (very conservative)
            qty = int(self.risk_budget_per_trade / entry)
        max_by_notional = int(self.equity * self.params.max_notional_pct / entry)
        qty = min(qty, max_by_notional)
        if adv and adv > 0:
            qty = min(qty, int(adv * self.params.adv_participation_pct))
        return max(0, qty)

    def position_risk(self, entry: float, stop: Optional[float], qty: int) -> float:
        """Rupee risk this position commits against the daily budget."""
        floor = entry * self.params.min_stop_distance_pct
        dist = max(abs(entry - stop), floor) if stop and stop > 0 else entry
        return qty * dist

    async def get_adv(self, security_id: str) -> Optional[float]:
        """~20-trading-day average daily volume from the 1d bars (cached per session).

        Uses a time-bounded WHERE clause (≥ CURRENT_DATE - 30 days) so TimescaleDB
        can exclude old chunks entirely — no full scan, no decompression of historical
        chunks.  ~30 calendar days ≈ ~20 trading days of 1d bars, close enough for
        the ADV liquidity cap.

        The old query used ORDER BY time DESC LIMIT 20, which is the banned anti-pattern
        on this 300M-row hypertable: it forces a chunk scan + sort before the LIMIT
        prunes rows, making it orders of magnitude slower than chunk-excluded aggregates.
        """
        if security_id in self._adv_cache:
            return self._adv_cache[security_id]
        def _query():
            from sqlalchemy import text
            from db import get_session
            with get_session() as s:
                return s.execute(text("""
                    SELECT AVG(volume) FROM bars
                    WHERE security_id = :sid AND timeframe = '1d'
                      AND time >= (CURRENT_DATE - INTERVAL '30 days')
                """), {"sid": security_id}).scalar()
        try:
            adv = await asyncio.get_running_loop().run_in_executor(None, _query)
            adv = float(adv) if adv else None
        except Exception as exc:
            logger.warning("Risk: ADV lookup failed for %s (%s) — no liquidity cap",
                           security_id, exc)
            adv = None
        self._adv_cache[security_id] = adv
        return adv

    # ── Open-risk registry ────────────────────────────────────────────────────

    def register_risk(self, security_id: str, amount: float):
        self._open_risk[security_id] = max(0.0, amount)

    def release_risk(self, security_id: str):
        self._open_risk.pop(security_id, None)

    # ── Pre-trade gate ────────────────────────────────────────────────────────

    def check_intent(self, intent: OrderIntent, ref_price: float) -> tuple[bool, str]:
        if self.state.kill_switch or self.state.halted:
            return False, f"Trading halted: {self.state.halt_reason or 'kill switch active'}"
        if intent.qty <= 0:
            return False, "Quantity sized to 0 — risk budget too small for this stop distance"

        pos = self._portfolio.get(intent.security_id)
        is_exit = pos.qty != 0 and (
            (pos.qty > 0 and intent.side == "SELL") or (pos.qty < 0 and intent.side == "BUY"))
        if is_exit:
            return True, "OK"   # exits are never blocked by exposure limits

        if self._portfolio.open_count() >= self.params.max_open_positions:
            return False, f"Max open positions ({self.params.max_open_positions}) reached"

        notional = intent.qty * ref_price
        max_notional = self.equity * self.params.max_notional_pct
        if notional > max_notional + 1e-6:
            return False, (f"Notional ₹{notional:,.0f} exceeds per-trade cap "
                           f"₹{max_notional:,.0f} ({self.params.max_notional_pct:.0%} of equity)")

        # Gross exposure — no accidental leverage
        gross = sum(abs(p.qty) * float(self._ltp(p.security_id) or p.avg_price)
                    for p in self._portfolio.open_positions())
        max_gross = self.equity * self.params.max_gross_exposure_pct
        if gross + notional > max_gross + 1e-6:
            return False, (f"Gross exposure ₹{gross + notional:,.0f} would exceed "
                           f"₹{max_gross:,.0f} ({self.params.max_gross_exposure_pct:.0%} of equity)")

        # Daily risk budget — committed stop-risk + today's realized losses
        # must leave room for this trade's risk BEFORE it goes on
        new_risk = self.position_risk(ref_price, intent.stop_price, intent.qty)
        consumed = self.committed_risk + max(0.0, -self._realized_today)
        if consumed + new_risk > self.daily_loss_budget + 1e-6:
            return False, (f"Daily risk budget exhausted: committed ₹{self.committed_risk:,.0f}"
                           f" + realized losses ₹{max(0.0, -self._realized_today):,.0f}"
                           f" + new ₹{new_risk:,.0f} > budget ₹{self.daily_loss_budget:,.0f}")
        return True, "OK"

    # ── Monitoring loop ───────────────────────────────────────────────────────

    async def run(self):
        await self.refresh_pnl()
        logger.info("RiskEngine: equity ₹%s · daily budget ₹%s (%.1f%%) · weekly ₹%s (%.1f%%)",
                    f"{self.equity:,.0f}", f"{self.daily_loss_budget:,.0f}",
                    self.params.max_daily_loss_pct * 100,
                    f"{self.weekly_loss_budget:,.0f}", self.params.weekly_loss_pct * 100)
        while True:
            try:
                await self._evaluate()
            except Exception as exc:
                logger.error("Risk check error: %s", exc)
            await asyncio.sleep(self.params.check_interval_seconds)

    async def _evaluate(self):
        # Out-of-process resume (written by the api process). Clear the halt and
        # delete the kill-switch / persisted-halt markers so we don't re-trip.
        # We do NOT return early: the normal loss checks below run again this
        # tick, so an operator can never click "resume" past a still-breached
        # daily/weekly loss budget — only a kill-switch or a recovered P&L sticks.
        rf = self.params.resume_file
        if rf and rf.exists():
            if self.state.halted or self.state.kill_switch:
                logger.warning("▶ RESUME requested (dashboard) — clearing halt")
                self.resume()                  # clears state + killswitch/halt files
                rf.unlink(missing_ok=True)     # consume ONLY after resume() succeeds,
                                               # so a raised resume() is retried next tick
                for cb in self._on_resume:
                    try:
                        if asyncio.iscoroutinefunction(cb):
                            await cb()
                        else:
                            cb()
                    except Exception as exc:
                        logger.error("Resume callback error: %s", exc)
            else:
                # Stray flag while healthy (e.g. left over from before a halt) —
                # consume it so it can't fire later, but don't pretend we resumed.
                logger.info("Resume flag present but engine not halted — ignoring")
                rf.unlink(missing_ok=True)

        # Out-of-process kill switch (written by the api process). Goes
        # through _halt() so on_halt callbacks (flatten + alert) fire.
        ks_file = self.params.killswitch_file
        if ks_file and ks_file.exists() and not self.state.kill_switch:
            reason = ks_file.read_text().strip() or "kill switch file"
            self.state.kill_switch = True
            logger.critical("⛔ KILL SWITCH (external): %s", reason)
            await self._halt(f"Kill switch: {reason}", scope="")

        await self.refresh_pnl()
        unrealized = self._portfolio.unrealized_pnl(self._ltp)
        day_total = self._realized_today + unrealized
        week_total = self._realized_week + unrealized
        self.state.last_checked = datetime.now(timezone.utc)  # T5: tz-aware

        violations, scope = [], "day"
        if day_total < -self.daily_loss_budget:
            violations.append(f"Daily loss ₹{abs(day_total):,.0f} exceeds "
                              f"budget ₹{self.daily_loss_budget:,.0f}")
        if week_total < -self.weekly_loss_budget:
            violations.append(f"Weekly loss ₹{abs(week_total):,.0f} exceeds "
                              f"budget ₹{self.weekly_loss_budget:,.0f}")
            scope = "week"
        self.state.violations = violations

        if violations and not self.state.halted:
            await self._halt("; ".join(violations), scope=scope)

        if self._db:
            try:
                await self._db.snapshot_equity(
                    cash=self.equity,
                    holdings_value=sum(
                        abs(p.qty) * float(self._ltp(p.security_id) or p.avg_price)
                        for p in self._portfolio.open_positions()),
                    realized_pnl=self._realized_today, unrealized_pnl=unrealized,
                    drawdown=0.0)
            except Exception:
                pass   # equity snapshot is best-effort

    async def _halt(self, reason: str, scope: str = "day"):
        self.state.halted = True
        self.state.halt_reason = reason
        self.state.halt_scope = scope
        if scope:
            self._persist_halt(reason, scope)   # loss halts survive restarts
        logger.critical("⛔ TRADING HALTED (%s): %s", scope or "kill-switch", reason)
        for cb in self._on_halt:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(reason)
                else:
                    cb(reason)
            except Exception as exc:
                logger.error("Halt callback error: %s", exc)

    def activate_kill_switch(self, reason: str = "operator"):
        """Activate the kill switch and fire on_halt callbacks (flatten positions).

        If called from within a running asyncio event loop (normal production path in
        trader.py), `_halt` is scheduled as a coroutine task so all async on_halt
        callbacks (e.g. executor flatten) fire correctly.

        If called from a synchronous context with no running loop (tests, CLI), the
        halt state is set immediately and synchronous on_halt callbacks are called
        directly; async callbacks are scheduled via asyncio.run() in a best-effort
        fashion (logs a warning if any are present).
        """
        full_reason = f"Kill switch: {reason}"
        # Set the flag eagerly on BOTH paths so check_intent() and get_summary()
        # never report kill_switch=False in the window before the scheduled _halt
        # task runs. _halt does not set this field; it is owned here.
        self.state.kill_switch = True
        try:
            loop = asyncio.get_running_loop()
            # We are inside an async context — schedule _halt as a task so all
            # async on_halt callbacks (position flattening) are awaited properly.
            loop.create_task(self._halt(full_reason, scope=""))
        except RuntimeError:
            # No running event loop — synchronous caller (tests, CLI tools).
            # Set state immediately so check_intent blocks right away, then fire
            # sync callbacks inline and async ones via asyncio.run().
            self.state.kill_switch = True
            self.state.halted = True
            self.state.halt_reason = full_reason
            self.state.halt_scope = ""
            logger.critical("⛔ KILL SWITCH: %s", reason)
            async_cbs = []
            for cb in self._on_halt:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        async_cbs.append(cb)
                    else:
                        cb(full_reason)
                except Exception as exc:
                    logger.error("Halt callback error: %s", exc)
            if async_cbs:
                async def _run_async_cbs():
                    for cb in async_cbs:
                        try:
                            await cb(full_reason)
                        except Exception as exc:
                            logger.error("Halt callback error: %s", exc)
                asyncio.run(_run_async_cbs())

    def resume(self):
        self.state.halted = False
        self.state.kill_switch = False
        self.state.halt_reason = ""
        self.state.halt_scope = ""
        self.state.violations = []
        if self.params.killswitch_file and self.params.killswitch_file.exists():
            self.params.killswitch_file.unlink()
        if self.params.halt_file and self.params.halt_file.exists():
            self.params.halt_file.unlink()
        logger.warning("Risk halt cleared — trading resumed")

    def get_summary(self) -> dict:
        unrealized = self._portfolio.unrealized_pnl(self._ltp)
        return {
            "realised_pnl": round(self._realized_today, 2),
            "unrealised_pnl": round(unrealized, 2),
            "total_pnl": round(self._realized_today + unrealized, 2),
            "week_pnl": round(self._realized_week + unrealized, 2),
            "equity": round(self.equity, 2),
            "daily_loss_budget": round(self.daily_loss_budget, 2),
            "weekly_loss_budget": round(self.weekly_loss_budget, 2),
            "committed_risk": round(self.committed_risk, 2),
            "open_positions": self._portfolio.open_count(),
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
            "halt_scope": self.state.halt_scope,
            "kill_switch": self.state.kill_switch,
            "violations": self.state.violations,
            "last_checked": (self.state.last_checked.isoformat()
                             if self.state.last_checked else None),
        }
