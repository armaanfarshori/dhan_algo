"""
DailyGovernor — per-day, meter-driven risk-governor for the scalper orchestrator.

This is the *strategy-scoped* honesty layer that sits on top of the platform
``engine/risk.py`` ``RiskEngine`` (which still owns the kill-switch and is never
bypassed — Safety rule 2).  Where ``RiskEngine`` enforces equity-fraction loss
halts for the whole platform, this governor enforces *scalper-sleeve* caps:
trade counts, concurrency, a hard daily-loss cap, an optional daily-profit lock
and a consecutive-loss stop.

Design contract (mirrors ``engine/risk.py`` discipline, see
``strategies/scalper_specs/04_daily_governor.md``):

1. **The meter is the source of truth; the state is DERIVED every tick.**  Nothing
   here is edge-triggered bookkeeping: ``recompute()`` rebuilds ``state`` from the
   counters, so a mid-session restart that rebuilds counters lands in the correct
   state.  Call ``recompute(now)`` at the top of every orchestrator tick.

2. **The governor blocks OPENING risk only — it NEVER forces an exit.**  The only
   things that close a position are the unconditional EOD square-off, the scalper's
   own stops/trails, and the platform ``RiskEngine`` kill path.  When a loss cap
   trips the governor goes ``STOOD_DOWN`` and *requests* a flatten through the
   existing kill path (``request_flatten`` flag) — it does not own its own exit
   route (Safety rule 2: never bypass the kill-switch owner).

3. **Per-index AND aggregate meters.**  Every cap exists at the sleeve (aggregate)
   level and, where it makes sense, per-underlying — so one runaway index can't
   eat the whole sleeve's trade budget, and the sleeve total is still capped.

4. **TZ-safe** (CI IST-date trap): every wall-clock read goes through ``now`` which
   the caller supplies as an aware IST datetime; session-date comparison uses
   ``now.date()`` on that aware value, never ``date.today()`` / naive ``now()``.

This module is **pure** (no DB, no network, no asyncio).  The orchestrator feeds it
realized-P&L steps via ``record_trade_close`` and concurrency via ``record_open`` /
``record_close``; tests drive it directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Optional

logger = logging.getLogger("dhan.engine.daily_governor")


# ---------------------------------------------------------------------------
# Parameters — concrete ₹250K-sleeve defaults (research, PAPER, unvalidated)
# ---------------------------------------------------------------------------


@dataclass
class GovernorParams:
    """Caps for one scalper sleeve.  Defaults sized for a ₹250,000 paper sleeve
    running NIFTY + BANKNIFTY long-option scalps; ALL are research defaults and
    are NOT validated (no intraday backtest exists — forward-paper only)."""

    sleeve_capital: float = 250_000.0

    # ── trade-count caps ────────────────────────────────────────────────────
    max_trades_per_day: int = 12          # opens across the whole sleeve / day
    max_trades_per_underlying: int = 8    # opens per index / day

    # ── concurrency caps (open contracts at once) ───────────────────────────
    max_concurrent_total: int = 4         # open contracts across the sleeve
    max_concurrent_per_underlying: int = 2

    # ── hard daily-loss circuit-breaker ─────────────────────────────────────
    max_daily_loss: float = 7_500.0       # ₹ realized-loss → STOOD_DOWN + flatten
                                          #   (≈3% of the ₹250K sleeve)

    # ── optional daily PROFIT-LOCK (defend a green day) ─────────────────────
    daily_profit_lock: Optional[float] = 7_500.0   # None disables; arm at +this
    profit_lock_floor_frac: float = 0.5            # initial floor = arm × this
    profit_lock_giveback: float = 2_500.0          # ₹ off realized high-water
    profit_lock_mode: str = "trail_only"           # "trail_only" | "stop"

    # ── consecutive-loss stop ───────────────────────────────────────────────
    consecutive_loss_stop: int = 4        # N losing trades in a row → STOOD_DOWN


# Governor states (recomputed from the meter every tick).
ACTIVE = "ACTIVE"          # normal — new opens allowed (subject to caps)
LOCKED = "LOCKED"          # profit-lock armed; still trading, day defended
TRAIL_ONLY = "TRAIL_ONLY"  # floor broken (trail_only mode) — manage only, no opens
STOOD_DOWN = "STOOD_DOWN"  # terminal for the day — flat, no opens


@dataclass
class _Meter:
    """Per-scope counters.  Used both per-underlying and for the aggregate."""

    trades_opened: int = 0
    open_contracts: int = 0
    realized_pnl: float = 0.0


@dataclass
class GovernorState:
    state: str = ACTIVE
    block_reason: str = ""
    request_flatten: bool = False     # set once when entering STOOD_DOWN with a
                                      # position; the orchestrator routes the
                                      # flatten through the existing kill path.
    # profit-lock bookkeeping (aggregate sleeve)
    lock_armed: bool = False
    profit_hi_water: float = 0.0
    profit_floor: float = 0.0
    # streak
    consec_losses: int = 0


class DailyGovernor:
    """Meter-driven per-day governor for one scalper sleeve.

    Lifecycle per orchestrator tick:
        gov.recompute(now)              # derive state from the meter (resets on
                                        #   date change)
        if gov.allows_open(underlying): ...   # gate ENTRIES only
        # ...on a completed trade:
        gov.record_trade_close(underlying, pnl, now)

    Concurrency is fed by record_open / record_close as contracts open/flatten.
    """

    def __init__(self, params: Optional[GovernorParams] = None):
        self.p = params or GovernorParams()
        self.state = GovernorState()
        self._session_date: Optional[date] = None
        self._agg = _Meter()
        self._by_under: Dict[str, _Meter] = {}

    # ── session ──────────────────────────────────────────────────────────────

    def _meter(self, underlying: str) -> _Meter:
        return self._by_under.setdefault(underlying, _Meter())

    def reset_session(self, on: date) -> None:
        """Clear all meters + derived state for a new trading day."""
        self._session_date = on
        self._agg = _Meter()
        self._by_under = {}
        self.state = GovernorState()
        logger.info("DailyGovernor: new session %s — meters reset", on)

    def seed_trades_today(self, underlying: str, *, trades_opened: int,
                          realized_pnl: float,
                          open_contracts: Optional[int] = None,
                          consec_losses: int = 0) -> None:
        """Restart-safety: re-derive the day's meter for one underlying from the
        journal (mirrors ``RiskEngine.refresh_pnl``).  Call AFTER reset_session
        for *today* on boot, BEFORE the first recompute.  The aggregate is the
        sum of per-underlying meters, so seeding each index rebuilds the total.

        ``open_contracts`` is OPTIONAL: concurrency is owned by reconcile
        (``record_open`` during boot), so by default this method does NOT touch
        it — passing it would clobber the per-underlying concurrency that
        reconcile already set.  Only pass it when seeding concurrency directly
        (no prior reconcile)."""
        m = self._meter(underlying)
        m.trades_opened = trades_opened
        m.realized_pnl = realized_pnl
        if open_contracts is not None:
            m.open_contracts = open_contracts
        # Aggregate is recomputed from the per-underlying meters (concurrency
        # included — reconcile's record_open already populated it).
        self._agg.trades_opened = sum(x.trades_opened for x in self._by_under.values())
        self._agg.realized_pnl = sum(x.realized_pnl for x in self._by_under.values())
        self._agg.open_contracts = sum(x.open_contracts for x in self._by_under.values())
        # consec_losses is a sleeve-level streak — take the max seed seen.
        self.state.consec_losses = max(self.state.consec_losses, consec_losses)

    # ── concurrency tracking ──────────────────────────────────────────────────

    def record_open(self, underlying: str, contracts: int = 1) -> None:
        self._meter(underlying).open_contracts += contracts
        self._agg.open_contracts += contracts

    def record_close(self, underlying: str, contracts: int = 1) -> None:
        m = self._meter(underlying)
        m.open_contracts = max(0, m.open_contracts - contracts)
        self._agg.open_contracts = max(0, self._agg.open_contracts - contracts)

    def record_trade_open(self, underlying: str) -> None:
        """Count a newly-opened trade (ladder/contract) toward the daily caps."""
        self._meter(underlying).trades_opened += 1
        self._agg.trades_opened += 1

    def record_trade_close(self, underlying: str, pnl: float,
                           now: datetime) -> None:
        """Feed a completed-trade realized P&L step into the meters + lock + streak.

        ``pnl`` is net-of-cost realized ₹ for the closed trade (a full flatten of
        one contract/ladder).  A scratch (==0) is NOT a loss for the streak.
        """
        self._meter(underlying).realized_pnl += pnl
        self._agg.realized_pnl += pnl

        # consecutive-loss streak (sleeve-level)
        if pnl < 0:
            self.state.consec_losses += 1
        elif pnl > 0:
            self.state.consec_losses = 0
        # pnl == 0 (scratch) leaves the streak unchanged

        # ratchet the profit-lock on the realized step (not only on ticks)
        self._update_profit_lock()
        self.recompute(now)

    # ── profit-lock math ──────────────────────────────────────────────────────

    def _update_profit_lock(self) -> None:
        if self.p.daily_profit_lock is None:
            return
        realized = self._agg.realized_pnl
        st = self.state
        if not st.lock_armed:
            if realized >= self.p.daily_profit_lock:
                st.lock_armed = True
                st.profit_hi_water = realized
                st.profit_floor = self.p.daily_profit_lock * self.p.profit_lock_floor_frac
                logger.info("DailyGovernor: profit-lock ARMED at ₹%.0f — floor ₹%.0f",
                            realized, st.profit_floor)
            return
        # already armed — ratchet the floor up off the high-water
        st.profit_hi_water = max(st.profit_hi_water, realized)
        candidate = st.profit_hi_water - self.p.profit_lock_giveback
        st.profit_floor = max(st.profit_floor, candidate)

    @property
    def defended_floor(self) -> float:
        """The level the day is defended at: the loss cap is the absolute hard
        floor; the profit-lock only ever RAISES the defended level above it."""
        hard = -self.p.max_daily_loss
        if self.state.lock_armed:
            return max(self.state.profit_floor, hard)
        return hard

    # ── state derivation (the meter drives the state) ─────────────────────────

    def recompute(self, now: datetime) -> str:
        """Re-derive ``state`` from the meter.  Idempotent; call every tick."""
        today = now.date()
        if self._session_date != today:
            self.reset_session(today)

        st = self.state
        realized = self._agg.realized_pnl

        # 1. hard daily-loss cap → terminal STOOD_DOWN (+ request a flatten once)
        if realized <= -self.p.max_daily_loss:
            self._stand_down(f"daily-loss cap hit (realized ₹{realized:,.0f} "
                             f"≤ −₹{self.p.max_daily_loss:,.0f})")
            return st.state

        # 2. consecutive-loss stop → terminal STOOD_DOWN
        if st.consec_losses >= self.p.consecutive_loss_stop:
            self._stand_down(f"consecutive-loss stop ({st.consec_losses} in a row "
                             f"≥ {self.p.consecutive_loss_stop})")
            return st.state

        # Once STOOD_DOWN, stay there for the rest of the day (terminal).
        if st.state == STOOD_DOWN:
            return st.state

        # 3. profit-lock states (only if armed)
        if st.lock_armed:
            self._update_profit_lock()
            if realized <= st.profit_floor:
                # floor broken — defend it
                if self.p.profit_lock_mode == "stop":
                    self._stand_down(f"profit-floor broken in 'stop' mode "
                                     f"(realized ₹{realized:,.0f} ≤ floor "
                                     f"₹{st.profit_floor:,.0f})")
                    return st.state
                # trail_only: manage open, open no new risk
                st.state = TRAIL_ONLY
                st.block_reason = (f"profit-floor broken — TRAIL_ONLY "
                                   f"(floor ₹{st.profit_floor:,.0f})")
                return st.state
            st.state = LOCKED
            st.block_reason = ""
            return st.state

        st.state = ACTIVE
        st.block_reason = ""
        return st.state

    def _stand_down(self, reason: str) -> None:
        st = self.state
        if st.state != STOOD_DOWN:
            # entering STOOD_DOWN — request a flatten through the kill path if a
            # position is open.  This flag is consumed once by the orchestrator.
            if self._agg.open_contracts > 0:
                st.request_flatten = True
            logger.warning("DailyGovernor: STAND DOWN — %s", reason)
        st.state = STOOD_DOWN
        st.block_reason = reason

    def consume_flatten_request(self) -> bool:
        """Returns True once if the governor has asked for a residual flatten,
        then clears the flag.  The orchestrator routes the flatten through the
        existing kill/flatten path (the governor never owns its own exit)."""
        if self.state.request_flatten:
            self.state.request_flatten = False
            return True
        return False

    # ── opening gate (entries only — NEVER gates exits) ───────────────────────

    def allows_open(self, underlying: str) -> bool:
        return self.block_open_reason(underlying) is None

    def block_open_reason(self, underlying: str) -> Optional[str]:
        """Why a NEW open for ``underlying`` is blocked, or None if allowed.
        EXITS are never routed through here — only entries."""
        st = self.state
        if st.state in (STOOD_DOWN, TRAIL_ONLY):
            return st.block_reason or f"governor state {st.state}"

        m = self._meter(underlying)
        if self._agg.trades_opened >= self.p.max_trades_per_day:
            return (f"sleeve max trades/day reached "
                    f"({self._agg.trades_opened}/{self.p.max_trades_per_day})")
        if m.trades_opened >= self.p.max_trades_per_underlying:
            return (f"{underlying} max trades/day reached "
                    f"({m.trades_opened}/{self.p.max_trades_per_underlying})")
        if self._agg.open_contracts >= self.p.max_concurrent_total:
            return (f"sleeve max concurrent reached "
                    f"({self._agg.open_contracts}/{self.p.max_concurrent_total})")
        if m.open_contracts >= self.p.max_concurrent_per_underlying:
            return (f"{underlying} max concurrent reached "
                    f"({m.open_contracts}/{self.p.max_concurrent_per_underlying})")
        return None

    # ── introspection ─────────────────────────────────────────────────────────

    def summary(self) -> dict:
        return {
            "state": self.state.state,
            "block_reason": self.state.block_reason,
            "session_date": str(self._session_date),
            "aggregate": {
                "trades_opened": self._agg.trades_opened,
                "open_contracts": self._agg.open_contracts,
                "realized_pnl": round(self._agg.realized_pnl, 2),
            },
            "by_underlying": {
                u: {"trades_opened": m.trades_opened,
                    "open_contracts": m.open_contracts,
                    "realized_pnl": round(m.realized_pnl, 2)}
                for u, m in self._by_under.items()
            },
            "lock_armed": self.state.lock_armed,
            "profit_floor": round(self.state.profit_floor, 2),
            "defended_floor": round(self.defended_floor, 2),
            "consec_losses": self.state.consec_losses,
            "max_daily_loss": self.p.max_daily_loss,
        }
