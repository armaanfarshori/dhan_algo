"""
ScalperOrchestrator — the async task that owns the intraday options scalper across
BOTH indices (NIFTY + BANKNIFTY) on one sleeve.

This is the *orchestration* layer.  It is deliberately decoupled from the concrete
signal engine (``strategies/options_scalper.py``) and the concrete option-resolver
(the F&O screener) via two narrow Protocols defined here:

    SignalProvider.poll(underlying, now)  -> ScalpSignal | None
    OptionResolver.resolve(signal)        -> OptionLeg   | None

The real implementations (the per-index ``OptionsScalper`` instances and the
screener/instrument lookup) are wired in a *later* PR (apps/trader.py).  This file
defines against the interfaces only and must never import the trader or the risk
engine's internals — it *consumes* ``Portfolio`` and ``RiskEngine`` through their
existing public methods (``apply_fill``, ``check_intent``, ``register_risk`` …).

PER-DAY STATE MACHINE (one orchestrator owns both indices)
    PRE_OPEN  → before the session warm-up window; manage nothing, open nothing.
    ARMED     → in-session, governor permitting; entries + exits run.
    MANAGING  → entries are off (governor STOOD_DOWN/TRAIL_ONLY or post-T) but
                open contracts are still actively managed/exited.
    FLATTEN_ALL → at/after the unconditional EOD square-off: flatten everything.
    DONE      → flat and past square-off; idle until the next session date.

TICK ORDER (the inviolable ordering, mirrors engine/risk.py + the scalper specs):
    1. MANAGE EXITS on every open contract FIRST (exits are NEVER gated).
    2. UNCONDITIONAL EOD square-off gate — ABOVE everything (SessionClock(EQUITY)
       squareoff_time ≈ 15:15).  Flatten all; emit no new entries past it.
    3. GOVERNOR + halt gate — entries only (RiskEngine halt/kill + DailyGovernor).
    4. PER-UNDERLYING entry scan (NIFTY then BANKNIFTY), with a per-index
       cooldown-after-exit.

Hard rules honoured:
  • EXITS ARE NEVER BLOCKED — not by the governor, not by a RiskEngine halt/kill,
    not by caps.  ``RiskEngine.check_intent`` already returns OK for exits; this
    orchestrator additionally never even *asks* the governor about an exit.
  • EOD square-off is unconditional and above the governor.
  • The RiskEngine owns the kill-switch; the governor only *requests* a flatten
    (routed through the same flatten path), never owning its own exit route.
  • Lot-aligned sizing — quantity is always a whole multiple of the contract lot.
  • PAPER throughout (mode is the executor's concern; this layer is mode-blind).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from core.sessions import SessionClock, get_session
from engine.daily_governor import DailyGovernor, GovernorParams
from engine.types import Fill, OrderIntent

logger = logging.getLogger("dhan.engine.scalper_orchestrator")
IST = ZoneInfo("Asia/Kolkata")

# Orchestrator states (the per-day state machine).
PRE_OPEN = "PRE_OPEN"
ARMED = "ARMED"
MANAGING = "MANAGING"
FLATTEN_ALL = "FLATTEN_ALL"
DONE = "DONE"

# Strategy tag persisted on every scalper trade (used to reconcile on boot +
# to seed the day's meter from the journal).
SCALPER_STRATEGY = "options_scalper"


# ---------------------------------------------------------------------------
# Interface value types + Protocols (the real impls are wired later)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScalpSignal:
    """A directional scalp intent emitted by a per-index SignalProvider.

    ``action`` is "ENTER" or "EXIT".  For ENTER, ``side`` is always "BUY"
    (long-only options).  For EXIT, ``side`` is "SELL".  ``lots`` is the
    tranche size in *lots* (not units)."""

    underlying: str            # "NIFTY" | "BANKNIFTY"
    action: str                # "ENTER" | "EXIT"
    side: str                  # "BUY" (enter) | "SELL" (exit)
    option_type: str = ""      # "CE" | "PE"
    strike: int = 0
    lots: int = 1
    reason: str = ""


@dataclass(frozen=True)
class OptionLeg:
    """A concrete tradable option contract resolved from a ScalpSignal.

    Carries its own ``lot`` so the orchestrator can size in whole lots without
    hardcoding per-index lot tables (BANKNIFTY ≠ NIFTY, and the exchange revises
    them — the resolver/screener is the source of truth)."""

    underlying: str
    security_id: str
    exchange_segment: str      # e.g. "NSE_FNO"
    option_type: str           # "CE" | "PE"
    strike: int
    lot: int                   # contract lot size (units per lot)
    ltp: float = 0.0           # reference premium for the executor


@runtime_checkable
class SignalProvider(Protocol):
    """Per-underlying signal engine (the real impl wraps ``OptionsScalper``)."""

    def poll(self, underlying: str, now: datetime) -> Optional[ScalpSignal]:
        """Return the next ENTER/EXIT signal for ``underlying``, or None."""
        ...

    def notify_fill(self, underlying: str, side: str, lots: int,
                    premium: float, now: datetime) -> None:
        """Confirm a fill back to the per-index engine so its book stays correct."""
        ...

    def open_lots(self, underlying: str) -> int:
        """Lots the engine believes are open for ``underlying`` (for exit scan)."""
        ...


@runtime_checkable
class OptionResolver(Protocol):
    """Resolves a ScalpSignal into a concrete tradable contract (real impl =
    the F&O screener / instrument lookup)."""

    def resolve(self, signal: ScalpSignal) -> Optional[OptionLeg]:
        ...


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorParams:
    underlyings: List[str] = field(default_factory=lambda: ["NIFTY", "BANKNIFTY"])
    session_name: str = "EQUITY"        # SessionClock profile — index options
                                        # trade the equity clock (square-off ~15:15).
    tick_interval_seconds: float = 5.0  # poll cadence of the async task
    cooldown_min: int = 3               # per-index pause after a full flatten
    warmup_min: int = 15                # no entries before OPEN + this
    governor: GovernorParams = field(default_factory=GovernorParams)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ScalperOrchestrator:
    """Owns the scalper across both indices.  One async ``run`` loop; pure,
    testable ``tick(now)`` underneath it.

    Dependencies (consumed via existing public interfaces — never edited here):
        signals   : SignalProvider
        resolver  : OptionResolver
        portfolio : engine.portfolio.Portfolio  (apply_fill, get, open_positions)
        risk      : engine.risk.RiskEngine       (check_intent, register/release_risk)
        executor  : engine.execution.OrderExecutor (submit)
    """

    def __init__(self, *, signals: SignalProvider, resolver: OptionResolver,
                 portfolio, risk, executor,
                 params: Optional[OrchestratorParams] = None):
        self.signals = signals
        self.resolver = resolver
        self.portfolio = portfolio
        self.risk = risk
        self.executor = executor
        self.p = params or OrchestratorParams()

        self.clock = SessionClock(get_session(self.p.session_name))
        self.governor = DailyGovernor(self.p.governor)

        self.state: str = PRE_OPEN
        self._session_date: Optional[date] = None

        # Per-index live contracts the orchestrator is managing (security_id →
        # OptionLeg).  Mirrors the portfolio but keyed by underlying for the
        # exit scan + cooldown.
        self._legs: Dict[str, Dict[str, OptionLeg]] = {
            u: {} for u in self.p.underlyings}
        # Per-index cooldown after a full flatten.
        self._cooldown_until: Dict[str, Optional[datetime]] = {
            u: None for u in self.p.underlyings}

        self._running = False

    # ── async driver ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        """The orchestrator async task.  Boots (reconcile + seed), then ticks on
        ``tick_interval_seconds`` until cancelled."""
        await self.boot()
        self._running = True
        logger.info("ScalperOrchestrator: running — underlyings=%s session=%s",
                    self.p.underlyings, self.p.session_name)
        try:
            while self._running:
                try:
                    await self.tick(self.clock.now())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:   # never let one tick kill the loop
                    logger.error("ScalperOrchestrator tick error: %s", exc)
                await asyncio.sleep(self.p.tick_interval_seconds)
        except asyncio.CancelledError:
            logger.info("ScalperOrchestrator: cancelled")
            raise

    def stop(self) -> None:
        self._running = False

    # ── boot: reconcile open SCALPER contracts + seed the day's meter ─────────

    async def boot(self) -> None:
        """Restart-safety.  Reconcile open scalper contracts from the Portfolio
        into MANAGING and seed ``trades_today`` / realized P&L into the governor
        from the journal — so a mid-session restart does NOT reset the day's caps
        (the engine/risk.py lesson)."""
        today = self.clock.session_date()
        self._session_date = today
        self.governor.reset_session(today)

        # 1. Reconcile open scalper positions the Portfolio restored on boot.
        reconciled = 0
        try:
            open_positions = self.portfolio.open_positions()
        except Exception as exc:
            logger.error("ScalperOrchestrator boot: portfolio read failed (%s)", exc)
            open_positions = []
        for pos in open_positions:
            if (pos.strategy or "") != SCALPER_STRATEGY:
                continue
            leg = self._resolve_open_position(pos)
            if leg is None:
                logger.warning("ScalperOrchestrator boot: cannot resolve underlying "
                               "for open scalper position %s — leaving to EOD flatten",
                               pos.security_id)
                continue
            self._legs[leg.underlying][pos.security_id] = leg
            self.governor.record_open(leg.underlying, contracts=1)
            reconciled += 1

        # 2. Seed the day's trade-count + realized P&L meter from the journal.
        await self._seed_governor_from_journal(today)

        # If we restored a live book, we boot into MANAGING (manage what we hold);
        # otherwise the normal state machine picks PRE_OPEN/ARMED on the first tick.
        if reconciled > 0:
            self.state = MANAGING
            logger.warning("ScalperOrchestrator boot: reconciled %d open scalper "
                           "contracts → MANAGING", reconciled)

    def _resolve_open_position(self, pos) -> Optional[OptionLeg]:
        """Best-effort: rebuild an OptionLeg for a reconciled open position.  The
        real screener can map a security_id → contract; here we ask the resolver
        with a synthetic EXIT signal carrying just the security context, falling
        back to None (the EOD square-off still flattens it via the portfolio)."""
        for u in self.p.underlyings:
            sig = ScalpSignal(underlying=u, action="EXIT", side="SELL",
                              reason="boot-reconcile probe")
            leg = self.resolver.resolve(sig)
            if leg is not None and leg.security_id == pos.security_id:
                return leg
        return None

    async def _seed_governor_from_journal(self, today: date) -> None:
        """Re-derive per-underlying trades_opened / realized_pnl / consec_losses
        for *today* from the trades journal, like RiskEngine.refresh_pnl.  Runs in
        a thread executor; failure leaves the meter at zero (documented gap)."""
        db = getattr(self.portfolio, "_db", None)
        if db is None or not getattr(db, "_enabled", False):
            logger.info("ScalperOrchestrator: no DB — governor meter starts at zero "
                        "(restart resets the day's caps — known PAPER gap)")
            return

        def _query():
            from sqlalchemy import text
            import db as _db
            with _db.get_session() as s:
                # Per-underlying is not stored on trades (only security_id); the
                # scalper tags every row strategy=SCALPER_STRATEGY, so we seed the
                # SLEEVE aggregate by underlying-agnostic counting and attribute by
                # the leg map we already reconciled.  Counts today's CLOSED+OPEN
                # scalper trades and today's realized P&L.
                opened = s.execute(text("""
                    SELECT COUNT(*) FROM trades
                    WHERE strategy = :strat
                      AND entry_ts >= timezone('Asia/Kolkata',
                            date_trunc('day', timezone('Asia/Kolkata', now())))
                """), {"strat": SCALPER_STRATEGY}).scalar() or 0
                realized = s.execute(text("""
                    SELECT COALESCE(SUM(pnl), 0) FROM trades
                    WHERE strategy = :strat AND status = 'CLOSED'
                      AND exit_ts >= timezone('Asia/Kolkata',
                            date_trunc('day', timezone('Asia/Kolkata', now())))
                """), {"strat": SCALPER_STRATEGY}).scalar() or 0.0
                return int(opened), float(realized)

        try:
            opened, realized = await asyncio.get_running_loop().run_in_executor(
                None, _query)
        except Exception as exc:
            logger.warning("ScalperOrchestrator: governor seed query failed (%s) — "
                           "meter starts at zero", exc)
            return

        # Attribute the sleeve trade-count + realized P&L to the first underlying
        # as a conservative seed (the caps are enforced at the sleeve aggregate
        # too, so the day's budget is correctly consumed even if the per-index
        # split is approximate — the journal only stores security_id, not index).
        # Concurrency is NOT seeded here: reconcile (record_open above) already
        # owns the per-underlying open_contracts meter; passing it would clobber
        # the correct per-index concurrency with the aggregate total.
        seed_under = self.p.underlyings[0] if self.p.underlyings else "NIFTY"
        self.governor.seed_trades_today(
            seed_under, trades_opened=opened, realized_pnl=realized)
        logger.warning("ScalperOrchestrator: seeded governor from journal — "
                       "trades_today=%d realized=₹%.0f", opened, realized)

    # ── the tick ──────────────────────────────────────────────────────────────

    async def tick(self, now: datetime) -> None:
        """One orchestration cycle.  Pure ordering; all IO is delegated to the
        injected dependencies.  Safe to call directly from tests."""
        today = self.clock.session_date(now)
        if self._session_date != today:
            self._reset_session(today)

        # 1. MANAGE EXITS FIRST — on every open contract, both indices.  Exits
        #    are NEVER gated by the governor or a halt.
        await self._manage_exits(now)

        # 2. UNCONDITIONAL EOD SQUARE-OFF — above everything.
        squareoff = self.clock.squareoff_time(today)
        if now.time() >= squareoff:
            await self._flatten_all(now, reason="EOD square-off (unconditional)")
            self.state = DONE if self._total_open() == 0 else FLATTEN_ALL
            return

        # 3. GOVERNOR + halt gate (entries only).  Recompute the governor state
        #    from its meter (the meter is the source of truth).
        self.governor.recompute(now)
        # A loss cap / floor-stop may have asked for a residual flatten — route it
        # through the existing flatten path (the governor never owns its own exit).
        if self.governor.consume_flatten_request():
            await self._flatten_all(now, reason="governor stand-down — flatten residual")

        self._update_state(now)

        if self.state in (PRE_OPEN, DONE, FLATTEN_ALL):
            return

        # 4. PER-UNDERLYING entry scan (NIFTY then BANKNIFTY), cooldown-aware.
        if self.state == ARMED:
            for underlying in self.p.underlyings:
                await self._scan_entry(underlying, now)

    # ── exits (never gated) ───────────────────────────────────────────────────

    async def _manage_exits(self, now: datetime) -> None:
        # One poll per underlying per tick (the SignalProvider.poll contract is
        # per-underlying).  An EXIT applies to the matching leg, or — when the
        # signal names no specific contract (strike==0) — to every open leg of
        # that underlying (a full-flatten exit).
        for underlying in self.p.underlyings:
            if not self._legs[underlying]:
                continue
            sig = self.signals.poll(underlying, now)
            if sig is None or sig.action != "EXIT":
                continue
            # Snapshot legs — the dict mutates as we flatten.
            for sid in list(self._legs[underlying].keys()):
                leg = self._legs[underlying].get(sid)
                if leg is None:
                    continue
                if sig.strike and leg.strike != sig.strike:
                    continue   # targeted exit names a different contract
                await self._submit_exit(leg, sig, now)

    async def _submit_exit(self, leg: OptionLeg, sig: ScalpSignal,
                           now: datetime) -> Optional[Fill]:
        """Submit an EXIT.  Exits are never blocked — we still pass through
        ``risk.check_intent`` (which returns OK for exits by its invariant) so the
        RiskEngine keeps a complete audit, but a False here would be a bug, so we
        log loudly and proceed only if it says OK."""
        if leg.lot <= 0:
            logger.error("ScalperOrchestrator: leg %s has invalid lot %d — cannot "
                         "size exit; leaving to EOD flatten", leg.security_id, leg.lot)
            return None
        pos = self.portfolio.get(leg.security_id)
        qty = abs(pos.qty) if sig.lots <= 0 else min(abs(pos.qty), sig.lots * leg.lot)
        if qty <= 0:
            # nothing on the book — drop our stale leg
            self._drop_leg(leg, now)
            return None

        intent = OrderIntent(
            security_id=leg.security_id, exchange_segment=leg.exchange_segment,
            side="SELL", qty=qty, strategy=SCALPER_STRATEGY,
            reason=sig.reason or "scalper exit", product_type="INTRADAY")

        ref = leg.ltp if leg.ltp > 0 else (pos.avg_price or 0.0)
        ok, why = self.risk.check_intent(intent, ref)
        if not ok:
            # An exit being blocked violates the check_intent invariant — surface
            # it but DO NOT swallow the exit; the EOD flatten is the backstop.
            logger.critical("ScalperOrchestrator: EXIT blocked by risk gate (%s) for "
                            "%s — this should never happen for an exit", why,
                            leg.security_id)
            return None

        fill = await self.executor.submit(intent, ref)
        if fill is None:
            return None
        realized = await self.portfolio.apply_fill(fill, strategy=SCALPER_STRATEGY)
        # Report whole lots to the signal engine.  qty is normally a whole-lot
        # multiple; a non-aligned residual (e.g. a partial fill) still reports at
        # least one lot so the engine's book never silently misses an exit.
        exit_lots = max(1, round(qty / leg.lot))
        self.signals.notify_fill(leg.underlying, "SELL", exit_lots, fill.price, now)
        self.risk.release_risk(leg.security_id)

        # If the contract is now flat, retire the leg + feed the governor.
        if self.portfolio.get(leg.security_id).qty == 0:
            self._drop_leg(leg, now)
            self.governor.record_close(leg.underlying, contracts=1)
            self.governor.record_trade_close(leg.underlying, realized, now)
            self._cooldown_until[leg.underlying] = now + timedelta(
                minutes=self.p.cooldown_min)
            logger.info("ScalperOrchestrator: %s flat — cooldown until %s "
                        "(realized ₹%.0f)", leg.underlying,
                        self._cooldown_until[leg.underlying], realized)
        return fill

    def _drop_leg(self, leg: OptionLeg, now: datetime) -> None:
        self._legs[leg.underlying].pop(leg.security_id, None)

    async def _flatten_all(self, now: datetime, *, reason: str) -> None:
        for underlying in self.p.underlyings:
            for sid in list(self._legs[underlying].keys()):
                leg = self._legs[underlying].get(sid)
                if leg is None:
                    continue
                exit_sig = ScalpSignal(
                    underlying=underlying, action="EXIT", side="SELL",
                    option_type=leg.option_type, strike=leg.strike,
                    lots=0, reason=reason)
                await self._submit_exit(leg, exit_sig, now)

    # ── entries (gated) ───────────────────────────────────────────────────────

    async def _scan_entry(self, underlying: str, now: datetime) -> Optional[Fill]:
        # warm-up window
        open_dt = datetime.combine(now.date(), self.clock.session.open_time,
                                   tzinfo=now.tzinfo)
        if now < open_dt + timedelta(minutes=self.p.warmup_min):
            return None

        # per-index cooldown after a full flatten
        cd = self._cooldown_until.get(underlying)
        if cd is not None and now < cd:
            return None

        # governor opening gate (entries only)
        if not self.governor.allows_open(underlying):
            return None

        sig = self.signals.poll(underlying, now)
        if sig is None or sig.action != "ENTER":
            return None

        leg = self.resolver.resolve(sig)
        if leg is None:
            logger.warning("ScalperOrchestrator: resolver returned no contract for "
                           "%s %s — skipping", underlying, sig.reason)
            return None

        # lot-aligned sizing — whole lots only.
        lots = max(1, int(sig.lots))
        qty = lots * leg.lot
        if qty <= 0:
            return None

        intent = OrderIntent(
            security_id=leg.security_id, exchange_segment=leg.exchange_segment,
            side="BUY", qty=qty, strategy=SCALPER_STRATEGY,
            reason=sig.reason or "scalper entry", product_type="INTRADAY")

        ref = leg.ltp if leg.ltp > 0 else 0.0
        ok, why = self.risk.check_intent(intent, ref)
        if not ok:
            logger.info("ScalperOrchestrator: entry blocked by risk gate for %s: %s",
                        underlying, why)
            return None

        fill = await self.executor.submit(intent, ref)
        if fill is None:
            return None
        await self.portfolio.apply_fill(fill, strategy=SCALPER_STRATEGY)
        self.signals.notify_fill(underlying, "BUY", lots, fill.price, now)

        # register + record this NEW open with the governor.
        self._legs[underlying][leg.security_id] = leg
        self.governor.record_open(underlying, contracts=1)
        self.governor.record_trade_open(underlying)
        logger.info("ScalperOrchestrator: ENTER %s %s%d %d lots @ ₹%.2f — %s",
                    underlying, leg.option_type, leg.strike, lots, fill.price,
                    sig.reason)
        return fill

    # ── state machine ──────────────────────────────────────────────────────────

    def _update_state(self, now: datetime) -> None:
        """Derive the orchestrator state from the clock + governor + book.
        Never overrides FLATTEN_ALL/DONE which are set by the EOD path."""
        if self.state in (FLATTEN_ALL, DONE):
            return

        open_dt = datetime.combine(now.date(), self.clock.session.open_time,
                                   tzinfo=now.tzinfo)
        warm = now >= open_dt + timedelta(minutes=self.p.warmup_min)

        if not warm:
            self.state = PRE_OPEN
            return

        gov = self.governor.state.state
        if gov in ("STOOD_DOWN", "TRAIL_ONLY"):
            # entries off, keep managing whatever is open
            self.state = MANAGING
        else:
            self.state = ARMED

    def _reset_session(self, today: date) -> None:
        self._session_date = today
        self.state = PRE_OPEN
        self._legs = {u: {} for u in self.p.underlyings}
        self._cooldown_until = {u: None for u in self.p.underlyings}
        self.governor.reset_session(today)
        logger.info("ScalperOrchestrator: new session %s — reset", today)

    # ── introspection ──────────────────────────────────────────────────────────

    def _total_open(self) -> int:
        return sum(len(v) for v in self._legs.values())

    def status(self) -> dict:
        return {
            "state": self.state,
            "session_date": str(self._session_date),
            "open_contracts": {u: len(v) for u, v in self._legs.items()},
            "cooldown_until": {u: (c.isoformat() if c else None)
                               for u, c in self._cooldown_until.items()},
            "governor": self.governor.summary(),
        }
